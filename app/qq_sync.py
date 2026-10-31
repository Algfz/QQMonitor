"""从本机 NTQQ 数据库（nt_msg.db）同步群消息——消息的权威来源。

为什么走本机库而不是 bak 备份包
--------------------------------
QQ 客户端在线时，新消息实时写入本机 `nt_msg.db`（含未 checkpoint 的 WAL）。
这是**已验证可用**的链路（见 `tools/ntqq_export.py`）：
剥 1024 字节自定义头 → SQLCipher 4（4096 页 / kdf_iter 4000 /
HMAC-SHA1 / PBKDF2-HMAC-SHA512）→ passphrase 解密。

而 QQ「备份聊天记录」生成的 `.bak` 是 ZIP 容器（内含 MSG0 分片），
用**独立密钥**加密，与本机会话密钥不同（实测同一 passphrase 解不开，
参数扫描见 `_probe_scan.py`）。因此 bak 的正确姿势是：
QQ 客户端「导入消息记录」恢复 → 消息进本机库 → 本模块同步。

一致性方案（WAL）
-----------------
主库与 WAL 必须是同一时刻的状态，否则 HMAC 校验失败
（database disk image is malformed）。两次独立拷贝之间 QQ 若在写库，
就会不一致。对策：**连续拷贝（窗口 <0.5s）+ 打开失败自动重试**，
实测一次通过率很高，重试兜底。

群范围
------
- 白名单非空：同步白名单内的全部群（含新群，自动入库）
- 白名单为空：只更新数据库**已有**的群（不自动引入新群，
  避免一次灌进几万条无关消息；想加新群先把群号填进白名单）

群名来源：本机 QQ 库 `recent_contact_v3_table` 的 [40021]=群号 / [40094]=群名
（NTQQ 最近会话表，只在最近会话里的群才有，但覆盖绝大多数活跃群）。

取不到名字时回退为「群{gid}」，之后拖入带文件头的 TXT 也可修正群名。
"""
from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
import time
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger("qq_sync")

_TOOL = None


def _fetch_names_from_db(cfg: Settings) -> tuple[dict[int, str], dict[int, str]]:
    """一次性从本机 QQ 库读取「群名」与「好友备注/昵称」。

    NTQQ 最近会话表 recent_contact_v3_table：
      [40021]=群号（str）  [40094]=群名/昵称  [41135]=备注
      [40030]=好友 QQ（群行为 0）
    只覆盖最近会话里的群/好友，但活跃会话基本都在其中。
    只拷贝一次库（237MB），群名与好友名共用这一次解密结果。
    """
    nt_db = (cfg.knowledge.get("nt_db") or "").strip()
    key = (cfg.knowledge.get("key") or "").strip()
    groups: dict[int, str] = {}
    friends: dict[int, str] = {}
    if not nt_db or not Path(nt_db).exists() or not key:
        return groups, friends
    try:
        import sqlcipher3.dbapi2  # noqa: F401
    except ImportError:
        return groups, friends
    try:
        with tempfile.TemporaryDirectory(prefix="qqmonitor_names_") as td:
            td = Path(td)
            raw = td / "nt_msg.db"
            clear = td / "cleartext.db"
            _copy_with_wal(Path(nt_db), raw)
            conn = _open_cleartext(Path(nt_db), key, raw, clear)
            try:
                rows = conn.execute(
                    "SELECT [40021], [40030], [40094], [41135]"
                    " FROM recent_contact_v3_table").fetchall()
            finally:
                conn.close()
        suffixes = [str(x).strip() for x in
                    ((cfg.auto_watch or {}).get("by_remark_suffix") or []) if str(x).strip()]
        for gid_str, qq, nick, remark in rows:
            nick = str(nick).strip() if nick else ""
            remark = str(remark).strip() if remark else ""
            gid = None
            try:
                if gid_str:
                    gid = int(gid_str)
            except (ValueError, TypeError):
                gid = None
            if gid and gid > 0:
                # 群：优先「群名」[40094]，没有再用备注
                label = nick or remark
            else:
                # 好友：优先备注，没有再用昵称
                label = remark or nick
            # 去掉用户为「自动白名单」加的后缀（_._），避免界面里显示脏名字
            for suf in suffixes:
                if label.endswith(suf):
                    label = label[:-len(suf)].rstrip()
                    break
            if not label:
                continue
            if gid and gid > 0:
                groups[gid] = label
                continue
            try:
                q = int(qq or 0)
            except (ValueError, TypeError):
                q = 0
            if q > 0:
                friends[q] = label
        if groups or friends:
            log.info("已从本机 QQ 库读取 %d 个群名、%d 个好友备注",
                     len(groups), len(friends))
    except Exception as e:  # noqa: BLE001
        log.debug("从本机 QQ 库读群名/好友名失败，跳过：%s", e)
    return groups, friends


