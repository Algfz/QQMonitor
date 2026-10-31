"""周 / 月 / 年总结：把该时间段内知识库的**全部资料**交给模型归纳。

与旧版的区别：
    旧版  只取 messages 表（聊天），且硬截断 30000 字符
           → 月/年总结实际只看到 1%~17% 内容，形同虚设且误导
    新版  取 kb_chunks 中该时间段内的所有片段（聊天 + 会议转写 + 文档），
           并做分层汇总（map-reduce），保证真正覆盖全部内容

数据源说明：
    走 kb_chunks 而非直接读文件——分块逻辑统一（聊天按天、文档按段），
    且已带事件日期。因此需要**先建立索引**；未建索引时会明确提示。

「日」频率已移除：日总结用现有的 digest.py（模板拼接、离线免费、可回溯原文），
两者定位不同——摘要看流水账，总结看归纳。
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import Settings
from ..db import Database
from .client import LLMClient, LLMError
from .prompts import (SUMMARY_PRESETS, SUMMARY_SYSTEM, SUMMARY_USER,
                      REDUCE_SYSTEM, REDUCE_USER)

log = logging.getLogger("llm.summary")

# 周期：已去掉 day（日总结用 digest.py）
PERIODS = ("week", "month", "year")

# 单次送入模型的字符上限。超出则走分层汇总（先分片小结，再汇总）。
# 按 4 万中文字符 ≈ 2.7 万 token 估算，留足输出空间。
_MAX_CHARS = 40000

# 分层时每个小组的目标字符数
_GROUP_CHARS = 20000


def period_range(period: str, base: date | None = None) -> tuple[str, str, str]:
    """返回 (date_from, date_to, 范围描述)。"""
    d = base or date.today()
    if period == "week":
        mon = d - timedelta(days=d.weekday())
        sun = mon + timedelta(days=6)
        return mon.isoformat(), sun.isoformat(), f"{mon.isoformat()} ~ {sun.isoformat()}"
    if period == "month":
        last = calendar.monthrange(d.year, d.month)[1]
        return (f"{d.year:04d}-{d.month:02d}-01",
                f"{d.year:04d}-{d.month:02d}-{last:02d}",
                f"{d.year:04d}-{d.month:02d}")
    if period == "year":
        return f"{d.year:04d}-01-01", f"{d.year:04d}-12-31", f"{d.year:04d} 年"
    raise ValueError(f"未知周期：{period}，可选 {PERIODS}")


def _fmt_chunks(chunks: list[dict], max_chars: int) -> tuple[str, bool]:
    """把片段按「来源 + 日期」组织成给模型看的文本。返回 (文本, 是否截断)。"""
    parts: list[str] = []
    total = 0
    truncated = False
    for c in chunks:
        head = f"【{c['source']}】"
        if c.get("day"):
            head += f" {c['day']}"
        block = f"{head}\n{c['text'].strip()}"
        if total + len(block) > max_chars:
            truncated = True
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts), truncated


def _group_by_size(chunks: list[dict], size: int) -> list[list[dict]]:
    """按累计字符数把片段分成若干组，保持原有顺序。"""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for c in chunks:
        n = len(c.get("text") or "")
        if cur and cur_len + n > size:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(c)
        cur_len += n
    if cur:
        groups.append(cur)
    return groups


def _summarize_group(client: LLMClient, chunks: list[dict], period_label: str,
                     range_label: str, idx: int, total: int) -> str:
    """对单个小组做初步归纳（map 阶段）。"""
    body, _ = _fmt_chunks(chunks, _MAX_CHARS)
    prompt = SUMMARY_USER.format(
        period_label=period_label,
        range_label=f"{range_label}（第 {idx}/{total} 部分）",
        content=body,
        max_words=600,
    )
    return client.chat([{"role": "system", "content": SUMMARY_SYSTEM},
                        {"role": "user", "content": prompt}])


def _source_gid(source: str) -> int | None:
    """从 kb_chunks.source（形如 chat/<群号>_<群名>.md）取群号。"""
    name = str(source or "").replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".md"):
        name = name[:-3]
    head = name.split("_", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def generate_summary(db: Database, cfg: Settings, client: LLMClient,
                     period: str = "week", base: date | None = None,
                     group_id: int | None = None,
                     progress=None) -> dict:
    """生成指定周期的总结。

    数据量小 → 一次调用完成
    数据量大 → 分层汇总：先按组小结，再汇总各组小结（保证覆盖全部内容）
    """
    if period not in PERIODS:
        raise ValueError(f"未知周期：{period}，可选 {PERIODS}")

    date_from, date_to, range_label = period_range(period, base)
    preset = SUMMARY_PRESETS[period]

    chunks = db.kb_chunks_by_date(date_from, date_to)
    if group_id:
        # 群号过滤只对聊天片段有意义（文件名形如 chat/<群号>_<群名>.md）
        chunks = [c for c in chunks
                  if c["section"] != "chat"
                  or _source_gid(c["source"]) == int(group_id)]
    if not chunks:
        return {"period": period, "range": range_label, "content": "",
                "path": "", "chunks": 0, "empty": True}

    body, truncated = _fmt_chunks(chunks, _MAX_CHARS)

    if not truncated:
        # 量不大，一次搞定
        prompt = SUMMARY_USER.format(
            period_label=preset["period_label"], range_label=range_label,
            content=body, max_words=preset["max_words"])
        content = client.chat(
            [{"role": "system", "content": SUMMARY_SYSTEM},
             {"role": "user", "content": prompt}])
        levels = 1
    else:
        # 分层汇总：map → reduce，真正覆盖全部内容
        groups = _group_by_size(chunks, _GROUP_CHARS)
        log.info("%s总结：%d 个片段超长，分 %d 组做分层汇总",
                 preset["period_label"], len(chunks), len(groups))
        partials: list[str] = []
        for i, g in enumerate(groups, 1):
            if progress:
                progress(i, len(groups) + 1, f"归纳第 {i}/{len(groups)} 部分")
            partials.append(_summarize_group(client, g, preset["period_label"],
                                             range_label, i, len(groups)))
        if progress:
            progress(len(groups) + 1, len(groups) + 1, "汇总各部分")
        merged = "\n\n".join(
            f"### 第 {i} 部分归纳\n{t}" for i, t in enumerate(partials, 1))
        content = client.chat(
            [{"role": "system", "content": REDUCE_SYSTEM},
             {"role": "user", "content": REDUCE_USER.format(
                 period_label=preset["period_label"], range_label=range_label,
                 parts=merged, max_words=preset["max_words"])}])
        levels = 2

    out_dir = (cfg.resolve(cfg.storage.get("digest_dir", "data/digests")).parent
               / "summaries")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{period}_{date_from.replace('-', '')}.md"
    if group_id:
        name = f"{period}_{date_from.replace('-', '')}_{group_id}.md"
    path = out_dir / name

    n_chat = sum(1 for c in chunks if c["section"] == "chat")
    n_trs = sum(1 for c in chunks if c["section"] == "transcripts")
    n_doc = sum(1 for c in chunks if c["section"] == "documents")
    header = (f"# {preset['period_label']}总结（{range_label}）\n\n"
              f"> AI 生成 · 基于 {len(chunks)} 个片段"
              f"（聊天 {n_chat} / 转写 {n_trs} / 文档 {n_doc}）"
              f"{' · 分层汇总' if levels > 1 else ''}\n\n")
    path.write_text(header + content + "\n", encoding="utf-8")
    log.info("生成%s总结：%s（%d 片段，%d 层）", preset["period_label"], name,
             len(chunks), levels)

    return {"period": period, "range": range_label, "content": content,
            "path": str(path), "chunks": len(chunks), "levels": levels,
            "n_chat": n_chat, "n_trs": n_trs, "n_doc": n_doc}


def list_summaries(out_dir: Path) -> list[dict]:
    """列出已生成的总结文件。"""
    if not out_dir.is_dir():
        return []
    out = []
    for p in sorted(out_dir.glob("*.md"), reverse=True):
        out.append({"name": p.name, "size": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime)
                    .strftime("%Y-%m-%d %H:%M")})
    return out
