"""聊天记录导入：支持 QQ 导出/解密工具输出的 TXT、CSV、JSON。

典型 TXT 格式（QQ 消息管理器导出，含官方文件头）：
    ==================================================
    消息分组:我的群聊
    ================================================================
    消息对象:班级群(123456789)
    ================================================================
    2026-08-30 12:00:00 小明(12345678)
    明天8点操场集合，重要通知
    收到收到
    2026-08-30 12:05:00 李华(87654321): 我准时到

也兼容无文件头、带 [] 时间戳等变体。
"""
from __future__ import annotations

import csv
import difflib
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger("importer")

TS_RE = re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})[ T](\d{1,2}:\d{2}(?::\d{2})?)")
SENDER_RE = re.compile(r"^\s*[\[【\]]?\s*(.+?)\s*[（(](\d{5,})[)）]\s*[:：]?\s*(.*)$")
# QQ 官方导出文件头：消息分组 / 消息对象 / 分隔线
GROUP_PART_RE = re.compile(r"^\s*消息分组[:：]\s*(.+?)\s*$")
GROUP_OBJ_RE = re.compile(r"^\s*消息对象[:：]\s*(.+?)\s*$")
# 群对象两种形态：群名(群号) 或 纯群号
OBJ_PAIR_RE = re.compile(r"^(.+?)\s*[（(](\d{5,})[)）]\s*$")

# 内容相似度阈值：用于识别「同一条消息的不同解析结果」（坑 #95）
CONTENT_SIM_MIN = 0.92