def _strip_auto_suffix(name: str, cfg: Settings) -> str:
    """去掉用户为自动白名单加的备注后缀（如 _._），避免界面显示脏名字。"""
    name = (name or "").strip()
    for suf in ((cfg.auto_watch or {}).get("by_remark_suffix") or []):
        suf = str(suf).strip()
        if suf and name.endswith(suf):
            return name[:-len(suf)].rstrip()
    return name


def _is_fallback_name(name: str, gid: int) -> bool:
    """判断群名是否为自动生成的 fallback 格式（「群{gid}」）。"""
    return name.strip() in (f"群{gid}", f"群 {gid}")


def _tool():
    """延迟加载 tools/ntqq_export.py，复用其 protobuf 解析（40800 → 文本）。

    不复制代码是为了避免两份解析逻辑漂移（字段号 45101/45402 等）。
    该模块顶层 import sqlcipher3，需等确认 sqlcipher3 可用后再加载。
    """
    global _TOOL
    if _TOOL is None:
        import importlib.util
        p = Path(__file__).resolve().parent.parent / "tools" / "ntqq_export.py"
        spec = importlib.util.spec_from_file_location("ntqq_export", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TOOL = mod
    return _TOOL


def _copy_with_wal(nt_db: Path, raw: Path) -> None:
    """主库 + WAL 连续拷贝（先库后 wal，窗口越小越好）。"""
    shutil.copy2(nt_db, raw)
    wal = nt_db.with_name(nt_db.name + "-wal")
    if wal.exists():
        try:
            shutil.copy2(wal, raw.with_name(raw.name + "-wal"))
        except OSError as e:
            # WAL 被独占的概率极低；丢了只是少最近几条消息，不阻断
            log.warning("WAL 拷贝失败（%s），仅同步已 checkpoint 数据", e)


def _open_cleartext(nt_db: Path, key: str, raw: Path, clear: Path):
    """剥头 + 配套 WAL + SQLCipher 打开。返回 connection。"""
    import sqlcipher3.dbapi2 as sc

    clear.write_bytes(raw.read_bytes()[1024:])
    wal = raw.with_name(raw.name + "-wal")
    if wal.exists():
        shutil.copy2(wal, clear.with_name(clear.name + "-wal"))

    conn = sc.connect(str(clear), isolation_level=None)
    # PRAGMA 顺序必须为 page_size → key → 其余参数
    # （key 之后的参数 PRAGMA 会触发重新派生，顺序错了解不开）
    conn.execute("PRAGMA cipher_page_size = 4096;")
    safe = key.replace("'", "''")
    conn.execute(f"PRAGMA key = '{safe}';")
    conn.execute("PRAGMA kdf_iter = 4000;")
    conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1;")
    conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
    return conn


def _check_lag(db: Database, gid: int, name: str, recs: list[dict],
               out: dict) -> None:
    """同步后校验：库内最新时间戳是否落后于本机库（防漏消息）。"""
    src_latest = max((r.get("ts") or 0 for r in recs), default=0)
    if not src_latest:
        return
    db_latest = db.latest_ts(gid)
    if db_latest and db_latest < src_latest:
        gap = src_latest - db_latest
        log.warning("同步校验：%s 库内最新 %s 落后本机库 %s（%d 秒），请检查是否漏同步",
                    name, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(db_latest)),
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(src_latest)), gap)
        out.setdefault("lag", []).append(
            {"group_id": gid, "name": name, "gap_s": gap})


