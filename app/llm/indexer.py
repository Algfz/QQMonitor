"""知识库索引：把 Markdown 切成块 → 生成向量 → 存入 SQLite。

分块策略按内容类型差异化（这直接影响检索质量）：
    chat/*.md        按 `## YYYY-MM-DD` 切分 —— 聊天自带日期结构，
                     按天切能精确定位「上周三说了什么」，机械切块会切断上下文
    transcripts/*.md 按段落累积到 chunk_size，段落间重叠 80 字避免切断语义
    documents/*.md   同上（文档提取产物也是 Markdown）

增量更新：以文件 mtime 为准，只重新 embedding 变动过的文件。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import Settings
from ..db import Database
from .client import LLMClient, LLMError
from .kbmeta import infer_event_date, meeting_date_map

log = logging.getLogger("llm.indexer")

# 可索引的文本扩展名（二进制原件如 PDF 本身不索引，只索引提取出的 .md）
# 注意：不要把 .json 加回来。transcripts/*_转写.json 与 *_日程.json 是转写中间产物，
# 正文与同名 .md 完全重复，且带 start_ms/end_ms/confidence/tier 等噪音字段：
# 曾占向量库 8154/11860 段（68.8%），检索命中后进上下文的是一堆坐标与浮点数。
INDEXABLE_EXTS = {".md", ".txt", ".srt", ".vtt", ".ass", ".csv"}

_DAY_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
_HEAD_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)

# 段落间重叠字数：避免一句话被切在两个块中间
_OVERLAP = 80


def _pack(vec: list[float]) -> tuple[bytes, int]:
    arr = np.asarray(vec, dtype=np.float32)
    return arr.tobytes(), int(arr.size)


_MAX_CHUNK_CHARS = 1200


def _split_long(text: str, limit: int = _MAX_CHUNK_CHARS) -> list[str]:
    """把超长文本切成若干段。

    embedding 服务对单条输入有 token 上限，一个几千字的聊天日块直接送过去
    会整批 400，旧实现会让整个文件（甚至整轮）索引失败。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 <= limit:
            buf = f"{buf}\n{line}" if buf else line
            continue
        if buf:
            out.append(buf.strip())
            buf = ""
        while len(line) > limit:
            out.append(line[:limit].strip())
            line = line[limit:]
        buf = line
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


def chunk_chat(text: str) -> list[tuple[str | None, str]]:
    """聊天 Markdown 按天切分。返回 [(day, content)]。"""
    matches = list(_DAY_RE.finditer(text))
    if not matches:
        body = text.strip()
        return [(None, b) for b in _split_long(body)]
    # 首个日期之前的内容（标题等）单独成块
    out: list[tuple[str | None, str]] = []
    head = text[:matches[0].start()].strip()
    if head:
        out.extend((None, b) for b in _split_long(head))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if body:
            out.extend((m.group(1), b) for b in _split_long(body))
    return out


def chunk_document(text: str, size: int) -> list[tuple[str | None, str]]:
    """普通文档按段落累积切分，带重叠。"""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paras:
        body = text.strip()
        return [(None, b) for b in _split_long(body)]
    out: list[tuple[str | None, str]] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) > size and buf:
            out.extend((None, b) for b in _split_long(buf))
            # 保留尾部作为下一块的开头，避免切断语义
            buf = buf[-_OVERLAP:] + "\n\n" + p if _OVERLAP < len(buf) else p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf.strip():
        out.extend((None, b) for b in _split_long(buf))
    return out


def iter_index_files(kdir: Path, index_chat: bool) -> list[tuple[Path, str]]:
    """列出待索引文件。返回 [(绝对路径, 分区名)]。"""
    out: list[tuple[Path, str]] = []
    sections = ["documents", "transcripts"]
    if index_chat:
        sections.insert(0, "chat")
    for sec in sections:
        d = kdir / sec
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in INDEXABLE_EXTS:
                out.append((p, sec))
    return out


