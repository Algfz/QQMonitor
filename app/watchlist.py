"""白名单自动规则：备注名带约定后缀（默认 `_._`）的群/好友自动纳入。

需求与设计
----------
「所有备注名加上特定后缀的群聊和好友自动加入白名单，且随自动扫描程序
自动修改白名单」。备注名和置顶会话都在本机 NTQQ 库里，客户端实时写入，
因此不需要 NapCat、不需要插件，只要在扫描时顺路读一次。

三层名单（都在 config.yaml）：
- ``watch_groups`` / ``watch_buddies``：**生效名单**，所有下游消费者
  （导入 / 本机库同步 / 知识库同步）只读这一份。
- ``watch_groups_manual`` / ``watch_buddies_manual``：用户在设置页亲手
  勾选的基线。自动规则**绝不**删它，否则用户删掉一个被置顶的群，
  下一轮扫描又会被自动加回，永远删不掉。
- ``auto_watch``：规则开关 + 用户手动移除过的自动项（ignore_*）。
  取消备注后缀时，只有「自动加入」的项会被移出；手工项原地保留。

数据源（实测 QQNT）
-----------------------------------------------
``nt_msg.db``:
- ``recent_contact_v3_table``: [40021]=peer id（群=群号字符串；私聊=uid），
  [40030]=好友 QQ 号（群行为 0），[40094]=昵称/群名，[41135]=备注名，
  [41104]=置顶标志
- ``recent_contact_top_table``: [40010] 1=私聊 / 2=群，[60001]=群号，
  [1000]=好友 uid（置顶的权威来源，[41104] 会滞后）

噪声过滤：频道私信/系统会话的 peer id 不是 6~10 位群号（如 17 位
``34380991649560441``、``102``/``103``），必须排除，否则会被当成群号写进
白名单，同步时白跑一趟还报「白名单里未导入的群」。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .config import Settings

log = logging.getLogger("watchlist")

STATE_NAME = "watchlist.state.json"

# 群号合法区间：6~10 位。低于 10 万的是系统会话，高于 10 位是频道私信 id
GROUP_MIN, GROUP_MAX = 100_000, 9_999_999_999

# recent_contact_v3_table 里不是真实群/好友的 peer id
_SKIP_PEERS = {"", "0", "102", "103"}

AUTO_DEFAULTS: dict = {
    "enabled": True,
    "by_remark_suffix": ["_._"],
    "by_pin": False,
    "scope": "both",          # both / groups / friends
    "ignore_groups": [],
    "ignore_buddies": [],
}


# ---------- 配置读取 ----------

def _as_list(value) -> list:
    """把 yaml 里的列表/逗号分隔字符串统一成列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [t for t in str(value).replace("，", ",").replace("、", ",")
            .replace(" ", ",").split(",") if t.strip()]


def _norm_suffixes(value) -> list[str]:
    out: list[str] = []
    for s in _as_list(value):
        s = str(s).strip()
        if s and s not in out:
            out.append(s)
    return out


def _int_list(value) -> list[int]:
    out: list[int] = []
    for v in _as_list(value):
        try:
            out.append(int(str(v).strip()))
        except (TypeError, ValueError):
            continue
    return out


def auto_cfg(cfg: Settings | None) -> dict:
    """把 cfg.auto_watch 规范化成带默认值的字典（缺字段/类型错都能兜住）。"""
    raw = dict(AUTO_DEFAULTS)
    if cfg is not None:
        raw.update({k: v for k, v in (cfg.auto_watch or {}).items()
                    if v is not None})
    out = dict(raw)
    out["enabled"] = bool(raw.get("enabled", True))
    out["by_remark_suffix"] = _norm_suffixes(raw.get("by_remark_suffix"))
    out["by_pin"] = bool(raw.get("by_pin", False))
    scope = str(raw.get("scope") or "both").strip().lower()
    out["scope"] = scope if scope in ("both", "groups", "friends") else "both"
    out["ignore_groups"] = _int_list(raw.get("ignore_groups"))
    out["ignore_buddies"] = _int_list(raw.get("ignore_buddies"))
    return out


def state_path(cfg: Settings) -> Path:
    base = cfg.resolve(cfg.storage.get("db_path", "data/qqmonitor.db")).parent
    return base / STATE_NAME


