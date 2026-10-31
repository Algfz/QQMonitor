"""知识库对外只读 API（供外部机器人 / 自建助手调用）。

设计要点：
- 与网页登录（web.access_token）完全独立：外部系统用 config.yaml 的 ``api.token``，
  请求头 ``Authorization: Bearer <token>`` 或 ``X-Api-Token``。
- ``api.enabled`` 为 false（默认）时接口一律 503，不会意外暴露。
- 只读：不提供写库/改文件能力。
- 检索在 embedding 服务不可用时会自动降级为纯关键词检索（返回 mode="keyword"），
  保证网络故障时机器人仍能查到资料。
"""

from __future__ import annotations

import hmac
import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from .llm import retriever as rag
from .llm.client import LLMError, build_client
from .llm.prompts import build_user_profile

API_VERSION = "1"

_RATE: dict[str, deque[float]] = defaultdict(deque)
_GID_RE = re.compile(r"(?:^|/)(-?\d+)_")


def api_conf(cfg) -> dict:
    """规范化 api 配置。"""
    raw = getattr(cfg, "api", {}) or {}

    def _int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    rate = _int(raw.get("rate_limit_per_min", 30), 30)
    max_k = _int(raw.get("max_k", 12), 12)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "token": str(raw.get("token") or "").strip(),
        "rate_limit": max(1, rate),
        "max_k": max(1, min(max_k, 30)),
    }


def _err(status: int, msg: str):
    """抛一个带 JSON 体的 HTTP 异常（aiohttp 会原样输出 text）。"""
    cls = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized,
           429: web.HTTPTooManyRequests, 503: web.HTTPServiceUnavailable}.get(
               status, web.HTTPBadRequest)
    raise cls(text=json.dumps({"ok": False, "error": msg}, ensure_ascii=False),
              content_type="application/json")