class Indexer:
    def __init__(self, db: Database, cfg: Settings, client: LLMClient | None = None):
        self.db = db
        self.cfg = cfg
        # 延迟创建客户端：没配 API 时也能跑（比如只做清空索引）
        self._client = client

    @property
    def kdir(self) -> Path:
        return self.cfg.resolve(self.cfg.knowledge["dir"])

    def _get_client(self) -> LLMClient:
        if self._client is None:
            from .client import build_client
            self._client = build_client(self.cfg, "embedding")
        return self._client

    def build(self, force: bool = False,
              progress: Callable[[int, int, str], None] | None = None
              ) -> dict:
        """构建/更新索引。

        force=True 时忽略 mtime 全量重建（换 embedding 模型后必须这么做，
        否则新旧向量维度/语义空间不一致）。
        """
        rag = (self.cfg.llm.get("rag") or {})
        size = int(rag.get("chunk_size") or 600)
        index_chat = bool(rag.get("index_chat", True))

        files = iter_index_files(self.kdir, index_chat)
        if not files:
            return {"files": 0, "chunks": 0, "skipped": 0, "added": 0,
                    "errors": 0}

        known = {} if force else self.db.kb_indexed_sources()
        if force:
            # 换模型/维度后旧向量不可用，先整体清空再重建（失败也不留混合维度）
            self.db.kb_clear()
        # force 时 known 被清空，但「清理已删文件残留」仍要用全量已索引源
        existing = self.db.kb_indexed_sources()
        todo: list[tuple[Path, str]] = []
        skipped = 0
        for p, sec in files:
            rel = f"{sec}/{p.name}"
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if not force and rel in known and abs(known[rel] - mt) < 1e-6:
                skipped += 1
                continue
            todo.append((p, sec))

        # 清理已删除文件的残留块与元数据
        alive = {f"{sec}/{p.name}" for p, sec in files}
        for src in list(existing):
            if src not in alive:
                self.db.kb_delete_source(src)
        for src in list(self.db.kb_all_meta()):
            if src not in alive:
                self.db.kb_del_meta(src)

        if not todo:
            return {"files": len(files), "chunks": self.db.kb_stats()["chunks"],
                    "skipped": skipped, "added": 0, "errors": 0}

        client = self._get_client()
        # 会议 held_date 供转写稿推断日期（transcripts 与 meetings 表无外键关联，靠标题匹配）
        meeting_map = meeting_date_map(self.db)
        added = 0
        failed = 0
        total = len(todo)
        for i, (p, sec) in enumerate(todo, 1):
            if progress:
                progress(i, total, p.name)
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                log.warning("读取失败跳过 %s：%s", p.name, e)
                continue

            if sec == "chat":
                # 聊天自带 ## 日期 分节，逐块提取，最准确
                parts = chunk_chat(raw)
            else:
                parts = chunk_document(raw, size)
                # 文档/转写稿无内建日期，用事件日期元数据（同一文件所有块共享）
                rel = f"{sec}/{p.name}"
                meta = self.db.kb_get_meta(rel)
                ev_date = meta.get("event_date")
                if not ev_date:
                    ev_date, _src = infer_event_date(p, raw, meeting_map)
                    # 自动推断值落库，供用户在知识库页查看与修正
                    self.db.kb_set_meta(rel, ev_date, note="", auto=True)
                parts = [(ev_date, body) for _d, body in parts]
            if not parts:
                self.db.kb_delete_source(f"{sec}/{p.name}")
                continue

            texts = [t for _, t in parts]
            try:
                vecs = client.embed(texts)
            except LLMError as e:
                # 单个文件失败不能拖垮整轮：其余文件继续索引，
                # 失败数回传给 pipeline 触发脏标记重试。
                log.error("embedding 失败（%s），跳过该文件（其余继续）：%s",
                          p.name, e)
                failed += 1
                continue
            if len(vecs) != len(texts) or not vecs or any(not v for v in vecs):
                log.warning("向量数量/内容异常，跳过 %s", p.name)
                failed += 1
                continue
            # 维度一致性：同一轮里出现不同维度说明服务端换了模型，
            # 写进去会让检索侧静默跳过这些块（页面却显示"已向量化"）。
            dims = {len(v) for v in vecs if v}
            if len(dims) != 1:
                log.warning("向量维度不一致 %s，跳过 %s（请用 force 重建索引）",
                            sorted(dims), p.name)
                failed += 1
                continue
            if not self.db.kb_check_dim(dims.pop()):
                log.warning("新向量维度与库中已有索引不一致，跳过 %s"
                            "（请用 force 重建索引）", p.name)
                failed += 1
                continue

            mt = p.stat().st_mtime
            chunks = []
            for idx, ((day, body), vec) in enumerate(zip(parts, vecs)):
                blob, dim = _pack(vec)
                chunks.append({
                    "section": sec, "day": day, "chunk_idx": idx,
                    "text": body, "embedding": blob, "dim": dim, "mtime": mt,
                })
            added += self.db.kb_replace_chunks(f"{sec}/{p.name}", chunks)

        return {"files": len(files), "chunks": self.db.kb_stats()["chunks"],
                "skipped": skipped, "added": added, "errors": failed}