# ---------- 生效名单（唯一权威读取点） ----------
# 所有消费者（实时监听 / 导入 / 本机库同步 / 聊天渲染 / 媒体同步 / 索引 / 检索）
# 都必须走这三个函数，不要各自再读 cfg.watch_groups 拼语义——群与好友的
# 「空名单」语义相反（群：空=不限制；好友：空=一个都不同步），散落判断极易写反
# （审计批次 2 ⑩）。

def effective(cfg: Settings) -> dict[str, set[int]]:
    """当前生效名单：{"groups": {群号}, "buddies": {好友QQ}}。"""
    return {
        "groups": {int(g) for g in (cfg.watch_groups or []) if str(g).strip()},
        "buddies": {int(b) for b in (cfg.watch_buddies or []) if str(b).strip()},
    }


def allows_group(cfg: Settings, group_id: int) -> bool:
    """群是否在范围内。空名单 = 不限制（所有已入库群都放行）。"""
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return False
    if gid <= 0:
        return False
    groups = effective(cfg)["groups"]
    return not groups or gid in groups


def allows_buddy(cfg: Settings, gid_or_qq: int) -> bool:
    """好友（私聊）是否在范围内。空名单 = 不同步任何好友。"""
    try:
        qq = abs(int(gid_or_qq))
    except (TypeError, ValueError):
        return False
    return qq in effective(cfg)["buddies"]


def allows_conversation(cfg: Settings, gid: int) -> bool:
    """统一入口：负数 gid 视为好友（-QQ），正数视为群。"""
    try:
        gid = int(gid)
    except (TypeError, ValueError):
        return False
    return allows_buddy(cfg, gid) if gid < 0 else allows_group(cfg, gid)


def load_state(cfg: Settings) -> dict:
    """读上一轮规则结果（设置页展示用；坏文件/不存在都返回空字典）。"""
    p = state_path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(cfg: Settings, data: dict) -> None:
    p = state_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError as e:
        log.warning("写白名单状态文件失败：%s", e)


# ---------- 从本机库派生 ----------

def _matches(remark: str, suffixes: list[str]) -> str:
    """备注名是否以某个后缀结尾，返回命中的后缀（空串=没命中）。"""
    r = remark.strip()
    if not r:
        return ""
    low = r.casefold()
    for s in suffixes:
        if low.endswith(s.casefold()):
            return s
    return ""


def _pinned(conn) -> tuple[set[int], set[str]]:
    """读置顶会话：返回 (置顶群号集合, 置顶好友 uid 集合)。

    recent_contact_top_table 是置顶的权威表；老版本 QQ 没有这张表时
    静默返回空集合，by_pin 规则自然失效（不影响备注后缀规则）。
    """
    gids: set[int] = set()
    uids: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT [40010], [60001], [1000] FROM recent_contact_top_table"
        ).fetchall()
    except sqlite3.Error:
        return gids, uids
    for typ, gid, uid in rows:
        try:
            typ = int(typ or 0)
        except (TypeError, ValueError):
            continue
        if typ == 2 and gid:
            try:
                gids.add(int(gid))
            except (TypeError, ValueError):
                pass
        elif typ == 1 and uid:
            uids.add(str(uid).strip())
    return gids, uids