def _content_supersedes(old: str, new: str) -> bool:
    """两条内容是否是「同一条消息的不同解析结果」。

    为什么需要（坑 #95）：去重键含 content。解析升级（占位符 → 真正文、
    正文末尾补卡片链接、图片占位符补后缀）之后重新同步，同一条消息的
    content 变了就绕过旧去重键，于是再插一条——聊天文件里同一句话出现
    两遍、日历里同一件事抽两遍。

    两条保守规则认定「同一条」：
    1. 一条是另一条的前缀（占位符升级、末尾追加链接都是这个形态）；
    2. difflib 相似度 ≥ 0.92 且前 20 字相同。
    宁可漏合并（多一条，能肉眼发现）也不能把两条真正不同的消息
    合成一条（那是数据丢失），所以阈值取得很紧。
    """
    a, b = (old or "").strip(), (new or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 8 and long.startswith(short):
        return True
    if len(a) >= 20 and len(b) >= 20 and a[:20] == b[:20]:
        return difflib.SequenceMatcher(None, a, b).ratio() >= CONTENT_SIM_MIN
    return False


def _find_same_slot(c, group_id: int, user_id: int, ts: int):
    """同一 (群, 用户, 秒) 上的唯一一条消息（(id, content)）。

    同秒多条时返回 None：那种情况分不清谁是谁，一律不动（坑 #95）。
    """
    rows = c.execute(
        "SELECT id, content FROM messages"
        " WHERE group_id=? AND user_id=? AND ts=?", (group_id, user_id, ts)
    ).fetchall()
    return rows[0] if len(rows) == 1 else None



def extract_meta(text: str) -> dict:
    """从 QQ 官方导出 TXT 的文件头提取群号/群名。"""
    meta = {"group_id": None, "group_name": ""}
    for raw in text.splitlines():
        line = raw.strip()
        m = GROUP_OBJ_RE.match(line)
        if m:
            obj = m.group(1).strip()
            pair = OBJ_PAIR_RE.match(obj)
            if pair:
                meta["group_name"] = pair.group(1).strip()
                meta["group_id"] = int(pair.group(2))
            elif obj.isdigit() and len(obj) >= 5:
                meta["group_id"] = int(obj)
            else:
                meta["group_name"] = obj
            continue
        m = GROUP_PART_RE.match(line)
        if m and not meta["group_name"]:
            meta["group_name"] = m.group(1).strip()
    return meta


def _parse_ts(date_part: str, time_part: str) -> int:
    date_part = date_part.replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(f"{date_part} {time_part}", fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value) -> bool:
    """CSV/JSON 里的「重要」列：是/1/true/yes 都算。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"是", "1", "true", "yes", "y", "⭐"}


def _to_int_ts(value) -> int:
    """把各种时间表示统一成秒级时间戳。

    历史坑：13 位毫秒时间戳（如 1730000000000）被直接当秒用，会变成
    公元 5 万年，排序/日期过滤全乱（审计 P0-6）。这里统一归一化。
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        ts = int(value)
    else:
        s = str(value).strip()
        if not s:
            return 0
        if s.isdigit():
            ts = int(s)
        elif s.replace(".", "", 1).isdigit():
            ts = int(float(s))
        else:
            try:
                ts = int(datetime.fromisoformat(
                    s.replace("Z", "+00:00")).timestamp())
            except (ValueError, TypeError):
                s2 = s.replace("/", "-")
                ts = 0
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        ts = int(datetime.strptime(s2[:len(fmt) + 4], fmt).timestamp())
                        break
                    except ValueError:
                        continue
                if not ts:
                    m = TS_RE.search(s)
                    if m:
                        ts = _parse_ts(*m.groups())
                if not ts:
                    return 0
    # 毫秒 → 秒（> 10^11 即 5138 年以后，不可能）
    if ts > 10 ** 11:
        ts //= 1000
    if ts <= 0:
        return 0
    # 超出 datetime 可表示范围的值直接归零，避免渲染时抛异常
    try:
        datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return 0
    return ts


def parse_txt(text: str) -> list[dict]:
    """解析 TXT，返回 [{ts, user_id, nickname, content}]。"""
    records: list[dict] = []
    cur: dict | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 跳过 QQ 官方导出文件头：分隔线 / 消息分组 / 消息对象
        if line.startswith("=") or line.startswith("消息分组") \
                or line.startswith("消息对象"):
            continue
        m = TS_RE.search(line)
        if m:
            # 新消息：时间戳行里可能带发送者和内容
            if cur and cur.get("content"):
                records.append(cur)
            ts = _parse_ts(*m.groups())
            rest = line[m.end():].strip()
            sm = SENDER_RE.match(rest)
            if sm:
                nickname, uid, inline = sm.groups()
                cur = {"ts": ts, "user_id": int(uid),
                       "nickname": nickname.strip(), "content": inline.strip()}
            else:
                cur = {"ts": ts, "user_id": 0,
                       "nickname": "未知", "content": rest}
        elif cur is not None:
            # 续行：追加内容
            if cur.get("content"):
                cur["content"] += "\n" + line
            else:
                cur["content"] = line
    if cur and cur.get("content"):
        records.append(cur)
    return records


def parse_csv(path: Path) -> list[dict]:
    """解析 CSV（兼容本项目 export_csv 的 9 列格式与旧的 4 列格式）。

    多群导出时每行自带「群号/群名」，回导按行归属，不会全塞进一个群。
    """
    records: list[dict] = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            gid = _safe_int(row.get("群号") or row.get("group_id") or 0)
            content = (row.get("内容") or row.get("content") or "").strip()
            records.append({
                "ts": _to_int_ts(row.get("时间") or row.get("time")
                                 or row.get("ts") or row.get("timestamp")),
                "user_id": _safe_int(row.get("发送者QQ") or row.get("user_id") or 0),
                "nickname": (row.get("昵称") or row.get("nickname") or "未知").strip(),
                "content": content,
                "msg_type": (row.get("类型") or row.get("msg_type") or "text").strip(),
                "important": _truthy(row.get("重要") or row.get("important")),
                "important_reason": (row.get("重要原因")
                                     or row.get("important_reason") or "").strip(),
                "group_id": gid or None,
                "group_name": (row.get("群名") or row.get("group_name") or "").strip(),
            })
    return records


def parse_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        # 兼容 {"messages": [...]}
        data = data.get("messages") or data.get("data") or []
    if not isinstance(data, list):
        return []
    records: list[dict] = []
    for item in data:
        gid = _safe_int(item.get("group_id") or item.get("群号") or 0)
        records.append({
            "ts": _to_int_ts(item.get("ts") or item.get("time")
                             or item.get("timestamp") or 0),
            "user_id": _safe_int(item.get("user_id") or item.get("qq")
                                 or item.get("sender_id") or 0),
            "nickname": (item.get("nickname") or item.get("name")
                         or item.get("sender") or "未知").strip(),
            "content": (item.get("content") or item.get("message")
                        or item.get("text") or "").strip(),
            "msg_type": (item.get("msg_type") or item.get("type") or "text").strip(),
            "important": bool(item.get("important")),
            "important_reason": (item.get("important_reason") or "").strip(),
            "group_id": gid or None,
            "group_name": (item.get("group_name") or "").strip(),
        })
    return records


def load_records(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return parse_csv(path)
    if suffix == ".json":
        return parse_json(path)
    return parse_txt(path.read_text(encoding="utf-8-sig", errors="replace"))


def dedup_messages(db: Database, dry_run: bool = False) -> dict:
    """清理重复消息：同一 (group_id, user_id, ts, content) 只保留最早一条。

    为什么需要（坑 #9）：去重键含 content。当解析格式升级导致同一条消息的
    content 变化（如 ``[图片]`` → ``[图片:xxx.jpg]``）时，重新导入会因
    content 不同而绕过去重，产生重复记录——2026-08-31 修复图片格式后
    重跑同步，就因此多出 14 条。

    保留策略：保留 id 最小的（最早入库的），删除后插入的重复项。
    """
    c = db._conn()
    rows = c.execute(
        "SELECT GROUP_CONCAT(id) AS pks FROM messages"
        " GROUP BY group_id, user_id, ts, content"
        " HAVING COUNT(*) > 1").fetchall()
    removed = 0
    for r in rows:
        pks = sorted(int(x) for x in (r[0] or "").split(",") if x)
        for pk in pks[1:]:
            if not dry_run:
                c.execute("DELETE FROM messages WHERE id=?", (pk,))
            removed += 1
    if not dry_run and removed:
        c.commit()
        log.info("清理重复消息 %d 条（%d 组）", removed, len(rows))
    return {"removed": removed, "groups": len(rows)}


def import_records(db: Database, cfg: Settings, group_id: int,
                   group_name: str, records: list[dict],
                   dry_run: bool = False, replace: bool = False,
                   force: bool = False, source: str = "file") -> dict:
    """把解析好的记录写入数据库（本机库同步 / 文件导入共用）。

    入库约定（改动会影响两条导入线，务必同步测试）：
    - message_id 用负数自减空间，避免与实时消息冲突
    - 去重键：group_id + user_id + ts + content（同一备份重复导、
      多分片重叠都靠它兜底）
    - force: 绕过群白名单检查
    - source: 写入 raw_json 的来源标记（file / qq），`--replace` 只清理 file
    """
    if not group_id:
        return {"ok": False, "error": "无法确定群号：请用 --group 指定，"
                                      "或确认导出文件头部含“消息对象:群名(群号)”"}

    # 群白名单：与实时监听共用同一份 watch_groups。
    # 白名单为空 = 不限制；非空时只放行名单内的群，避免导入整库却把无关群带进来。
    # 好友（负数 group_id）不走群白名单，由 watch_buddies 单独控制（见 qq_sync）。
    if group_id > 0:
        from .watchlist import allows_group
        if not allows_group(cfg, group_id) and not force:
            return {"ok": False, "blocked_by_whitelist": True,
                    "error": (f"群 {group_id} 不在白名单中，已拒绝导入。\n"
                              f"请在设置页把它加进白名单；若确实要导入，"
                              f"命令行加 --force 跳过检查。")}

    recs = [r for r in records if (r.get("content") or "").strip()]
    c = db._conn()

    # 试运行：只统计，不写库（旧实现在 dry_run 之前就 upsert 群/成员，审计 P0-6）
    if dry_run:
        inserted = skipped = updated = 0
        for r in recs:
            exists = c.execute(
                "SELECT id FROM messages WHERE group_id=? AND user_id=? AND ts=? AND content=?",
                (group_id, r["user_id"], r["ts"], r["content"]),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            twin = _find_same_slot(c, group_id, r["user_id"], r["ts"])
            if twin and _content_supersedes(twin[1], r["content"]):
                updated += 1
            else:
                inserted += 1
        return {"ok": True, "dry_run": True, "inserted": inserted,
                "skipped": skipped, "updated": updated, "total": len(records),
                "group_id": group_id, "group_name": group_name}

    # 重导：清掉该群旧「文件导入」消息（本机库同步/实时消息不动），避免占位符升级后重复。
    # 删除前先落一份 CSV 备份：replace 若中途失败，仍可人工恢复。
    removed = 0
    if replace:
        try:
            from .exporter import export_csv
            out_dir = Path(cfg.resolve("data/exports"))
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = out_dir / f"before_replace_{group_id}_{stamp}.csv"
            info = export_csv(db, backup, group_id=group_id)
            log.info("重导前已备份该群 %d 条消息到 %s", info["rows"], backup)
        except Exception as e:  # 备份失败就不能删，否则真丢数据
            return {"ok": False, "error": f"重导前备份失败，已中止（未删除任何消息）：{e}"}

    # 整批一个事务：删除旧消息 + 写入新消息要么全成功要么全回滚（审计 P0-9）。
    # 显式 BEGIN IMMEDIATE 还能锁住「最小负 id」的读取窗口，避免并发导入撞号。
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    try:
        if replace:
            removed = db.delete_imported(group_id, source="file", commit=False)
            log.info("重导模式：已删除该群旧文件导入 %d 条", removed)
        # 群不存在则创建（群号 + 群名）；成员一并补齐
        db.upsert_group(group_id, group_name or f"导入群{group_id}", 0, commit=False)
        for r in recs:
            if r["user_id"]:
                db.upsert_member(group_id, r["user_id"], r["nickname"], "",
                                 "member", commit=False)
        # message_id 用负数自增空间，避免与实时消息冲突。
        # 必须从当前已用最小负 id 继续递减，否则多群导入时 message_id 全局
        # 唯一约束冲突导致后续群静默丢失（表现为“跳过重复”）。
        row = c.execute(
            "SELECT MIN(message_id) FROM messages WHERE message_id < 0").fetchone()
        fake_id = (row[0] if row and row[0] else 0) - 1
        inserted = skipped = updated = 0
        for r in recs:
            exists = c.execute(
                "SELECT id FROM messages WHERE group_id=? AND user_id=? AND ts=? AND content=?",
                (group_id, r["user_id"], r["ts"], r["content"]),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            # 同一秒同一个人只存了一条、且内容像是同一条消息升级后的解析结果
            # → 就地更新内容，不再插一条重复的（坑 #95）。
            twin = _find_same_slot(c, group_id, r["user_id"], r["ts"])
            if twin and _content_supersedes(twin[1], r["content"]):
                c.execute("UPDATE messages SET content=? WHERE id=?",
                          (r["content"], twin[0]))
                updated += 1
                continue
            if "important" in r:
                # CSV/JSON 自带重要标记：原样回写，保证导出→导入往返一致
                important = bool(r.get("important"))
                reason = r.get("important_reason") or ("重要（导入保留）" if important else "")
            else:
                important, reason = _judge_important(cfg, r["content"])
            fake_id -= 1
            if db.insert_message(fake_id, group_id, r["user_id"], r["nickname"],
                                 r.get("msg_type") or "text", r["content"],
                                 {"imported": True, "source": source}, r["ts"],
                                 important, reason, commit=False):
                inserted += 1
            else:
                skipped += 1
        c.commit()
    except Exception:
        c.rollback()
        raise
    # 有新消息入库 → 标记聊天渲染/索引待补，下一轮自动同步即使「无新消息」
    # 也会把派生数据补齐（审计 P0-9 / 批次 2 checkpoint）
    try:
        from .pipeline import mark_dirty
        mark_dirty(cfg, "chat", "index")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "inserted": inserted, "skipped": skipped,
            "updated": updated, "total": len(records), "removed": removed,
            "group_id": group_id, "group_name": group_name}


def _judge_important(cfg: Settings, content: str) -> tuple[bool, str]:
    """复用监听规则判断重要消息（@全体成员 + 关键词）。"""
    reasons: list[str] = []
    imp = cfg.important or {}
    if imp.get("at_all") and ("@全体成员" in content
                              or "@all" in content.lower()):
        reasons.append("@全体成员")
    for kw in imp.get("keywords", []):
        if kw and kw in content:
            reasons.append(kw)
    return bool(reasons), "、".join(x for x in reasons if x)


def import_file(db: Database, cfg: Settings, file_path: Path,
                group_id: int = 0, group_name: str = "", dry_run: bool = False,
                replace: bool = False, force: bool = False) -> dict:
    """导入文件到指定群，返回统计。

    group_id / group_name 可省略：TXT 会尝试从 QQ 官方导出文件头
    （消息对象:群名(群号)）自动识别。
    replace: 导入前先删除该群已有的 imported 消息，实现“重导”。
    force: 绕过群白名单检查（白名单非空且该群不在其中时，默认拒绝导入）。
    """
    file_path = Path(file_path)
    if not file_path.exists():
        return {"ok": False, "error": f"文件不存在: {file_path}"}

    # QQ 新版备份包（ZIP 容器）：用独立密钥加密，与本机会话密钥不同
    # （实测解不开，参数扫描记录见项目内的解密研究笔记）。
    # 正确姿势：QQ 客户端恢复 bak → 消息进本机库 → 网页「从本机 QQ 库同步」。
    if file_path.suffix.lower() == ".bak":
        with open(file_path, "rb") as f:
            if f.read(4) == b"PK\x03\x04":
                return {"ok": False, "error": (
                    "这是 QQ 新版备份包（ZIP 加密容器），暂不支持直接解密。\n"
                    "请先在 QQ「消息管理器 → 导入消息记录」中恢复该备份，"
                    "然后到知识库页点「从本机 QQ 库同步」——恢复后的消息"
                    "会从本机数据库直接提取，无需再导入文件。")}

    records = load_records(file_path)
    if not records:
        return {"ok": False, "error": "未能从文件中解析出任何消息（格式不兼容？）"}

    # 未显式指定群时：TXT 从 QQ 官方导出文件头识别；CSV/JSON 按每行「群号」分组。
    meta: dict = {}
    if file_path.suffix.lower() not in (".csv", ".json"):
        meta = extract_meta(
            file_path.read_text(encoding="utf-8-sig", errors="replace"))
    explicit_gid = int(group_id or 0)
    groups: dict[int, str] = {}
    if explicit_gid:
        groups[explicit_gid] = group_name or meta.get("group_name") or ""
    else:
        for r in records:
            gid = int(r.get("group_id") or meta.get("group_id") or 0)
            if not gid:
                continue
            groups.setdefault(
                gid, r.get("group_name") or meta.get("group_name") or "")
        if not groups:
            return {"ok": False, "error": "无法确定群号：请用 --group 指定，"
                                          "或确认导出文件含「消息对象:群名(群号)」/「群号」列"}

    agg = {"ok": True, "inserted": 0, "skipped": 0, "removed": 0,
           "total": 0, "groups": [], "dry_run": bool(dry_run)}
    for gid, gname in groups.items():
        subset = records if explicit_gid else [
            r for r in records if int(r.get("group_id") or 0) == gid]
        if not subset:
            continue
        res = import_records(db, cfg, gid, gname, subset, dry_run=dry_run,
                             replace=replace, force=force, source="file")
        if not res.get("ok"):
            res["partial"] = agg
            return res
        agg["inserted"] += int(res.get("inserted") or 0)
        agg["skipped"] += int(res.get("skipped") or 0)
        agg["removed"] += int(res.get("removed") or 0)
        agg["total"] += int(res.get("total") or 0)
        agg["groups"].append({"group_id": gid, "group_name": gname})
    return agg
