"""知识库检索：向量语义 + 关键词混合，RRF 融合排序。

为什么要混合：
- 向量召回语义相关（问「学费多少」能命中写「收费标准」的片段）
- 关键词保证精确匹配（金额、专有名词、日期不该被语义模糊掉）

两者用 RRF（Reciprocal Rank Fusion）融合——不看原始分数，只看排名，
避免两路分数量纲不同导致一方独大。

注意：本模块依赖 embedding API。API 不可用时会抛出 LLMError，
由上层决定如何提示用户（本项目约定：直接报错，不静默降级）。
"""
from __future__ import annotations

import logging
import re
from typing import Callable

import numpy as np

from .client import LLMClient

log = logging.getLogger("llm.retriever")

# RRF 常数：越大则排名靠后的结果权重衰减越慢
_RRF_K = 60

_CJK = r"\u4e00-\u9fff"


def keywords_of(query: str) -> list[str]:
    """从查询里提取检索词。中文取 2-gram，英文数字整体成词。"""
    toks = re.findall(f"[{_CJK}]+|[A-Za-z0-9]+", query or "")
    out: list[str] = []
    for t in toks:
        if re.match(f"[{_CJK}]", t):
            if len(t) <= 2:
                out.append(t)
            else:
                out.extend(t[i:i + 2] for i in range(len(t) - 1))
        else:
            out.append(t.lower())
    return out or ([query.strip()] if query.strip() else [])


def keyword_scores(query: str, chunks: list[dict]) -> list[float]:
    """简单关键词打分：命中词比例 + 命中次数加权，做归一化。"""
    kws = keywords_of(query)
    if not kws:
        return [0.0] * len(chunks)
    scores = []
    for c in chunks:
        text = c["text"].lower()
        hit = sum(1 for k in kws if k in text)
        # 命中比例为主，次数为辅（避免长文本仅因篇幅长而得分高）
        ratio = hit / len(kws)
        scores.append(ratio + min(hit, 10) * 0.01)
    return scores


def _cosine(q: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    qn = q / (np.linalg.norm(q) + 1e-9)
    mn = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)
    return mn @ qn


def _rrf(rank_lists: list[list[int]], n: int) -> list[float]:
    """Reciprocal Rank Fusion：把多路的排名融合成统一分数。"""
    fused = [0.0] * n
    for ranks in rank_lists:
        for rank, idx in enumerate(ranks, 1):
            fused[idx] += 1.0 / (_RRF_K + rank)
    return fused


def rag_min_sim(cfg) -> float:
    """向量路最低余弦相似度（config: llm.rag.min_similarity，默认 0.20）。

    低于该值且关键词也没命中的块直接丢弃——否则无关内容会占满 top_k。
    """
    try:
        v = float(((cfg.llm.get("rag") or {}).get("min_similarity")
                   if cfg else None) or 0.20)
    except (TypeError, ValueError):
        v = 0.20
    return min(max(v, 0.0), 1.0)


def _filter_by_date(chunks: list[dict], date_from: str = "",
                    date_to: str = "") -> tuple[list[dict], list[int]]:
    """按事件日期过滤片段。

    返回 (过滤后的片段, 原始索引列表)。
    无日期的片段（day 为空）在指定时间范围时也会被排除——否则问「今天」
    会混入大量无日期的旧资料。
    """
    if not date_from and not date_to:
        return chunks, list(range(len(chunks)))
    kept: list[dict] = []
    idxs: list[int] = []
    for i, c in enumerate(chunks):
        day = c.get("day") or ""
        if not day:
            continue
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        kept.append(c)
        idxs.append(i)
    return kept, idxs


def _dedupe_head(text: str) -> str:
    """去掉块首的 Markdown 标题行（同一文件同一天的块都以 '## 2026-10-08' 开头），
    只留正文开头，避免把同一天的无关消息误判成重复。"""
    lines = (text or "").strip().splitlines()
    while lines and lines[0].lstrip().startswith("#"):
        lines.pop(0)
    return "\n".join(lines).strip()[:60]