@contextlib.contextmanager
def open_nt_connection(cfg: Settings, retries: int = 2):
    """临时打开本机 NTQQ 库（拷贝 → 剥头 → 解密），退出时清理临时目录。

    给「白名单自动规则」这种只读少量表的场景用。完整同步仍走
    sync_from_ntqq（它有自己的 per-group 进度与重试逻辑），而自动规则
    在同步流程里是复用那条已经打开的连接（apply_from_conn），不会多拷一份
    237MB 的库。
    """
    nt_db = (cfg.knowledge.get("nt_db") or "").strip()
    key = (cfg.knowledge.get("key") or "").strip()
    if not nt_db or not key:
        raise ValueError("config.yaml 未配置 knowledge.nt_db / knowledge.key")
    nt_path = Path(nt_db)
    if not nt_path.exists():
        raise ValueError(f"找不到 {nt_db}（QQ 未登录或路径已变）")
    try:
        import sqlcipher3.dbapi2  # noqa: F401
    except ImportError as e:
        raise ValueError("未安装 sqlcipher3（pip install sqlcipher3）") from e

    with tempfile.TemporaryDirectory(prefix="qqmonitor_nt_") as td:
        td = Path(td)
        raw, clear = td / "raw.db", td / "clear.db"
        conn = None
        last: Exception | None = None
        for attempt in range(1, retries + 2):
            try:
                _copy_with_wal(nt_path, raw)
                conn = _open_cleartext(nt_path, key, raw, clear)
                conn.execute("SELECT count(*) FROM group_msg_table").fetchone()
                break
            except Exception as e:  # noqa: BLE001
                last = e
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    conn = None
                if attempt <= retries:
                    log.warning("打开本机库失败（%s），重试 %d/%d",
                                e, attempt, retries)
        if conn is None:
            raise ValueError(f"读取本机 QQ 库失败：{last}")
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def sync_from_ntqq(db: Database, cfg: Settings, dry_run: bool = False) -> dict:
    """从本机 NTQQ 库同步群消息入库，返回统计（幂等，可重复执行）。

    返回 {ok, groups: [{group_id, name, inserted, skipped, total}],
          inserted, total, detail, error}
    """
    from .importer import dedup_messages, import_records

    nt_db = (cfg.knowledge.get("nt_db") or "").strip()
    if not nt_db:
        return {"ok": False, "error": "未配置本机 QQ 数据库路径：请在 config.yaml"
                                      " 的 knowledge.nt_db 填入 nt_msg.db 路径"}
    nt_path = Path(nt_db)
    if not nt_path.exists():
        return {"ok": False, "error": f"找不到 {nt_db}（QQ 未登录或路径已变）"}
    key = (cfg.knowledge.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "未配置解密密钥：请在 config.yaml 的"
                                      " knowledge.key 填入（qq-win-db-key 提取）"}
    try:
        import sqlcipher3.dbapi2  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "未安装 sqlcipher3"
                                      "（pip install sqlcipher3），无法解密"}

    tool = _tool()
    out = {"ok": True, "groups": [], "inserted": 0, "total": 0,
           "detail": "", "missing": []}

    # 拷贝 → 剥头 → 打开 → 验证；malformed（拷贝窗口撞上 QQ 写库）则重试
    conn = None
    with tempfile.TemporaryDirectory(prefix="qqmonitor_nt_") as td:
        td = Path(td)
        raw, clear = td / "raw.db", td / "clear.db"
        for attempt in range(1, 4):
            try:
                _copy_with_wal(nt_path, raw)
                conn = _open_cleartext(nt_path, key, raw, clear)
                conn.execute("SELECT count(*) FROM group_msg_table").fetchone()
                break
            except Exception as e:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                log.warning("第 %d 次打开本机库失败：%s", attempt, e)
                if attempt == 3:
                    return {"ok": False,
                            "error": f"读取本机 QQ 库失败（已重试 3 次）：{e}。"
                                     f"QQ 正在密集写入时可稍后再试"}
        if conn is None:
            return {"ok": False, "error": "读取本机 QQ 库失败"}

        # 白名单自动规则（备注名后缀 / 置顶）：必须在下面读 cfg.watch_groups
        # 之前跑。本函数是唯一持有解密连接的地方，顺路派生可省一次 237MB
        # 库拷贝；apply 就地更新 cfg.watch_groups / watch_buddies，后面的
        # 群过滤、导入校验、知识库同步都直接看到新名单。
        if not dry_run:
            try:
                from .watchlist import try_apply_from_conn
                wl = try_apply_from_conn(conn, cfg, db)
                if wl is None:
                    pass  # 另一个规则任务正在跑，本轮跳过（日志已在 watchlist 里）
                elif wl.get("error"):
                    log.warning("白名单自动规则未生效：%s", wl["error"])
                elif wl.get("changed"):
                    log.info("白名单已按规则自动调整：群 +%s/-%s，好友 +%s/-%s"
                             "（后缀 %s）",
                             wl.get("added_groups"), wl.get("removed_groups"),
                             wl.get("added_buddies"), wl.get("removed_buddies"),
                             "/".join(wl.get("suffixes") or []))
            except Exception as e:  # noqa: BLE001
                log.warning("白名单自动规则执行失败（不影响同步）：%s: %s",
                            type(e).__name__, e)

        try:
            # 群过滤：白名单优先；为空时只更新已有群（不自动引入新群）
            all_groups = tool.list_groups(conn)  # [(gid, count)]
            watch = {int(g) for g in (cfg.watch_groups or [])}
            existing = {g["group_id"] for g in db.get_groups()}
            if watch:
                targets = [(g, c) for g, c in all_groups if g in watch]
                scope = f"白名单 {len(targets)}/{len(all_groups)} 个群"
                # 白名单里写了、但本机库没有的群，必须明确报出来。
                # 否则用户点了同步却毫无反应，完全不知道是群号错了还是没数据
                missing = sorted(g for g in watch
                                 if g not in {x for x, _ in all_groups})
                if missing:
                    out["missing"] = missing
            else:
                targets = [(g, c) for g, c in all_groups if g in existing]
                missing2 = sorted(existing - {x for x, _ in all_groups})
                if missing2:
                    out["missing"] = missing2
                scope = f"已导入群 {len(targets)}/{len(all_groups)} 个"
            log.info("本机库共 %d 个群，本次同步%s", len(all_groups), scope)

            # 同步前从本机库读一次真实群名（读不到就是空 dict，静默降级）
            real_names, _friend_names_now = _fetch_names_from_db(cfg)

            for gid, cnt in targets:
                g = db.get_group(gid)
                # 群名优先级：本机库真实群名 > 用户手工命名/TXT 导入（非 fallback 格式）
                #             > 已入库的 fallback 名（会被真实群名覆盖）> 群{gid}
                existing = (g or {}).get("name") or ""
                is_fallback = (not existing) or _is_fallback_name(existing, gid)
                if gid in real_names:
                    name = real_names[gid]
                elif not is_fallback:
                    name = existing          # 用户手工命名，保留
                else:
                    name = f"群{gid}"
                name = _strip_auto_suffix(name, cfg)
                # 群号字段回退：新创建群/小号群 [40030] 可能为 0（坑 #21）
                rows = conn.execute(
                    # 群消息昵称在 [40093]，[40090] 大多数群为空（实测）
                    "SELECT [40033],[40050],COALESCE(NULLIF([40093],''),[40090]),[40800]"
                    " FROM group_msg_table"
                    " WHERE COALESCE(NULLIF([40030],0), NULLIF([40027],0),"
                    "       [40021])=? ORDER BY [40050]", (gid,)).fetchall()
                recs = []
                for sender, ts, nick, blob in rows:
                    content = tool.extract_content(blob)
                    if not content:
                        continue
                    recs.append({
                        "ts": int(ts or 0),
                        "user_id": int(sender) if sender else 0,
                        "nickname": (nick or "").strip(),
                        "content": content,
                    })
                    # 文件消息：只登记（文件名/大小/md5/本地路径），
                    # 真正复制进知识库由 group_files.process_pending 负责。
                    if blob and not dry_run:
                        for f in tool.extract_file_elements(blob):
                            db.upsert_group_file(
                                gid, int(ts or 0), f["name"],
                                size=f["size"], md5=f["md5"],
                                file_id=f["file_id"], busid=f["busid"],
                                local_path=f["local"])
                r = import_records(db, cfg, gid, name, recs, dry_run=dry_run,
                                   source="qq")
                log.info("本机库同步群 %s（%s）：导入 %d / %d 条",
                         gid, name, r["inserted"], r["total"])
                out["groups"].append({"group_id": gid, "name": name, **r})
                out["inserted"] += r["inserted"]
                out["total"] += r["total"]
                if not dry_run:
                    _check_lag(db, gid, name, recs, out)

            # ============ 好友消息同步（c2c_msg_table）============
            # 好友用负数 group_id（-好友QQ），与群（正数）区分，复用同一套表/渲染/页面。
            # 好友名：本机库备注/昵称 > 已入库非 fallback 名 > 好友{QQ}
            try:
                all_buddies = tool.list_buddies(conn)  # [(qq, count)]
            except Exception as e:
                log.warning("查询好友列表失败（c2c_msg_table 可能不存在）：%s", e)
                all_buddies = []
            if all_buddies:
                friend_names = _friend_names_now
                # 好友白名单：cfg.watch_buddies。好友隐私敏感度高于群，语义与群相反——
                # 白名单为空 = 一个好友都不同步（不采集私聊）；必须显式把好友 QQ
                # 加进 watch_buddies 才同步。这与群「为空只更新已有、不引入新群」
                # 的保护意图一致，只是好友更严格（连已有也要白名单才更新）。
                watch_b = {int(b) for b in (cfg.watch_buddies or [])}
                if not watch_b:
                    log.info("watch_buddies 为空，跳过全部 %d 个好友会话"
                             "（如需同步好友，请在设置页加入白名单）", len(all_buddies))
                    buddy_targets = []
                else:
                    buddy_targets = [(q, c) for q, c in all_buddies if q in watch_b]
                    log.info("本机库共 %d 个好友会话，本次同步白名单 %d/%d 个好友",
                             len(all_buddies), len(buddy_targets), len(all_buddies))

                for qq, cnt in buddy_targets:
                    buddy_gid = -qq  # 负数 group_id 表示好友
                    g = db.get_group(buddy_gid)
                    existing = (g or {}).get("name") or ""
                    is_fallback = (not existing) or _is_fallback_name(existing, buddy_gid)
                    if qq in friend_names:
                        name = friend_names[qq]
                    elif not is_fallback:
                        name = existing  # 用户手工命名，保留
                    else:
                        name = f"好友{qq}"
                    name = _strip_auto_suffix(name, cfg)
                    rows = conn.execute(
                        "SELECT [40033],[40050],[40093],[40800] FROM c2c_msg_table"
                        " WHERE [40030]=? ORDER BY [40050]", (qq,)).fetchall()
                    recs = []
                    for sender, ts, nick, blob in rows:
                        content = tool.extract_content(blob)
                        if not content:
                            continue
                        recs.append({
                            "ts": int(ts or 0),
                            "user_id": int(sender) if sender else 0,
                            "nickname": (nick or "").strip(),
                            "content": content,
                        })
                        if blob and not dry_run:
                            for f in tool.extract_file_elements(blob):
                                db.upsert_group_file(
                                    buddy_gid, int(ts or 0), f["name"],
                                    size=f["size"], md5=f["md5"],
                                    file_id=f["file_id"], busid=f["busid"],
                                    local_path=f["local"])
                    r = import_records(db, cfg, buddy_gid, name, recs,
                                       dry_run=dry_run, source="qq")
                    log.info("本机库同步好友 %s（%s）：导入 %d / %d 条",
                             qq, name, r["inserted"], r["total"])
                    out["groups"].append({"group_id": buddy_gid, "name": name, **r})
                    out["inserted"] += r["inserted"]
                    out["total"] += r["total"]
                    if not dry_run:
                        _check_lag(db, buddy_gid, name, recs, out)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # 去重兜底：解析格式升级后，同一条消息的 content 会变（如 [图片] →
    # [图片:xxx.jpg]），去重键含 content 因而失效，重跑同步会产生重复（坑 #9）。
    # 这里每次同步后自动收敛，保证 messages 表不会因重复操作而膨胀。
    if not dry_run:
        dedup = dedup_messages(db)
        if dedup["removed"]:
            log.info("同步后自动去重：清理重复 %d 条", dedup["removed"])
            out["deduped"] = dedup["removed"]

    # 群文件入库：只处理「本机已有副本」的文件（不联网下载），每轮限量。
    # 失败不影响消息同步——文件入库只是知识库增强项。
    if not dry_run:
        try:
            from .group_files import process_pending
            fr = process_pending(db, cfg)
            if fr.get("scanned"):
                out["files"] = fr
                log.info("群文件自动入库：%s", fr.get("detail", ""))
        except Exception as e:
            log.warning("群文件入库失败（不影响消息同步）：%s", e)

    if not out["groups"]:
        out["detail"] = "本机库中没有符合范围的群消息。"
        if out.get("missing"):
            m = "、".join(str(g) for g in out["missing"][:5])
            out["detail"] += (f"⚠ 白名单/已导入的群在本机库里都没有记录（{m}）。"
                              f"本机库只收录已有消息的群，可能原因："
                              f"① 群号填错；② 该群不在这个 QQ 号下；"
                              f"③ 群刚建还没有消息——"
                              f"在群里发一条消息后重新同步即可。")
        else:
            out["detail"] += "想同步新群请先把群号加进设置页白名单。"
        return out

    parts = [f"{g['name']}：+{g['inserted']}"
             + (f"（总 {g['total']}）" if g["inserted"] < g["total"] else "")
             for g in out["groups"] if g["inserted"] or g["total"]]
    out["detail"] = (f"从本机 QQ 库同步完成：新增 {out['inserted']} 条"
                     f"（{len(out['groups'])} 个群）。"
                     + "；".join(parts[:8])
                     + ("…" if len(parts) > 8 else ""))
    if out.get("missing"):
        m = "、".join(str(g) for g in out["missing"][:5])
        out["detail"] += (f" ⚠ 本机库里没有这些群的记录（{m}"
                          + ("…" if len(out["missing"]) > 5 else "")
                          + "）。本机库只收录已有消息的群，可能原因："
                            "① 群号填错；② 该群不在这个 QQ 号下；"
                            "③ 群刚建还没有消息——"
                            "在群里发一条消息后重新同步即可。")
    return out