def derive(conn, ac: dict | None = None) -> dict:
    """从已解密的 NTQQ 连接派生命中会话（只读，不写任何东西）。

    返回 {ok, error, groups: [{id,name,remark,reasons}], buddies: [...],
          scanned, pinned_groups, pinned_buddies}
    """
    ac = ac or auto_cfg(None)
    suffixes = ac["by_remark_suffix"]
    out: dict = {"ok": False, "error": "", "groups": [], "buddies": [],
                 "scanned": 0, "pinned_groups": [], "pinned_buddies": [],
                 "enabled": bool(ac["enabled"]) and bool(suffixes or ac["by_pin"])}
    if not out["enabled"]:
        out["ok"] = True
        return out
    scope = ac["scope"]
    ignore_g, ignore_b = set(ac["ignore_groups"]), set(ac["ignore_buddies"])

    try:
        rows = conn.execute(
            "SELECT [40021], [40030], [40094], [41135] FROM recent_contact_v3_table"
        ).fetchall()
    except sqlite3.Error as e:
        out["error"] = f"读取会话表失败：{e}（QQ 版本过旧或缺表？）"
        return out
    out["scanned"] = len(rows)

    pinned_gids, pinned_uids = _pinned(conn)
    out["pinned_groups"] = sorted(pinned_gids)
    out["pinned_buddies"] = sorted(pinned_uids)

    # 先建 uid → QQ 映射：置顶私聊只给 uid，需要换回 QQ 号才能进白名单
    uid2qq: dict[str, int] = {}
    for row in rows:
        peer = "" if row[0] is None else str(row[0]).strip()
        try:
            qq = int(row[1] or 0)
        except (TypeError, ValueError):
            qq = 0
        if peer and qq > 0:
            uid2qq[peer] = qq

    group_seen: set[int] = set()
    buddy_seen: set[int] = set()
    for peer, qq, name, remark in rows:
        peer = "" if peer is None else str(peer).strip()
        if peer in _SKIP_PEERS:
            continue
        name = ("" if name is None else str(name)).strip()
        remark = ("" if remark is None else str(remark)).strip()
        if peer.isdigit():
            gid = int(peer)
            if not (GROUP_MIN <= gid <= GROUP_MAX) or gid in group_seen:
                continue
            reasons: list[str] = []
            hit = _matches(remark, suffixes) if suffixes else ""
            if hit:
                reasons.append(f"备注以 {hit} 结尾")
            if ac["by_pin"] and gid in pinned_gids:
                reasons.append("会话置顶")
            if not reasons or scope == "friends":
                continue
            group_seen.add(gid)
            if gid in ignore_g:
                continue
            out["groups"].append({"id": gid, "name": name or remark,
                                  "remark": remark, "reasons": reasons})
        else:
            try:
                qq_int = int(qq or 0)
            except (TypeError, ValueError):
                qq_int = 0
            qq_int = qq_int or uid2qq.get(peer, 0)
            if qq_int <= 0 or qq_int in buddy_seen:
                continue
            reasons = []
            hit = _matches(remark, suffixes) if suffixes else ""
            if hit:
                reasons.append(f"备注以 {hit} 结尾")
            if ac["by_pin"] and peer in pinned_uids:
                reasons.append("会话置顶")
            if not reasons or scope == "groups":
                continue
            buddy_seen.add(qq_int)
            if qq_int in ignore_b:
                continue
            out["buddies"].append({"id": qq_int, "name": remark or name,
                                   "remark": remark, "reasons": reasons})

    out["groups"].sort(key=lambda x: x["id"])
    out["buddies"].sort(key=lambda x: x["id"])
    out["ok"] = True
    return out


# ---------- 合并并写回 ----------

def apply_derived(cfg: Settings, derived: dict, db=None) -> dict:
    """把派生结果合并进生效名单并持久化。

    生效名单 = (手工基线 ∪ 自动命中) − ignore。只有集合真的变了才写盘，
    避免每 3 分钟刷新一次 config.yaml 的 mtime（Web 端会跟着反复重载）。
    """
    ac = auto_cfg(cfg)
    old_groups = {int(g) for g in (cfg.watch_groups or [])}
    old_buddies = {int(b) for b in (cfg.watch_buddies or [])}
    manual_groups = {int(g) for g in (cfg.watch_groups_manual or [])}
    manual_buddies = {int(b) for b in (cfg.watch_buddies_manual or [])}

    summary: dict = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": bool(derived.get("ok")),
        "error": derived.get("error") or "",
        "enabled": bool(ac["enabled"]),
        "suffixes": ac["by_remark_suffix"],
        "by_pin": ac["by_pin"],
        "scope": ac["scope"],
        "ignore_groups": ac["ignore_groups"],
        "ignore_buddies": ac["ignore_buddies"],
        "scanned": derived.get("scanned", 0),
        "auto_groups": [], "auto_buddies": [],
        "named_groups": [], "named_buddies": [],
        "added_groups": [], "removed_groups": [],
        "added_buddies": [], "removed_buddies": [],
        "changed": False, "wrote": False,
    }
    if not summary["ok"] or not summary["enabled"]:
        # 读库失败/规则关闭：保持现状，绝不移除任何条目
        _write_state(cfg, summary)
        return summary

    auto_g = {int(x["id"]): x for x in derived.get("groups") or []}
    auto_b = {int(x["id"]): x for x in derived.get("buddies") or []}
    ignore_g = set(ac["ignore_groups"])
    ignore_b = set(ac["ignore_buddies"])
    eff_groups = (manual_groups | set(auto_g)) - ignore_g
    eff_buddies = (manual_buddies | set(auto_b)) - ignore_b

    summary.update({
        "auto_groups": sorted(auto_g),
        "auto_buddies": sorted(auto_b),
        "named_groups": [auto_g[g] for g in sorted(auto_g)],
        "named_buddies": [auto_b[b] for b in sorted(auto_b)],
        "added_groups": sorted(set(auto_g) - old_groups - manual_groups),
        "removed_groups": sorted(old_groups - eff_groups),
        "added_buddies": sorted(set(auto_b) - old_buddies - manual_buddies),
        "removed_buddies": sorted(old_buddies - eff_buddies),
    })
    summary["changed"] = bool(summary["added_groups"]
                              or summary["removed_groups"]
                              or summary["added_buddies"]
                              or summary["removed_buddies"])
    try:
        summary["wrote"] = cfg.set_watchlists(
            sorted(eff_groups), sorted(eff_buddies),
            sorted(manual_groups), sorted(manual_buddies), cfg.auto_watch)
    except OSError as e:
        summary["ok"] = False
        summary["error"] = f"写回 config.yaml 失败：{e}"
        log.warning("写回白名单失败：%s", e)
    # 被移出白名单的会话：删除聊天 md + 向量块 + 事件日期（不删消息本体，
    # 重新加回还能重渲染），否则它们会变成「幽灵知识」继续被 AI 检索到。
    if db is not None and summary["wrote"] and summary["changed"]:
        try:
            from .knowledge import purge_conversation
            for gid in summary["removed_groups"]:
                purge_conversation(db, cfg, int(gid),
                                   drop_messages=False, drop_media=False)
            for qq in summary["removed_buddies"]:
                purge_conversation(db, cfg, -int(qq),
                                   drop_messages=False, drop_media=False)
        except Exception as e:  # noqa: BLE001  派生数据清理失败不应阻断白名单写入
            log.warning("清理移出白名单会话的派生数据失败：%s", e)
    _write_state(cfg, summary)
    return summary