def _dedupe_hits(hits: list[dict]) -> list[dict]:
    """去掉同一文件同一天内内容高度重合的片段。

    索引按固定字数滑窗切块，相邻块常有大量重叠；检索命中相邻块时，
    上下文里会出现两段几乎一样的文字（还重复计入引用），白白吃掉预算。
    判定：同 source + 同 day + 正文开头 20 字相同（或整段互相包含）。
    """
    out: list[dict] = []
    for h in hits:
        head = _dedupe_head(h.get("text") or "")
        dup = False
        for k in out:
            if k.get("source") != h.get("source"):
                continue
            if (k.get("day") or "") != (h.get("day") or ""):
                continue
            ktxt = (k.get("text") or "").strip()
            khead = _dedupe_head(ktxt)
            if not head or not khead:
                continue
            if head in ktxt or khead in (h.get("text") or ""):
                dup = True
                break
            common = 0
            for a, b in zip(head, khead):
                if a != b:
                    break
                common += 1
            if common >= 20:
                dup = True
                break
        if not dup:
            out.append(h)
    return out


def retrieve(db, cfg, client: LLMClient, query: str, top_k: int = 6,
             date_from: str = "", date_to: str = "") -> list[dict]:
    """检索与 query 最相关的片段。

    返回 [{source, section, day, text, score}]，按相关度降序。
    指定 date_from/date_to 时先按事件日期过滤再检索。
    """
    chunks = db.kb_all_chunks()
    if not chunks:
        return []

    # 时间过滤（在向量检索之前做，避免无谓计算）
    chunks, _ = _filter_by_date(chunks, date_from, date_to)
    if not chunks:
        return []
    # ---- 向量路 ----
    vec_scores: list[float] | None = None
    try:
        qvecs = client.embed([query])
        q = np.asarray(qvecs[0], dtype=np.float32)
    except Exception as e:
        # embedding 失败按约定直接上抛，不静默降级
        raise
    if q.size:
        mats, valid = [], []
        for i, c in enumerate(chunks):
            blob = c.get("embedding")
            if not blob:
                continue
            v = np.frombuffer(blob, dtype=np.float32)
            # 维度不一致（换过 embedding 模型但没重建索引）时跳过该块
            if v.size != q.size:
                continue
            mats.append(v)
            valid.append(i)
        if mats:
            sims = _cosine(q, np.vstack(mats))
            vec_scores = [0.0] * len(chunks)
            for i, s in zip(valid, sims):
                vec_scores[i] = float(s)

    # ---- 关键词路 ----
    kw = keyword_scores(query, chunks)

    # ---- 融合 ----
    # 候选门槛：两路都没打中的块不参与融合。
    # 旧实现对全部块做 RRF，而 RRF 给每一块都记正分（第 1 名 1/61、最后一名也有分），
    # 于是「两路都没命中不送上下文」的过滤永远不生效，top_k 总是塞满无关内容。
    min_sim = float(rag_min_sim(cfg))
    candidates = [
        i for i in range(len(chunks))
        if kw[i] > 0 or (vec_scores is not None and vec_scores[i] >= min_sim)
    ]
    if not candidates:
        return []

    rank_lists: list[list[int]] = []
    if vec_scores is not None:
        rank_lists.append(sorted(candidates,
                                 key=lambda i: vec_scores[i], reverse=True))
    rank_lists.append(sorted(candidates, key=lambda i: kw[i], reverse=True))
    fused = _rrf(rank_lists, len(chunks))

    order = sorted(candidates, key=lambda i: fused[i], reverse=True)
    out = []
    for i in order:
        out.append({
            "source": chunks[i]["source"],
            "section": chunks[i]["section"],
            "day": chunks[i].get("day"),
            "text": chunks[i]["text"],
            "score": round(fused[i], 5),
            "kw_hit": round(kw[i], 4),
        })
    return _dedupe_hits(out)[:top_k]


def retrieve_keyword(db, cfg, query: str, top_k: int = 6,
                     date_from: str = "", date_to: str = "") -> list[dict]:
    """纯关键词检索，不需要 embedding。

    给三种场景兜底：embedding 未配置/模型缺失、调用失败、以及外部 API 降级。
    返回结构与 retrieve() 一致，score 为关键词得分（归一化到 0~1 附近）。
    """
    chunks = db.kb_all_chunks()
    if not chunks:
        return []
    chunks, _ = _filter_by_date(chunks, date_from, date_to)
    if not chunks:
        return []
    kw = keyword_scores(query, chunks)
    pairs = [(i, s) for i, s in enumerate(kw) if s > 0]
    if not pairs:
        return []
    pairs.sort(key=lambda p: p[1], reverse=True)
    peak = pairs[0][1] or 1.0
    out = []
    for i, s in pairs:
        out.append({
            "source": chunks[i]["source"],
            "section": chunks[i]["section"],
            "day": chunks[i].get("day"),
            "text": chunks[i]["text"],
            "score": round(float(s) / peak, 5),
            "kw_hit": round(float(s), 4),
        })
    return _dedupe_hits(out)[:top_k]