def require(request: web.Request, cfg) -> dict:
    """校验 API 开关、token 与频率；不通过直接抛 HTTP 响应。"""
    conf = api_conf(cfg)
    if not conf["enabled"]:
        _err(503, "知识库 API 未启用（config.yaml 里 api.enabled 设为 true）")
    if not conf["token"]:
        _err(503, "知识库 API 未配置 api.token，拒绝访问")

    auth = request.headers.get("Authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not supplied:
        supplied = (request.headers.get("X-Api-Token") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, conf["token"]):
        _err(401, "token 无效")

    key = f"{request.remote or '?'}:{conf['token'][:8]}"
    now = time.time()
    bucket = _RATE[key]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= conf["rate_limit"]:
        _err(429, f"请求过于频繁（上限 {conf['rate_limit']} 次/分钟）")
    bucket.append(now)
    return conf


def group_of_source(source: str) -> int | None:
    """从 ``chat/<群号>_...md`` 解析群号；文档/文字稿返回 None。"""
    m = _GID_RE.search(source or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _clip(text: str, limit: int = 900) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _keyword_hits(db, cfg, query: str, top_k: int,
                  date_from: str = "", date_to: str = "") -> list[dict]:
    """关键词检索（带重叠去重），返回结构与 rag.retrieve_keyword 一致。"""
    return rag.retrieve_keyword(db, cfg, query, top_k=top_k,
                                date_from=date_from, date_to=date_to)


def search(db, cfg, query: str, k: int = 8,
           groups: list[int] | None = None,
           date_from: str = "", date_to: str = "",
           include_documents: bool = True) -> dict:
    """检索知识库，返回 ``{mode, warning, hits}``。"""
    query = (query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    conf = api_conf(cfg)
    k = max(1, min(int(k or 8), conf["max_k"]))

    mode, warning = "hybrid", ""
    rows: list[dict]
    try:
        client = build_client(cfg, "embedding")
        rows = rag.retrieve(db, cfg, client, query,
                            top_k=max(k * 4, 16),
                            date_from=date_from, date_to=date_to)
    except LLMError as e:
        # 向量服务不可用：降级为关键词检索，机器人仍能回答
        mode, warning = "keyword", f"向量检索不可用（{e}），已降级为关键词检索"
        rows = _keyword_hits(db, cfg, query, top_k=max(k * 4, 16),
                             date_from=date_from, date_to=date_to)

    hits: list[dict] = []
    for r in rows:
        gid = group_of_source(r.get("source", ""))
        if groups:
            if gid is None:
                if not include_documents:
                    continue
            elif gid not in groups:
                continue
        elif gid is None and not include_documents:
            continue
        hits.append({
            "source": r.get("source"),
            "section": r.get("section"),
            "day": r.get("day"),
            "score": round(float(r.get("score") or 0.0), 4),
            "group_id": gid,
            "text": _clip(r.get("text") or ""),
        })
        if len(hits) >= k:
            break
    return {"mode": mode, "warning": warning, "hits": hits}


def ask(db, cfg, question: str, k: int = 8,
        groups: list[int] | None = None,
        date_from: str = "", date_to: str = "",
        include_documents: bool = True) -> dict:
    """检索 + 生成答案，返回 ``{answer, sources, mode, warning}``。"""
    question = (question or "").strip()
    if not question:
        raise ValueError("question 不能为空")
    found = search(db, cfg, question, k=k, groups=groups,
                   date_from=date_from, date_to=date_to,
                   include_documents=include_documents)
    hits = found["hits"]
    if not hits:
        return {
            "answer": "知识库里没有检索到和这个问题相关的内容。",
            "sources": [],
            "mode": found["mode"],
            "warning": found["warning"],
        }

    blocks = []
    for i, h in enumerate(hits, 1):
        day = h.get("day") or "日期未知"
        blocks.append(f"[{i}] 来源：{h.get('source')}（{day}）\n{h.get('text')}")
    context = "\n\n".join(blocks)[:16000]

    system = (
        "你是这个群的知识库助手。"
        "只依据下面提供的资料回答，不要编造资料里没有的信息；"
        "如果资料不足以回答，就直说“知识库里的资料不足以回答这个问题”。"
        "回答用中文，简洁准确，必要时用 [编号] 标注引用的资料。"
    )
    profile = build_user_profile(getattr(cfg, "user", None))
    if profile:
        # 身份卡优先：问「我是谁 / 我们班 / 我们组」时直接用，不要因为资料里没有就说不知道
        system = (
            "【关于提问者】下面这段是你已知的提问者信息，凡是问到提问者本人的身份、班级、职责、\n"
            "场景，直接据此回答，不要因为检索到的资料里没有就回答「不知道」。\n\n"
            + profile + "\n\n" + system
        )
    user = f"资料：\n\n{context}\n\n问题：{question}"
    extra = str((getattr(cfg, "llm", {}) or {}).get("system_extra") or "").strip()
    if extra:
        system += "\n\n【用户自定义规则】\n" + extra
    client = build_client(cfg, "chat")
    answer = client.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        temperature=0.2, max_tokens=1200)
    sources = [{"id": i + 1, "source": h.get("source"), "day": h.get("day"),
                "group_id": h.get("group_id")}
               for i, h in enumerate(hits)]
    return {"answer": answer, "sources": sources,
            "mode": found["mode"], "warning": found["warning"]}


_KIND_LABEL = {"chat": "群聊消息", "chatfile": "群聊记录", "documents": "文档",
               "transcripts": "转写稿", "media": "媒体"}

# 纯占位符的消息（图片/表情/系统提示…）当引用没意义，只留正文
_NOISE_RE = re.compile(r"^\[(?:系统提示|表情包?|图片|文件|语音|视频|回复)[^\]]*\]$")


def _is_noise(quote: str) -> bool:
    return bool(_NOISE_RE.match((quote or "").strip()))


def citations(db, sources: list, limit: int = 20) -> list[dict]:
    """把工具用的原始 sources 规整成对外可读的引用条目。

    每条都带 ``quote``（原文片段）与 ``source``（知识库相对路径），
    外部系统可据此调 ``kb_read_document`` 继续深挖；群聊条目还带
    ``msg_id`` / ``ref``，可直接跳回原文。
    """
    out: list[dict] = []
    seen: set = set()
    # 同一份文件被多次命中时不该挤掉别的来源；群聊消息按群限量。
    per_file: dict[str, int] = {}
    per_group: dict[int, int] = {}
    MAX_PER_FILE, MAX_PER_GROUP = 3, 6
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        kind = str(s.get("kind") or "")
        if not kind:
            src0 = str(s.get("source") or "")
            kind = src0.split("/", 1)[0] if "/" in src0 else "documents"
        key = (kind, s.get("source") or "", s.get("section") or "",
               s.get("msg_id") or 0, s.get("day") or "")
        if key in seen:
            continue
        if kind == "chat":
            gkey = int(s.get("group_id") or 0)
            if per_group.get(gkey, 0) >= MAX_PER_GROUP:
                continue
            per_group[gkey] = per_group.get(gkey, 0) + 1
        else:
            fkey = str(s.get("source") or "")
            if per_file.get(fkey, 0) >= MAX_PER_FILE:
                continue
            per_file[fkey] = per_file.get(fkey, 0) + 1
        seen.add(key)
        item: dict[str, Any] = {"id": len(out) + 1, "kind": kind,
                                "kind_label": _KIND_LABEL.get(kind, kind)}
        if kind == "chat":
            gid = int(s.get("group_id") or 0)
            name = ""
            if gid:
                try:
                    grp = db.get_group(gid)
                    name = str((grp or {}).get("name") or "")
                except Exception:  # noqa: BLE001
                    name = ""
            day = str(s.get("day") or "")
            item.update({"title": (name or f"会话 {gid}") + (f" · {day}" if day else ""),
                         "group_id": gid, "group_name": name, "day": day,
                         "msg_id": int(s.get("msg_id") or 0)})
            if item["msg_id"]:
                item["ref"] = f"/msg/{item['msg_id']}"
        else:
            src = str(s.get("source") or "")
            item.update({"title": src.rsplit("/", 1)[-1] or src, "source": src,
                         "section": str(s.get("section") or ""),
                         "day": str(s.get("day") or "")})
        quote = str(s.get("quote") or "").strip()
        if quote:
            item["quote"] = quote
        # 纯占位符消息（[图片]/[表情]/[系统提示]…）不当作引用材料
        if kind == "chat" and _is_noise(quote):
            per_group[int(s.get("group_id") or 0)] -= 1
            continue
        out.append(item)
    # 具体消息最精确（能直接回查原文），排在前面；其余按命中顺序。
    order = {"chat": 0}
    out.sort(key=lambda it: order.get(it.get("kind"), 1))
    out = out[:max(1, limit)]
    for i, it in enumerate(out, 1):
        it["id"] = i
    return out


def ask_agent(db, cfg, question: str, *, history: list[dict] | None = None,
              limit: int = 20) -> dict:
    """用网页端同一个 Agent（会自己多轮调工具）回答问题，并给出引用材料。

    与 :func:`ask` 的区别：``ask`` 是「检索一次 + 总结一次」的快路径；
    这里跑的是 ``app.llm.agent.Agent``——模型可以自己决定查什么地方、
    翻多少页、要不要读原文，回答质量和可靠性明显更高（代价是慢）。
    返回 ``{answer, citations, tools, model, fallback, elapsed_ms}``。
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("question 不能为空")
    from .llm.agent import Agent

    t0 = time.monotonic()
    agent = Agent(db, cfg)
    res = agent.run(question, history or None)
    return {
        "answer": res.get("text") or "",
        "citations": citations(db, res.get("sources") or [], limit=limit),
        "tools": res.get("tools") or [],
        "model": res.get("model") or "",
        "fallback": bool(res.get("fallback")),
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }



def meta(db, cfg) -> dict:
    """给外部系统做联调用的元信息。"""
    stats = db.kb_stats()
    chunks = db.kb_all_chunks()
    latest = max((int(c.get("mtime") or 0) for c in chunks), default=0)
    groups = [{"group_id": g.get("group_id"), "name": g.get("name")}
              for g in db.get_groups()]
    return {
        "api_version": API_VERSION,
        "chunks": stats.get("chunks", 0),
        "files": stats.get("files", 0),
        "with_vector": stats.get("with_vector", 0),
        "by_section": stats.get("by_section", {}),
        "dims": stats.get("dims", []),
        "latest_mtime": latest,
        "groups": groups,
    }


# ---------------- 供 MCP 暴露的其余只读能力 ----------------

_TEXT_EXTS = {".md", ".txt", ".csv", ".json", ".srt", ".vtt", ".ass"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day(ts: Any) -> str:
    try:
        n = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return datetime.fromtimestamp(n).strftime("%Y-%m-%d")


def _norm_date(value: Any) -> str:
    s = str(value or "").strip()
    return s if _DATE_RE.match(s) else ""


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def _resolve_group(db, raw: Any) -> tuple[int | None, str]:
    """把群名/群号解析成 group_id；返回 (gid, 错误说明)。"""
    if raw in (None, ""):
        return None, ""
    text = str(raw).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text), ""
    groups = db.get_groups()
    lowered = text.casefold()
    exact = [g for g in groups if str(g.get("name") or "").casefold() == lowered]
    if len(exact) == 1:
        return int(exact[0]["group_id"]), ""
    if len(exact) > 1:
        return None, _ambiguity(exact)
    part = [g for g in groups if lowered in str(g.get("name") or "").casefold()]
    if len(part) == 1:
        return int(part[0]["group_id"]), ""
    if len(part) > 1:
        return None, _ambiguity(part)
    return None, f"没有找到名字包含「{text}」的会话，可用 /api/kb/meta 查看清单"


def _ambiguity(groups: list[dict]) -> str:
    cand = "、".join(f"{g.get('name')}（{g.get('group_id')}）" for g in groups[:8])
    return f"匹配到多个会话：{cand}，请改用 group_id"


def overview(db, cfg, detail: str = "brief") -> dict:
    """知识库总体情况。"""
    stats = db.get_stats()
    kb = db.kb_stats()
    groups = db.get_groups()
    first, last = db.message_span()
    n_groups = sum(1 for g in groups if int(g.get("group_id") or 0) > 0)
    data = {
        "conversations": len(groups),
        "groups": n_groups,
        "buddies": len(groups) - n_groups,
        "messages": stats.get("messages", 0),
        "important": stats.get("important", 0),
        "members": stats.get("members", 0),
        "first_day": _day(first),
        "last_day": _day(last),
        "kb": {
            "files": kb.get("files", 0),
            "chunks": kb.get("chunks", 0),
            "with_vector": kb.get("with_vector", 0),
            "by_section": kb.get("by_section", {}),
            "dims": kb.get("dims", []),
        },
        "index_model": db.get_meta("index_model_sig") or "",
    }
    if str(detail).lower() in ("full", "all", "complete"):
        data["top_conversations"] = [
            {
                "group_id": r.get("group_id"),
                "name": r.get("name"),
                "kind": "group" if int(r.get("group_id") or 0) > 0 else "buddy",
                "messages": r.get("msg_count", 0),
                "last_day": _day(r.get("last_ts")),
            }
            for r in (stats.get("group_ranking") or [])
        ]
    return data


def files(db, cfg, section: str = "", limit: int = 20, offset: int = 0) -> dict:
    """知识库文件清单（分页）。"""
    limit = _clamp_int(limit, 20, 1, 200)
    offset = _clamp_int(offset, 0, 0, 10 ** 7)
    section = (section or "").strip()
    rows = db.kb_source_index()
    if section:
        rows = [r for r in rows if (r.get("section") or "") == section]
    total = len(rows)
    page = rows[offset:offset + limit]
    out = [{
        "source": r.get("source"),
        "section": r.get("section"),
        "chunks": r.get("n", 0),
        "chars": r.get("chars", 0),
        "day_from": str(r.get("d1") or ""),
        "day_to": str(r.get("d2") or ""),
    } for r in page]
    return {
        "section": section,
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(out),
        "has_more": offset + len(out) < total,
        "files": out,
    }


def read_document(db, cfg, source: str, keyword: str = "",
                  offset: int = 0, max_chars: int = 20000) -> dict:
    """读取知识库目录内的源文件（可关键词取段落、可续读）。"""
    source = str(source or "").replace("\\", "/").strip().lstrip("/")
    if not source or ".." in source.split("/"):
        raise ValueError("source 不合法")
    root = Path(cfg.resolve(cfg.knowledge.get("dir") or "data/knowledge")).resolve()
    path = (root / source).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError("source 越出知识库目录")
    if not path.is_file():
        raise ValueError(f"文件不存在：{source}")
    if path.suffix.lower() not in _TEXT_EXTS:
        raise ValueError(f"该文件不是可读文本（{path.suffix or '无扩展名'}），"
                         "PDF/Word 请读已提取到 transcripts/ 的文字稿")
    max_chars = _clamp_int(max_chars, 20000, 1000, 60000)
    offset = _clamp_int(offset, 0, 0, 10 ** 8)
    text = path.read_text(encoding="utf-8", errors="replace")
    body = text
    note = ""
    if keyword:
        paras = [p for p in re.split(r"\n\s*\n", text)
                 if keyword.casefold() in p.casefold()]
        if paras:
            body = "\n\n".join(paras)
            note = f"已按关键词「{keyword}」筛出 {len(paras)} 段"
        else:
            note = f"没有包含「{keyword}」的段落，返回全文开头"
    total = len(body)
    start = min(offset, total)
    piece = body[start:start + max_chars]
    nxt = start + len(piece)
    return {
        "source": source,
        "section": source.split("/", 1)[0],
        "total_chars": total,
        "offset": start,
        "returned_chars": len(piece),
        "next_offset": nxt if nxt < total else None,
        "has_more": nxt < total,
        "note": note,
        "text": piece,
    }


def chat_messages(db, cfg, *, keyword: str = "", group: Any = None,
                  group_id: Any = None, user_id: Any = None,
                  important_only: bool = False, date_from: Any = "",
                  date_to: Any = "", limit: int = 20, offset: int = 0) -> dict:
    """按条件查群消息原文（只读）。"""
    limit = _clamp_int(limit, 20, 1, 200)
    offset = _clamp_int(offset, 0, 0, 10 ** 7)
    gid: int | None = None
    if group_id not in (None, ""):
        try:
            gid = int(group_id)
        except (TypeError, ValueError):
            raise ValueError("group_id 必须是整数")
    elif group not in (None, ""):
        gid, err = _resolve_group(db, group)
        if err:
            raise ValueError(err)
    uid: int | None = None
    if user_id not in (None, ""):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            raise ValueError("user_id 必须是整数")
    d_from = _norm_date(date_from)
    d_to = _norm_date(date_to)

    total = db.count_messages(group_id=gid, keyword=keyword, user_id=uid,
                              important_only=bool(important_only),
                              date_from=d_from or None, date_to=d_to or None)
    rows = db.query_messages(group_id=gid, keyword=keyword, user_id=uid,
                             important_only=bool(important_only),
                             date_from=d_from or None, date_to=d_to or None,
                             limit=limit, offset=offset)
    names = {int(g.get("group_id") or 0): g.get("name") for g in db.get_groups()}
    msgs = []
    for m in reversed(rows):
        mgid = int(m.get("group_id") or 0)
        msgs.append({
            "message_id": m.get("message_id"),
            "group_id": mgid,
            "group_name": names.get(mgid) or "",
            "user_id": m.get("user_id"),
            "nickname": m.get("nickname") or "",
            "ts": m.get("ts"),
            "day": _day(m.get("ts")),
            "msg_type": m.get("msg_type") or "text",
            "important": bool(m.get("important")),
            "important_reason": m.get("important_reason") or "",
            "content": _clip(m.get("content") or "", 2000),
        })
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(msgs),
        "has_more": offset + len(msgs) < total,
        "group_id": gid,
        "messages": msgs,
    }
