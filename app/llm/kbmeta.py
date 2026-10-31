"""知识库文件的「事件日期」推断与解析。

为什么需要：文件 mtime 是「什么时候整理的」，不是「内容发生在什么时候」。
你今天导入的去年学费文件，mtime 是今天，但它讲的是去年的事。
RAG 检索时若无日期，问「这学期学费多少」可能召回去年的标准。

推断优先级（越靠前越可信，命中即停）：
    1. 用户手动设定（kb_meta.auto=0，最高优先级，永不被覆盖）
    2. 文件名中的日期      2026-08-30_通知.md  → 2026-08-30
    3. 正文前部的日期      首屏出现的 YYYY-MM-DD / YYYY年M月D日
    4. 会议记录的 held_date（transcripts 且与会议名匹配时）
    5. 文件 mtime（兜底，标记为「推断」以便用户核对）

聊天记录不走这里：它由 chunk_chat() 从 ## YYYY-MM-DD 分节直接提取，更准确。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# 文件名/正文里的日期模式，按可信度排序
_PATTERNS = [
    # 2026-08-30 / 2026_08_30 / 2026.08.30
    re.compile(r"(20\d{2})[-_.](0[1-9]|1[0-2])[-_.](0[1-9]|[12]\d|3[01])"),
    # 20260830
    re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"),
    # 2026年8月30日 / 2026年08月30日
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    # 8月30日（缺年份，用 mtime 的年份补）
    re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]

_ISO = "%Y-%m-%d"


def _valid(y: int, m: int, d: int) -> str | None:
    """校验日期合法性，非法返回 None（如 2026-02-30）。"""
    try:
        return datetime(y, m, d).strftime(_ISO)
    except ValueError:
        return None


def date_from_name(name: str, fallback_year: int | None = None) -> str | None:
    """从文件名提取日期。"""
    stem = Path(name).stem
    for pat in _PATTERNS[:3]:
        m = pat.search(stem)
        if m:
            got = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if got:
                return got
    # 「8月30日」缺年份，需要参照年份
    if fallback_year:
        m = _PATTERNS[3].search(stem)
        if m:
            got = _valid(fallback_year, int(m.group(1)), int(m.group(2)))
            if got:
                return got
    return None


def date_from_text(text: str, fallback_year: int | None = None) -> str | None:
    """从正文前部提取日期。

    只看前 2000 字：文档开头通常标注发文日期，而正文里出现的日期
    多为引用或举例，越靠后越不可信。
    """
    head = text[:2000]
    for pat in _PATTERNS[:3]:
        m = pat.search(head)
        if m:
            got = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if got:
                return got
    if fallback_year:
        m = _PATTERNS[3].search(head)
        if m:
            got = _valid(fallback_year, int(m.group(1)), int(m.group(2)))
            if got:
                return got
    return None


def infer_event_date(path: Path, text: str = "",
                     meeting_map: dict[str, str] | None = None) -> tuple[str, str]:
    """推断文件事件日期。

    返回 (日期, 来源说明)。来源用于 UI 展示，让用户知道该值可不可信。
    """
    mtime_year = datetime.fromtimestamp(path.stat().st_mtime).year

    # 1. 文件名
    got = date_from_name(path.name, mtime_year)
    if got:
        return got, "文件名"

    # 2. 会议记录的 held_date（转写稿名通常与会议录音名对应）
    if meeting_map:
        stem = Path(path.stem).stem.replace("_转写", "")
        for key, d in meeting_map.items():
            if key and (key in path.name or path.name.startswith(key)):
                return d, "会议记录"
        # 模糊匹配：去掉扩展名后比较
        for key, d in (meeting_map or {}).items():
            if key and stem and (stem in key or key in stem):
                return d, "会议记录"

    # 3. 正文
    if not text:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    got = date_from_text(text, mtime_year)
    if got:
        return got, "正文"

    # 4. mtime 兜底（标记为推断，用户可改）
    return datetime.fromtimestamp(path.stat().st_mtime).strftime(_ISO), "文件时间"


def meeting_date_map(db) -> dict[str, str]:
    """从 meetings 表取 {会议标题: held_date}，供转写稿匹配。"""
    try:
        rows = db.get_meetings()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for m in rows:
        d = (m.get("held_date") or "").strip()
        title = (m.get("title") or "").strip()
        if d and title:
            out[title] = d[:10]
    return out