def reparse_messages(db: Database, cfg: Settings, dry_run: bool = False) -> dict:
    """重新解析数据库里已有消息的 content，补全旧格式丢失的媒体文件名。

    背景：早期入库时提取器还不支持 QQ 新版「云文件 ID」格式，图片消息
    只存成裸的 ``[图片]``，导致聊天 Markdown 里没有链接、也无法 OCR。
    现在提取器已支持，用本机库把 content 重跑一遍即可补全为
    ``[图片:xxxx.jpg]``（媒体链接与 OCR 随后由 sync_knowledge 生成）。

    匹配依据：group_id + user_id + ts（同一人同一秒的消息视为同一条）。
    只 UPDATE，不新增——去重键含 content，插入会产生重复。
    """
    nt_db = (cfg.knowledge.get("nt_db") or "").strip()
    key = (cfg.knowledge.get("key") or "").strip()
    if not nt_db or not Path(nt_db).exists():
        return {"ok": False, "error": "未配置或找不到本机 QQ 数据库（knowledge.nt_db）"}
    if not key:
        return {"ok": False, "error": "未配置解密密钥（knowledge.key）"}
    try:
        import sqlcipher3.dbapi2  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "未安装 sqlcipher3（pip install sqlcipher3）"}

    tool = _tool()
    out = {"ok": True, "checked": 0, "updated": 0, "groups": 0, "detail": ""}

    with tempfile.TemporaryDirectory(prefix="qqmonitor_re_") as td:
        td = Path(td)
        raw, clear = td / "raw.db", td / "clear.db"
        conn = None
        for attempt in range(1, 4):
            try:
                _copy_with_wal(Path(nt_db), raw)
                conn = _open_cleartext(Path(nt_db), key, raw, clear)
                conn.execute("SELECT count(*) FROM group_msg_table").fetchone()
                break
            except Exception as e:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                if attempt == 3:
                    return {"ok": False,
                            "error": f"读取本机 QQ 库失败（重试 3 次）：{e}"}
        if conn is None:
            return {"ok": False, "error": "读取本机 QQ 库失败"}

        try:
            targets = [g["group_id"] for g in db.get_groups()]
            watch = {int(g) for g in (cfg.watch_groups or [])}
            if watch:
                targets = [g for g in targets if g in watch]
            out["groups"] = len(targets)
            c = db._conn()
            for gid in targets:
                # 群号列 [40030] 在新群上可能为 0（坑 #83）：必须与全表同步同源地用
                # tools/ntqq_export._GID_SQL 的 COALESCE 谓词，否则这类群的 reparse
                # 永远命中 0 行，历史消息的裸占位再也补不上。
                rows = conn.execute(
                    "SELECT [40033],[40050],[40800] FROM group_msg_table"
                    f" WHERE {tool._GID_SQL}=? AND [40800] IS NOT NULL",
                    (gid,)).fetchall()
                for sender, ts, blob in rows:
                    new = tool.extract_content(blob)
                    if not new:
                        continue
                    uid = int(sender) if sender else 0
                    ts_i = int(ts or 0)
                    old = c.execute(
                        "SELECT content FROM messages WHERE group_id=?"
                        " AND user_id=? AND ts=?", (gid, uid, ts_i)).fetchone()
                    if not old:
                        continue
                    out["checked"] += 1
                    old_c = old[0] or ""
                    # 只在「新解析结果带文件名、旧的没有」时更新
                    if old_c != new and ":" not in old_c and ":" in new:
                        if dry_run:
                            out["updated"] += 1
                            continue
                        cur = c.execute(
                            "UPDATE messages SET content=? WHERE group_id=?"
                            " AND user_id=? AND ts=? AND content=?",
                            (new, gid, uid, ts_i, old_c))
                        if cur.rowcount:
                            out["updated"] += 1
            if not dry_run:
                c.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    out["detail"] = (f"重新解析完成：检查 {out['checked']} 条，"
                     f"补全媒体文件名 {out['updated']} 条"
                     f"（{out['groups']} 个群）。"
                     + ("（试运行，未写入）" if dry_run else
                        "请再点一次「重新同步」生成图片链接与 OCR。"))
    return out