def apply_from_conn(conn, cfg: Settings, db=None) -> dict:
    """在已打开的 NTQQ 连接上跑一轮规则（sync 顺路调用，零额外拷贝）。"""
    return apply_derived(cfg, derive(conn, auto_cfg(cfg)), db=db)


# 同一进程内串行化「打开本机库跑规则」：每次都要整份拷贝 nt_msg.db，
# 自动同步与设置页「立即刷新」撞在一起会白白做两次，还可能与写盘交错。
_RUN_LOCK = threading.Lock()


def try_apply_from_conn(conn, cfg: Settings, db=None) -> dict | None:
    """sync 路径用：拿到锁就跑，别的任务正在跑就跳过本轮（返回 None）。"""
    if not _RUN_LOCK.acquire(blocking=False):
        log.warning("自动白名单规则本轮跳过：另一处规则任务正在执行")
        return None
    try:
        return apply_from_conn(conn, cfg, db=db)
    finally:
        _RUN_LOCK.release()


def apply_from_nt(cfg: Settings, db=None) -> dict:
    """独立跑一轮（设置页「立即刷新」/ `main.py watchlist`）：拷贝解密一次。"""
    from .qq_sync import open_nt_connection

    if not _RUN_LOCK.acquire(blocking=False):
        return {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ok": False, "changed": False,
                "error": "自动规则正在执行（自动同步或另一次刷新），请稍后重试"}
    try:
        with open_nt_connection(cfg) as conn:
            return apply_from_conn(conn, cfg, db=db)
    except ValueError as e:
        summary = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": False, "error": str(e),
            "enabled": auto_cfg(cfg)["enabled"],
            "suffixes": auto_cfg(cfg)["by_remark_suffix"],
            "changed": False,
        }
        _write_state(cfg, summary)
        return summary
    finally:
        _RUN_LOCK.release()


def preview(cfg: Settings) -> dict:
    """只读预览：不写白名单、不写状态文件（设置页保存前展示用）。"""
    from .qq_sync import open_nt_connection

    ac = auto_cfg(cfg)
    try:
        with open_nt_connection(cfg) as conn:
            derived = derive(conn, ac)
    except ValueError as e:
        return {"ok": False, "error": str(e), "groups": [], "buddies": []}
    manual_g = {int(g) for g in (cfg.watch_groups_manual or [])}
    manual_b = {int(b) for b in (cfg.watch_buddies_manual or [])}
    for item in derived["groups"]:
        item["is_manual"] = item["id"] in manual_g
        item["in_whitelist"] = item["id"] in {int(g) for g in cfg.watch_groups or []}
    for item in derived["buddies"]:
        item["is_manual"] = item["id"] in manual_b
        item["in_whitelist"] = item["id"] in {int(b) for b in cfg.watch_buddies or []}
    return derived
