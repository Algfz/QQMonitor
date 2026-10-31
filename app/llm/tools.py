"""工具（Function Calling）：把知识库能力交给模型自己调用。

为什么不再后端预判「要不要检索」
--------------------------------
旧版用 `intent.needs_search` 的正则规则判断。规则覆盖不全就会出问题：
用户问「这次活动要交多少钱」，答案是「场地费 + 物料费 + 餐费…」分散在
《入学指南》的好几处，一次 top_k=6 的检索只召回其中一小块，模型就只能
回答「资料里只写了医疗统筹费 290 元」。

交给模型之后，它可以：
  - 自己决定查不查：「你好」直接回答，「学费多少」去查资料
  - 一次查不全就换检索词多查几次（学费 / 扣款 / 住宿费 / 教材费…）
  - 定位到具体文件后用 read_document 通读全文，不再受分块截断影响
代价只是多几轮 API 往返，远比答错一次划算。

工具返回值即喂回模型的文本，因此自带结构（编号 / 来源 / 事件日期），
模型才能按 [1] [2] 引用；同时单独返回 sources 供前端展示引用来源。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .client import LLMClient, LLMError, build_client, llm_ready
from .prompts import build_context

log = logging.getLogger("llm.tools")

# 工具展示名（前端用）
LABELS = {
    "search_knowledge": "检索知识库",
    "read_document": "读取文档",
    "list_knowledge": "列出知识库文件",
    "kb_overview": "知识库概览",
    "search_chat": "搜索群消息",
    "recent_chat": "查看最近群消息",
    "list_schedule": "查看日历日程",
}


# ---------------- 工具 schema（OpenAI function calling 格式） ----------------
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "在个人知识库（QQ 群聊天记录、群文件与通知附件、会议/录音转写稿、图片 OCR 文字）中"
                "做语义 + 关键词混合检索，返回最相关的若干片段。"
                "凡是涉及群里的事务、通知、文件内容、金额/日期/地点等"
                "具体事实的问题，都应该先调用本工具查证，不要凭印象回答。"
                "一次查不全可以换检索词再查几次（同义词、简称、把长问题拆成几个关键词）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": ("检索词。用问题里的核心名词或短句，"
                                        "多个词用空格分隔；不要写完整长句。"),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": ("返回片段数，默认 12。信息分散在多个主题时"
                                        "可调大到 20~30。"),
                    },
                    "date_from": {
                        "type": "string",
                        "description": ("只保留事件日期 >= 该日期的片段，格式 YYYY-MM-DD。"
                                        "用户问「今天/本周/最近」时用。"),
                    },
                    "date_to": {
                        "type": "string",
                        "description": "只保留事件日期 <= 该日期的片段，格式 YYYY-MM-DD。",
                    },
                    "group": {
                        "type": "string",
                        "description": ("可选，只在某个群的群聊内容里检索（按群名模糊匹配，"
                                        "如「班级群」）。文档/转写稿默认仍会参与，"
                                        "可用 include_documents=false 关掉。"),
                    },
                    "group_id": {
                        "type": "integer",
                        "description": "可选，直接用群号限定范围（优先于 group）。",
                    },
                    "include_documents": {
                        "type": "boolean",
                        "description": ("限定群时是否仍检索文档/转写稿，默认 true；"
                                        "只想看群聊原话时设为 false。"),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "读取知识库中某个文件的全部内容（按原文顺序拼接）。"
                "适用于：答案可能分散在一份长文档多处（如《入学指南》里学费、住宿费、"
                "教材费分散在不同章节），检索片段拼不出完整答案时。"
                "先用 list_knowledge 确认文件名，再调用本工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": ("文件路径，如 documents/入群须知.md。"
                                        "也可只写文件名或文件名的一部分，会自动匹配。"),
                    },
                    "keyword": {
                        "type": "string",
                        "description": ("可选。只返回包含该关键词的片段，用于快速定位长文档中的"
                                        "相关章节；留空则返回全文。"),
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "本次返回的最大字符数，默认 60000。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": ("从文件第几个字符开始读，默认 0。内容太长被截断时，"
                                        "按返回里的提示用 offset 续读。"),
                    },
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_knowledge",
            "description": (
                "列出知识库里已索引的文件：路径、分区（chat/documents/transcripts）、"
                "片段数、事件日期范围。用于判断该读哪份文件，或确认某类资料是否存在。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": ("可选，只列出某个分区：chat（群聊）/ documents"
                                        "（文档）/ transcripts（会议转写）。留空列全部。"),
                    },
                    "limit": {"type": "integer",
                              "description": "本次最多列出几个文件，默认 20，最大 200。"},
                    "offset": {"type": "integer",
                               "description": "从第几个开始（0 起），用于翻页看后面的文件。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_overview",
            "description": (
                "一眼看清整个知识库的规模与构成：有多少会话、多少条消息、"
                "重要消息数、时间跨度、知识库文件/片段数、各分区块数、"
                "哪些群消息最多、最近一条消息是什么时候。"
                "用户问「知识库里有什么」「数据全不全」「最近同步到哪天了」时先用它，"
                "再决定要不要深入检索。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "detail": {
                        "type": "string",
                        "description": ("简略（brief，默认）或完整（full）。"
                                        "full 会额外列出消息最多的会话与各分区文件数。"),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_chat",
            "description": (
                "直接在消息数据库里按条件精确搜索群聊原文（不经过向量索引），"
                "返回逐条消息及发言人、时间，并给出符合条件**真实总条数**。"
                "可按群名/群号、发言人、日期、是否重要筛选；结果过长会自动截断，"
                "用 offset 翻页继续看。适合查某句话是谁说的、某次通知的原文，"
                "以及索引里没覆盖到的最新消息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "要匹配的消息文本关键词。",
                    },
                    "group": {
                        "type": "string",
                        "description": ("可选，按会话名筛选，如「班级群」"
                                        "「学院通知群」；会按名称模糊匹配，"
                                        "匹配不到时返回候选清单。"),
                    },
                    "group_id": {
                        "type": "integer",
                        "description": "可选，直接用群号/会话号筛选（优先于 group）。",
                    },
                    "user_id": {
                        "type": "integer",
                        "description": "可选，只看某个发言人（QQ 号）。",
                    },
                    "important_only": {
                        "type": "boolean",
                        "description": "可选，只看被标记为「重要」的消息（@全体、通知等）。",
                    },
                    "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD。"},
                    "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD。"},
                    "limit": {
                        "type": "integer",
                        "description": "本页返回条数，默认 100，最大 300。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "跳过前 N 条，用于翻页/续查，默认 0。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_chat",
            "description": (
                "按时间顺序看最近的群聊消息（不需要关键词）。用于：「最近群里在聊什么」"
                "「今天有什么通知」「某群最近有没有人提某件事」（配合 date_from/date_to）。"
                "问「今天/明天有什么安排」时 date_from 建议取到昨天——群里的通知常在前一晚发出。"
                "可按群名/群号、是否重要筛选，并给出符合条件的总条数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": "可选，只看某个会话（按群名模糊匹配，如「项目组」）。",
                    },
                    "group_id": {
                        "type": "integer",
                        "description": "可选，直接用群号/会话号（优先于 group）。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回最近多少条，默认 40，最大 200。",
                    },
                    "important_only": {
                        "type": "boolean",
                        "description": "可选，只看重要消息（@全体、通知等）。",
                    },
                    "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD（含）。"},
                    "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD（含）。"},
                    "offset": {
                        "type": "integer",
                        "description": "跳过最近的前 N 条（翻更早的消息），默认 0。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_schedule",
            "description": (
                "查日历里的日程条目（由群聊、群文件、录音转写自动抽取，也可能有手动添加的）。"
                "用于「今天/明天/这周有什么安排」「某天有什么会」这类问题：先看日历，"
                "再用 recent_chat / search_chat 补充群里没被抽成日程的零散消息。"
                "默认返回今天的条目。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string",
                                  "description": "可选，起始日期 YYYY-MM-DD（含）。默认今天。"},
                    "date_to": {"type": "string",
                                "description": "可选，结束日期 YYYY-MM-DD（含）。默认同 date_from。"},
                    "limit": {"type": "integer",
                              "description": "可选，最多返回多少条，默认 60。"},
                },
            },
        },
    },
]


class ToolResult:
    """一次工具调用的结果。"""

    def __init__(self, content: str, summary: str = "", sources: list[dict] | None = None,
                 ok: bool = True):
        self.content = content
        self.summary = summary
        self.sources = sources or []
        self.ok = ok

    def as_dict(self) -> dict:
        return {"content": self.content, "summary": self.summary,
                "sources": self.sources, "ok": self.ok}


def _as_int(v: Any, default: int, lo: int = 1, hi: int = 100000) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _as_str(v: Any) -> str:
    return str(v or "").strip()


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _day_range(d1: str | None, d2: str | None) -> str:
    d1, d2 = d1 or "", d2 or ""
    if d1 and d2 and d1 != d2:
        return f"{d1}~{d2}"
    return d1 or d2 or "无日期"


def _quote(text: Any, limit: int = 200) -> str:
    """把一段原文压成单行引用（给外部系统看「依据是什么」）。"""
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _chat_cite(m: dict, quote: str = "") -> dict:
    """一条消息 → 一条引用（含 message_id，外部系统可据此回查原文）。"""
    return {"kind": "chat",
            "msg_id": int(m.get("id") or 0),
            "group_id": int(m.get("group_id") or 0),
            "day": (m.get("time_str") or "")[:10],
            "quote": _quote(quote or m.get("content"))}


_KIND_OF_SECTION = {"chat": "chatfile", "documents": "documents",
                    "transcripts": "transcripts", "media": "media"}


def _doc_cite(source: str, section: str = "", day: Any = None,
              quote: str = "") -> dict:
    """一个知识库片段 → 一条引用。

    注意 ``chat/xxx.md`` 是**群聊记录的 Markdown 文件**，与一条具体消息
    （``_chat_cite``，带 msg_id）不是一回事，所以用 ``chatfile`` 区分。
    """
    pref = str(source or "").split("/", 1)[0]
    return {"kind": _KIND_OF_SECTION.get(pref, pref or "documents"),
            "source": str(source or ""), "section": str(section or ""),
            "day": day or None, "quote": _quote(quote)}



class ToolRunner:
    """执行工具调用。持有 db / cfg，惰性创建 embedding 客户端。"""

    def __init__(self, db, cfg, emb: LLMClient | None = None):
        self.db = db
        self.cfg = cfg
        self._emb = emb

    # ---- 内部 ----
    @property
    def rag(self) -> dict:
        return self.cfg.llm.get("rag") or {}

    def _embedding(self) -> LLMClient:
        if self._emb is None:
            self._emb = build_client(self.cfg, "embedding")
        return self._emb

    def _resolve_group(self, raw: str) -> tuple[int | None, str]:
        """把用户口述的会话名解析成群号。

        返回 (群号, 错误说明)：错误说明非空表示解析失败（含候选清单），
        群号已给出且合法时错误说明为空。
        """
        want = (raw or "").strip()
        if not want:
            return None, ""
        if want.lstrip("-").isdigit():
            return int(want), ""
        try:
            groups = self.db.get_groups()
        except Exception:
            groups = []
        names = [(int(g.get("group_id") or 0), (g.get("name") or "").strip())
                 for g in groups if int(g.get("group_id") or 0)]
        low = want.casefold()
        exact = [gid for gid, name in names if name.casefold() == low]
        if len(exact) == 1:
            return exact[0], ""
        cands = exact or [gid for gid, name in names if low in name.casefold()]
        if len(cands) == 1:
            return cands[0], ""
        if not cands:
            listing = "\n".join(f"- {gid} {name}" for gid, name in names[:20]) or "（暂无会话）"
            return None, (f"没有找到名字包含「{want}」的会话。可用会话：\n{listing}")
        listing = "\n".join(f"- {gid} {name}" for gid, name in names
                            if gid in cands[:12])
        return None, (f"「{want}」匹配到多个会话，请改用群号或更完整的名字：\n{listing}")

    def _scope_groups(self, args: dict) -> tuple[list[int] | None, str]:
        """解析检索范围：group_id 优先，其次 group 名称；都没有返回 None（全库）。"""
        gid_raw = args.get("group_id")
        if gid_raw not in (None, "", "null"):
            # 注意：好友会话的 gid 是负数，范围必须包含负区
            gid = _as_int(gid_raw, 0, -10 ** 12, 10 ** 12) or 0
            return ([gid] if gid else None), ""
        raw = _as_str(args.get("group"))
        if not raw:
            return None, ""
        gid, err = self._resolve_group(raw)
        if err:
            return None, err
        return [gid], ""

    @staticmethod
    def _source_gid(source: str) -> int | None:
        from ..kbapi import group_of_source
        return group_of_source(source)

    @classmethod
    def _filter_scope(cls, hits: list[dict], groups: list[int] | None,
                      include_documents: bool) -> list[dict]:
        """按群范围过滤检索结果；文档/转写稿（无群号）受 include_documents 控制。"""
        if not groups:
            return hits
        want = set(groups)
        out = []
        for h in hits:
            gid = cls._source_gid(h.get("source") or "")
            if gid is None:
                if include_documents:
                    out.append(h)
            elif gid in want:
                out.append(h)
        return out

    def _read_text_source(self, source: str) -> tuple[str, str, str]:
        """读知识库里的文本源文件（md/txt/…）。

        返回 (文本, 绝对路径, 错误说明)；非文本类型或文件缺失时文本文空。
        """
        exts = (".md", ".txt", ".csv", ".json", ".srt", ".vtt", ".ass")
        if not source.lower().endswith(exts):
            return "", "", "不是文本文件"
        root = self.cfg.resolve(self.cfg.knowledge.get("dir") or "data/knowledge")
        path = (root / source).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return "", "", "路径越界"
        if not path.is_file():
            return "", str(path), "源文件不存在（可能已被删除）"
        try:
            return path.read_text(encoding="utf-8", errors="replace"), str(path), ""
        except OSError as e:
            return "", str(path), f"读取失败：{e}"

    def run(self, name: str, raw_args: Any) -> ToolResult:
        """执行工具。参数解析失败也返回 ToolResult（把错误告诉模型，让它重试）。"""
        args = self._parse_args(raw_args)
        try:
            if name == "search_knowledge":
                return self._search_knowledge(args)
            if name == "read_document":
                return self._read_document(args)
            if name == "list_knowledge":
                return self._list_knowledge(args)
            if name == "kb_overview":
                return self._kb_overview(args)
            if name == "search_chat":
                return self._search_chat(args)
            if name == "recent_chat":
                return self._recent_chat(args)
            if name == "list_schedule":
                return self._list_schedule(args)
        except LLMError as e:
            # 约定：embedding 等 API 失败直接报错，不静默降级
            log.warning("工具 %s 调用失败：%s", name, e)
            return ToolResult(f"工具执行失败：{e}", summary=f"失败：{e}", ok=False)
        except Exception as e:
            log.exception("工具 %s 异常", name)
            return ToolResult(f"工具执行异常：{type(e).__name__}: {e}",
                              summary=f"异常：{e}", ok=False)
        return ToolResult(f"未知工具：{name}。可用工具：{', '.join(LABELS)}。",
                          summary="未知工具", ok=False)

    @staticmethod
    def _parse_args(raw: Any) -> dict:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return {}
            try:
                parsed = json.loads(s)
            except ValueError:
                return {"_raw": s}
            return parsed if isinstance(parsed, dict) else {"_raw": s}
        return {}

    # ---- 工具实现 ----
    def _search_knowledge(self, args: dict) -> ToolResult:
        from .retriever import retrieve, retrieve_keyword

        query = _as_str(args.get("query") or args.get("_raw"))
        if not query:
            return ToolResult("缺少参数 query：请提供检索词。",
                              summary="缺少检索词", ok=False)
        top_k = _as_int(args.get("top_k"), int(self.rag.get("top_k") or 12), 1, 40)
        d_from = _as_str(args.get("date_from"))
        d_to = _as_str(args.get("date_to"))
        max_chars = int(self.rag.get("tool_result_chars") or 30000)
        groups, gerr = self._scope_groups(args)
        if gerr:
            return ToolResult(gerr, summary="群名未能确定", ok=False)
        include_docs = _as_bool(args.get("include_documents", True))
        want_k = top_k * 4 if groups else top_k  # 先多取，过滤后还能凑够条数
        want_k = max(1, min(want_k, 40))

        mode = "hybrid"
        hits: list[dict] = []
        try:
            if not llm_ready(self.cfg, "embedding"):
                raise LLMError("embedding 未就绪")
            hits = retrieve(self.db, self.cfg, self._embedding(), query, want_k,
                            d_from, d_to)
        except Exception as e:
            # 向量检索不可用（模型缺失 / 服务异常）时降级关键词，
            # 至少让模型知道库里有这些资料，而不是整轮报错。
            log.warning("语义检索不可用，降级关键词检索：%s", e)
            mode = "keyword"
            hits = retrieve_keyword(self.db, self.cfg, query, want_k, d_from, d_to)
        if groups:
            hits = self._filter_scope(hits, groups, include_docs)
        hits = hits[:top_k]
        if not hits:
            scope = f"（范围 {d_from}~{d_to}）" if (d_from or d_to) else ""
            where = ""
            if groups:
                names = []
                for g in groups:
                    grp = self.db.get_group(g)
                    names.append(f"{grp['name']}（{g}）" if grp and grp.get("name")
                                 else str(g))
                where = " · 限定 " + "、".join(names)
            return ToolResult(
                f"没有检索到与「{query}」相关的资料{scope}{where}。\n"
                f"建议：换更短的关键词或同义词重试；去掉范围限制；"
                f"或用 list_knowledge 看看知识库里有哪些文件，再用 read_document 通读。",
                summary="未命中", ok=True)

        context, used = build_context(hits, max_chars)
        head = (f"【检索结果】查询「{query}」"
                + (f" · 范围 {d_from}~{d_to}" if (d_from or d_to) else "")
                + (f" · 限定群 {groups}" if groups else "")
                + f" · 命中 {len(used)} 段"
                + ("（向量检索不可用，已降级为关键词检索）" if mode == "keyword" else "")
                + "\n")
        body = head + context
        if len(used) < len(hits):
            body += (f"\n\n（受长度限制只展示了前 {len(used)} 段，"
                     f"可用 keyword 更精确的检索词缩小范围）")
        sources = [_doc_cite(c["source"], c.get("section", ""), c.get("day"),
                             c.get("text") or "") for c in used]
        return ToolResult(body, summary=f"命中 {len(used)} 段", sources=sources)

    def _read_document(self, args: dict) -> ToolResult:
        want = _as_str(args.get("source") or args.get("_raw"))
        if not want:
            return ToolResult("缺少参数 source：请提供文件路径或文件名。",
                              summary="缺少文件名", ok=False)
        rows = self.db.kb_source_index()
        if not rows:
            return ToolResult("知识库还没有任何索引内容，请先到设置页建立索引。",
                              summary="空索引", ok=False)

        # 匹配顺序：全等 → 文件名（basename）全等 → 包含且唯一；多个候选时
        # 不能让模型「随便读一个」，必须把候选清单交回去让它自己挑。
        match = next((r for r in rows if r["source"] == want), None)
        if match is None:
            base = want.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            same = [r for r in rows
                    if r["source"].replace("\\", "/").rsplit("/", 1)[-1].casefold() == base]
            if len(same) == 1:
                match = same[0]
        if match is None:
            cands = [r for r in rows if want.casefold() in r["source"].casefold()]
            if len(cands) == 1:
                match = cands[0]
            elif len(cands) > 1:
                listing = "\n".join(f"- {r['source']}（{r['n']} 段）" for r in cands[:30])
                return ToolResult(
                    f"「{want}」匹配到 {len(cands)} 个文件，请写得更具体（可带目录名）：\n"
                    f"{listing}", summary=f"{len(cands)} 个候选", ok=False)
        if match is None:
            listing = "\n".join(f"- {r['source']}（{r['n']} 段）" for r in rows[:60])
            return ToolResult(
                f"没有找到匹配「{want}」的文件。知识库现有文件：\n{listing}",
                summary="文件不存在", ok=False)

        source = match["source"]
        kw = _as_str(args.get("keyword"))
        max_chars = _as_int(args.get("max_chars"),
                            int(self.rag.get("doc_read_chars") or 60000), 500, 200000)
        offset = _as_int(args.get("offset"), 0, 0, 10 ** 7)

        # 优先读源文件本身（比 kb_chunks 更完整，不受分块/长度切分影响）
        text, path, ferr = self._read_text_source(source)
        if text and not kw:
            total = len(text)
            if offset >= total:
                return ToolResult(
                    f"{source} 共 {total} 字，offset={offset} 已到文件末尾。"
                    f"想看开头请省略 offset 或传 0。", summary="已到末尾", ok=True)
            seg = text[offset:offset + max_chars]
            end = offset + len(seg)
            head = (f"【全文】{source} · 共 {total} 字 · 本次读取第 {offset}~{end} 字"
                    + (f"\n源文件：{path}" if path else "") + "\n\n")
            tail = ("" if end >= total else
                    f"\n\n（后面还有 {total - end} 字，用 offset={end} 继续读）")
            return ToolResult(head + seg + tail, summary=f"读取 {len(seg)} 字",
                              sources=[_doc_cite(source,
                                                 match.get("section", ""),
                                                 None, seg)])

        if text and kw:
            # 关键词 + 源文件：按段落取窗口，比 chunks 更连贯
            paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            picked = [p for p in paras if kw in p]
            if not picked:
                return ToolResult(
                    f"{source} 中没有包含「{kw}」的段落。可去掉 keyword 通读全文。",
                    summary="关键词未命中", ok=True)
            body_parts: list[str] = []
            used = 0
            for p in picked:
                if used + len(p) > max_chars and body_parts:
                    break
                body_parts.append(p)
                used += len(p)
            head = (f"【全文】{source} · 命中关键词「{kw}」的 {len(picked)} 个段落"
                    + f"（共 {len(text)} 字）\n")
            tail = (f"\n\n（受长度限制只展示了 {len(body_parts)} 段；"
                    f"可换更精确的 keyword，或去掉 keyword 用 offset 通读）"
                    if len(body_parts) < len(picked) else "")
            return ToolResult(head + "\n\n---\n\n".join(body_parts) + tail,
                              summary=f"命中 {len(picked)} 段",
                              sources=[_doc_cite(source,
                                                 match.get("section", ""), None,
                                                 body_parts[0] if body_parts else "")])

        # 源文件不可用（被删/非文本）→ 退回 kb_chunks
        chunks = self.db.kb_chunks_of_source(source)
        if not chunks:
            return ToolResult(f"{source} 没有可读取的内容（{ferr or '无片段'}）。",
                              summary="空文件", ok=False)
        if kw:
            picked = [c for c in chunks if kw in (c.get("text") or "")]
            if not picked:
                return ToolResult(
                    f"{source} 中没有包含「{kw}」的片段。可去掉 keyword 通读全文。",
                    summary="关键词未命中", ok=True)
            chunks = picked

        parts: list[str] = []
        total = 0
        cut = False
        for i, c in enumerate(chunks, 1):
            tag = f"[{i}] 来源：{c['source']}"
            if c.get("day"):
                tag += f" · {c['day']}"
            block = f"{tag}\n{(c.get('text') or '').strip()}"
            if total + len(block) > max_chars and parts:
                cut = True
                break
            parts.append(block)
            total += len(block)

        head = (f"【全文】{source} · 共 {len(chunks)} 段"
                + (f"（已按关键词「{kw}」过滤）" if kw else "")
                + (f"\n（源文件不可读：{ferr}，以下是索引片段）" if ferr else "") + "\n")
        tail = ""
        if cut:
            tail = (f"\n\n（内容较长，已截断到 {max_chars} 字；"
                    f"可用 keyword 参数只取相关段落）")
        sources = [_doc_cite(source, match.get("section", ""), c.get("day"),
                             c.get("text") or "")
                   for c in chunks[:len(parts)]]
        return ToolResult(head + "\n\n---\n\n".join(parts) + tail,
                          summary=f"读取 {len(parts)} 段", sources=sources)

    def _recent_chat(self, args: dict) -> ToolResult:
        groups, gerr = self._scope_groups(args)
        if gerr:
            return ToolResult(gerr, summary="群名未能确定", ok=False)
        gid = groups[0] if groups else None
        limit = _as_int(args.get("limit"), 40, 1, 200)
        offset = _as_int(args.get("offset"), 0, 0, 10 ** 6)
        important_only = _as_bool(args.get("important_only"))
        d_from = _as_str(args.get("date_from"))
        d_to = _as_str(args.get("date_to"))

        total = self.db.count_messages(group_id=gid, important_only=important_only,
                                       date_from=d_from or None, date_to=d_to or None)
        rows = self.db.query_messages(group_id=gid, important_only=important_only,
                                      date_from=d_from or None, date_to=d_to or None,
                                      limit=limit, offset=offset)
        if not rows:
            return ToolResult(
                "没有符合条件的消息："
                + ("该会话还没有消息。" if gid else "数据库里还没有消息。"),
                summary="无消息", ok=True)
        rows = list(reversed(rows))

        desc = "全部会话"
        if gid is not None:
            grp = self.db.get_group(gid)
            desc = (f"{grp['name']}（{gid}）" if grp and grp.get("name") else f"会话 {gid}")
        if important_only:
            desc += " · 仅重要"
        if d_from or d_to:
            desc += f" · {d_from}~{d_to}"

        max_chars = int(self.rag.get("chat_result_chars")
                        or self.rag.get("tool_result_chars") or 20000)
        lines: list[str] = []
        used = 0
        last_day = ""
        shown = 0
        picked_rows: list[dict] = []
        for m in rows:
            who = m.get("nickname") or str(m.get("user_id") or "")
            text = (m.get("content") or "").strip().replace("\n", " ")
            if len(text) > 400:
                text = text[:400] + "…"
            line = f"[{m.get('time_str', '')}] {who}：{text}"
            day = (m.get("time_str") or "")[:10]
            prefix = f"\n—— {day} ——\n" if day and day != last_day else ""
            if lines and used + len(prefix) + len(line) > max_chars:
                break
            if prefix:
                lines.append(prefix.strip())
                last_day = day
                used += len(prefix)
            lines.append(line)
            used += len(line)
            shown += 1
            picked_rows.append(m)

        head = (f"【最近消息】{desc} · 共 {total} 条 · 显示 {shown} 条"
                + (f"（跳过前 {offset} 条）" if offset else "") + "\n")
        tail = ""
        if offset + shown < total:
            tail = (f"\n（还有 {total - offset - shown} 条更早的消息，"
                    f"可用 offset={offset + shown} 继续看，或用 search_chat 按关键词查）")
        return ToolResult(head + "\n".join(lines) + tail,
                          summary=f"共 {total} 条，显示 {shown} 条",
                          sources=[_chat_cite(m) for m in picked_rows[:40]])

    def _list_schedule(self, args: dict) -> ToolResult:
        """读日历（schedule_items）：默认今天，可按日期区间取。"""
        from datetime import datetime as _dt

        today = _dt.now().strftime("%Y-%m-%d")
        d_from = _as_str(args.get("date_from")) or today
        d_to = _as_str(args.get("date_to")) or d_from
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        limit = _as_int(args.get("limit"), 60, 1, 500)

        rows = self.db.list_schedule_items(start=d_from, end=d_to, limit=limit)
        span = d_from if d_from == d_to else f"{d_from} ~ {d_to}"
        if not rows:
            return ToolResult(
                f"日历里 {span} 没有日程条目。可以再用 recent_chat / search_chat "
                f"看这几天的群消息，避免漏掉没被抽成日程的通知。",
                summary=f"{span}：0 条", ok=True)

        lines = [f"【日历】{span} · 共 {len(rows)} 条"]
        cur_day = ""
        for r in rows:
            day = r.get("date") or ""
            if day != cur_day:
                lines.append("")
                lines.append(f"—— {day} ——")
                cur_day = day
            tm = (r.get("time") or "").strip() or "--:--"
            title = (r.get("title") or "").strip()
            detail = (r.get("detail") or "").strip().replace("\n", " ")
            if len(detail) > 160:
                detail = detail[:160] + "…"
            try:
                conf = float(r.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            tags = []
            if r.get("manual"):
                tags.append("手动")
            if conf and conf <= 0.6:
                tags.append(f"待确认 {conf:g}")
            elif conf:
                tags.append(f"置信 {conf:g}")
            line = f"- {tm} {title}"
            if detail:
                line += f"（{detail}）"
            if tags:
                line += " [" + " · ".join(tags) + "]"
            lines.append(line)
            # 每条都带出处：抽错的条目通常在这里露馅（引文对不上标题）
            quote = (r.get("source_quote") or "").strip().replace("\n", " ")
            if len(quote) > 80:
                quote = quote[:80] + "…"
            label = (r.get("source_label") or "").strip()
            kind = {"chat": "群消息", "file": "文档", "recording": "录音",
                    "manual": "手动添加"}.get(r.get("source_type") or "", "")
            bits = [b for b in (label, kind) if b]
            if quote:
                lines.append(f"    依据：「{quote}」" + (f"（{' · '.join(bits)}）" if bits else ""))
            elif bits:
                lines.append(f"    依据：（{' · '.join(bits)}，无原文引文）")
        lines.append("")
        lines.append("（日程来自 AI 抽取，可能不全也不一定准：引用时请连同上面的「依据」和置信度一起说，"
                     "置信度 ≤0.6 的按「待确认」处理；与群消息原文冲突时以原文为准。"
                     "没抽成日程的消息用 recent_chat 复核）")
        return ToolResult("\n".join(lines), summary=f"{span}：{len(rows)} 条日程")

    def _list_knowledge(self, args: dict) -> ToolResult:
        section = _as_str(args.get("section") or args.get("_raw"))
        limit = _as_int(args.get("limit"), 20, 1, 200)
        offset = _as_int(args.get("offset"), 0, 0, 10 ** 7)
        rows = self.db.kb_source_index()
        if section:
            rows = [r for r in rows if (r.get("section") or "") == section]
        if not rows:
            return ToolResult(
                "知识库里还没有已索引的文件。" if not section
                else f"分区 {section} 下没有已索引的文件。",
                summary="无文件", ok=True)
        total = len(rows)
        if offset >= total:
            return ToolResult(
                f"知识库共 {total} 个文件，offset={offset} 已越界（从 0 开始）。",
                summary=f"共 {total} 个文件", ok=True)
        page = rows[offset:offset + limit]
        head = (f"【知识库文件清单】共 {total} 个文件"
                + (f"（分区 {section}）" if section else "")
                + f" · 本次显示第 {offset + 1}–{offset + len(page)} 个：\n")
        lines = []
        for r in page:
            lines.append(f"- {r['source']} · {r['n']} 段 · "
                         f"{r.get('chars') or 0} 字 · {_day_range(r.get('d1'), r.get('d2'))}")
        tail = ""
        nxt = offset + len(page)
        if nxt < total:
            tail = f"\n\n还有 {total - nxt} 个，可用 offset={nxt} 继续看。"
        body = head + "\n".join(lines) + tail
        return ToolResult(body, summary=f"共 {total} 个文件，显示 {len(page)} 个")

    def _kb_overview(self, args: dict) -> ToolResult:
        detail = (_as_str(args.get("detail") or "brief") or "brief").lower()
        st = self.db.get_stats()
        kb = self.db.kb_stats()
        groups = self.db.get_groups()
        first, last = self.db.message_span()

        def _day(ts: int) -> str:
            if not ts:
                return "—"
            from datetime import datetime as _dt
            return _dt.fromtimestamp(ts).strftime("%Y-%m-%d")

        n_groups = sum(1 for g in groups if int(g.get("group_id") or 0) > 0)
        n_buddies = len(groups) - n_groups
        secs = kb.get("by_section") or {}
        dims = kb.get("dims") or []
        sig = self.db.get_meta("index_model_sig") or ""
        lines = [
            "【知识库概览】",
            f"- 会话：{len(groups)} 个（群 {n_groups} · 好友 {n_buddies}），"
            f"消息 {st.get('messages', 0)} 条（重要 {st.get('important', 0)} 条）",
            f"- 时间跨度：{_day(first)} ~ {_day(last)}",
            f"- 知识库：{kb.get('files', 0)} 个文件 / {kb.get('chunks', 0)} 个片段，"
            f"已向量化 {kb.get('with_vector', 0)} 个"
            + (f"，维度 {dims[0]}" if dims else ""),
        ]
        if sig:
            lines.append(f"- 索引模型：{sig}")
        if detail in ("full", "all", "完整"):
            sec_txt = " · ".join(f"{k} {v}" for k, v in sorted(secs.items())) or "—"
            lines.append(f"- 分区片段：{sec_txt}")
            rank = st.get("group_ranking") or []
            if rank:
                lines.append("- 消息最多的会话：")
                for r in rank[:8]:
                    gid = int(r.get("group_id") or 0)
                    tag = "好友" if gid < 0 else "群"
                    lines.append(f"  · {r.get('name') or gid}（{tag} {gid}）"
                                 f" {r.get('msg_count', 0)} 条 · 最近 {_day(int(r.get('last_ts') or 0))}")
        else:
            lines.append(f"- 分区片段：{' · '.join(f'{k} {v}' for k, v in sorted(secs.items())) or '—'}"
                         "（detail=full 看更多）")
        return ToolResult("\n".join(lines), summary="知识库概览")

    def _search_chat(self, args: dict) -> ToolResult:
        keyword = _as_str(args.get("keyword") or args.get("_raw"))
        d_from = _as_str(args.get("date_from"))
        d_to = _as_str(args.get("date_to"))
        important_only = _as_bool(args.get("important_only"))
        user_raw = args.get("user_id")
        uid = None
        if user_raw not in (None, "", "null"):
            uid = _as_int(user_raw, 0, 0, 10 ** 12) or None

        # 群筛选：group_id 优先；否则按群名解析（可给「班级群」「项目组」这类口语）
        gid = None
        gid_raw = args.get("group_id")
        group_raw = _as_str(args.get("group"))
        if gid_raw not in (None, "", "null"):
            gid = _as_int(gid_raw, 0, -10 ** 12, 10 ** 12) or None
        elif group_raw:
            gid, err = self._resolve_group(group_raw)
            if err:
                return ToolResult(err, summary="群名未能确定", ok=False)

        if not keyword and not (d_from or d_to) and not important_only and gid is None:
            return ToolResult(
                "请至少提供 keyword、日期范围、important_only 或 group 之一，"
                "否则会返回过多消息。查「最近群里说了什么」请用 recent_chat。",
                summary="条件不足", ok=False)

        limit = _as_int(args.get("limit"), 100, 1, 300)
        offset = _as_int(args.get("offset"), 0, 0, 1_000_000)
        total = self.db.count_messages(group_id=gid, keyword=keyword, user_id=uid,
                                       important_only=important_only,
                                       date_from=d_from or None,
                                       date_to=d_to or None)
        rows = self.db.query_messages(group_id=gid, keyword=keyword, user_id=uid,
                                      important_only=important_only,
                                      date_from=d_from or None,
                                      date_to=d_to or None,
                                      limit=limit, offset=offset)
        if not rows:
            who = f" · 发言人 {uid}" if uid else ""
            return ToolResult(
                f"没有找到符合条件的群消息{who}"
                + (f"（{d_from}~{d_to}）" if (d_from or d_to) else "")
                + f"，共 {total} 条。可换个更短的词，或改用 search_knowledge 做语义检索。",
                summary="无匹配", ok=True)
        rows = list(reversed(rows))  # query_messages 是时间倒序，翻正便于阅读

        # 结果裁剪：300 条长消息可能有几十万字，不裁剪会把当轮上下文吃光
        max_chars = int(self.rag.get("chat_result_chars")
                        or self.rag.get("tool_result_chars") or 20000)
        lines: list[str] = []
        used_chars = 0
        picked_rows: list[dict] = []
        for m in rows:
            who = m.get("nickname") or str(m.get("user_id") or "")
            text = (m.get("content") or "").strip().replace("\n", " ")
            line = f"[{m.get('time_str', '')}] {who}：{text}"
            if lines and used_chars + len(line) > max_chars:
                break
            lines.append(line)
            used_chars += len(line)
            picked_rows.append(m)
        shown = len(lines)
        cut = shown < len(rows)

        cond = f"关键词「{keyword}」" if keyword else "消息"
        if gid is not None:
            grp = self.db.get_group(gid)
            if grp and grp.get("name"):
                cond += f" · {grp['name']}（{gid}）"
            else:
                cond += f" · 会话 {gid}"
        if uid:
            cond += f" · 发言人 {uid}"
        if important_only:
            cond += " · 仅重要"
        if d_from or d_to:
            cond += f" · {d_from}~{d_to}"
        head = (f"【群消息】{cond} · 共 {total} 条 · 本页 {shown} 条"
                + (f" · offset={offset}" if offset else "") + "\n")
        tail = ""
        if cut:
            tail += (f"\n（受长度限制本页只展示前 {shown} 条，"
                     f"可用 offset={offset + shown} 继续翻页）")
        elif offset + shown < total:
            tail += (f"\n（还有 {total - offset - shown} 条，"
                     f"可用 offset={offset + shown} 继续翻页）")
        return ToolResult(head + "\n".join(lines) + tail,
                          summary=f"共 {total} 条，本页 {shown} 条",
                          sources=[_chat_cite(m) for m in picked_rows[:40]])
