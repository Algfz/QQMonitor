"""Agent：让模型自己决定「查不查、查几次、用哪个工具」的问答循环。

流程
----
1. system（AGENT_SYSTEM，含今天日期与工具使用准则）+ 历史 + 用户问题
2. 模型流式返回：可能直接给文本（寒暄/通用问题），也可能请求调用工具
3. 有工具调用 → 执行 → 结果作为 role=tool 消息追加 → 回到 2
4. 无工具调用 → 这一轮的文本即最终答案
5. 达到 max_rounds 仍未收尾 → 禁用工具再问一次，强制它用已有资料作答

事件回调
--------
run() 期间通过 on_event 同步回调事件字典（在调用方线程里执行，
服务端负责跨线程投递）：
    {"type": "delta",       "text": "..."}                  文本增量
    {"type": "tool_start",  "name": ..., "label": ..., "args": {...}, "args_text": "..."}
    {"type": "tool_done",   "name": ..., "label": ..., "summary": "...",
                            "ok": bool, "sources": [...]}
    {"type": "fallback",    "reason": "..."}                模型不支持工具，走固定检索

降级
----
部分模型/网关不支持 function calling（400 报错含 tool/function 等字样），
此时自动切回「固定检索流程」（intent 预判 + RAG 提示词），功能不缺失，
只是由后端决定要不要检索。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .client import LLMError, build_client, llm_ready
from . import prompts as P
from .tools import LABELS, TOOL_SCHEMAS, ToolRunner, _doc_cite

log = logging.getLogger("llm.agent")

# 模型/网关不支持工具调用时，报错里常见的关键词
_UNSUPPORTED_HINT = ("tool", "function", "tools", "unsupported", "not supported",
                     "不支持", "不支持工具", "function_call")

# 历史消息参与上下文的上限（字符数与条数，取先到者）
_HISTORY_MAX_MSGS = 40
_HISTORY_MAX_CHARS = 30000

# 单轮问答里工具返回内容的总字符预算。给出大方，但要有上界：
# 否则 6 轮 × 6 万字 ≈ 18 万 token，会直接把请求撑到 400。
_DEFAULT_BUDGET_CHARS = 60000

_BUDGET_NOTICE = ("（资料量已达上下文上限，请立即依据上面已获取的内容作答。"
                  "资料不足就明确说明查到了什么、还缺什么，不要继续调用工具。）")
_ROUND_NOTICE = ("（工具调用轮数已达上限，请立即依据上面已获取的资料作答。"
                 "资料不足就明确说明查到了什么、还缺什么，不要继续调用工具。）")


def _looks_unsupported(msg: str) -> bool:
    low = (msg or "").lower()
    return any(h in low for h in _UNSUPPORTED_HINT)


def _args_text(args: dict) -> str:
    """把工具参数压成一行，用于前端展示（避免刷屏）。"""
    if not args:
        return ""
    parts = []
    for k, v in list(args.items())[:4]:
        if v in (None, "", []):
            continue
        s = str(v)
        if len(s) > 40:
            s = s[:40] + "…"
        parts.append(f"{k}={s}")
    return " · ".join(parts)


class Agent:
    """一次提问的完整处理（含多轮工具调用）。"""

    def __init__(self, db, cfg, on_event=None):
        self.db = db
        self.cfg = cfg
        self.on_event = on_event or (lambda _ev: None)
        self.runner = ToolRunner(db, cfg)
        self.tools_used: list[dict] = []
        self.sources: list[dict] = []
        self.used_tools = False
        self._tool_chars = 0  # 本轮问答里工具返回内容的累计字符数

    # ---------- 对外入口 ----------
    def _extra_system(self) -> str:
        """用户在 llm.system_extra 里写的自定义提示词，追加在内置提示词之后。"""
        try:
            extra = (self.cfg.llm.get("system_extra") or "").strip()
        except Exception:
            return ""
        if not extra:
            return ""
        return "\n\n【用户自定义规则】（优先于上面同样的主题，但仍须遵守查证与引用要求）\n" + extra

    def run(self, question: str, history: list[dict] | None = None) -> dict:
        """执行问答。返回 {text, tools, sources, model, fallback}。"""
        cfg = self.cfg
        rag = cfg.llm.get("rag") or {}
        max_rounds = max(1, int(rag.get("max_rounds") or 6))
        budget = int(rag.get("context_budget_chars") or _DEFAULT_BUDGET_CHARS)
        today = datetime.now().strftime("%Y-%m-%d")
        model = (cfg.llm.get("chat") or {}).get("model", "")

        chat = build_client(cfg, "chat")
        # 工具不再被 embedding 绑定：search_chat / read_document / list_knowledge
        # 都是纯 SQL/文件操作，embedding 挂了也照样能查；只有 search_knowledge
        # 会按需降级为关键词检索（见 ToolRunner._search_knowledge）。
        tools = TOOL_SCHEMAS

        user_profile = P.build_user_profile(getattr(cfg, "user", None))
        try:
            group_list = P.build_group_list(self.db.get_groups())
        except Exception:  # 群列表只是锦上添花，读不到不影响问答
            log.debug("读取群列表失败", exc_info=True)
            group_list = ""
        messages: list[dict] = [
            {"role": "system", "content": P.AGENT_SYSTEM.format(
                today=today, user_profile=user_profile, group_list=group_list)
                + self._extra_system()}]
        messages += self._history(history)
        messages.append({"role": "user", "content": question})

        answer = ""
        for _round in range(1, max_rounds + 1):
            try:
                text, calls = self._call(chat, messages, tools)
            except LLMError as e:
                # 不管是第几轮，只要错误像「模型不支持工具调用」，就整轮切到
                # 固定检索流程，而不是把 500 抛给前端（部分模型是第二轮才暴露）。
                if tools and _looks_unsupported(str(e)):
                    log.warning("模型不支持工具调用，切换固定检索流程：%s", e)
                    self.on_event({"type": "fallback", "reason": str(e)})
                    return self._fallback(question, history, model)
                raise
            if not calls:
                answer = text
                break
            # 本轮文本只是「思考/过渡语」，不作为最终答案（前端会折叠展示）
            messages.append(_assistant_tool_message(text, calls))
            for call in calls:
                self._run_tool(messages, call)
            # 预算按「整段对话的实际字符数」算（系统提示/历史/工具结果都算），
            # 旧实现只累加工具文本，长历史会话会悄悄超出模型窗口。
            total_chars = sum(len(m.get("content") or "") for m in messages)
            if self._tool_chars >= budget or total_chars >= budget:
                # 上下文预算用完：禁用工具再问一次，逼它用现有资料收尾
                messages.append({"role": "user", "content": _BUDGET_NOTICE})
                text, _calls = self._call(chat, messages, None, force_text=True)
                answer = text
                break
        else:
            # 轮次用尽仍在调工具：禁用工具再问一次，逼它收尾
            messages.append({"role": "user", "content": _ROUND_NOTICE})
            text, _calls = self._call(chat, messages, None, force_text=True)
            answer = text

        return {"text": (answer or "").strip(),
                "tools": self.tools_used,
                "sources": self._dedup(self.sources),
                "model": model,
                "fallback": False}

    # ---------- 内部 ----------
    def _emit(self, ev: dict) -> None:
        try:
            self.on_event(ev)
        except Exception:  # 回调异常不能影响主流程
            log.exception("事件回调异常")

    def _call(self, chat, messages: list[dict], tools, force_text: bool = False):
        """流式调用一次，返回 (本轮文本, 工具调用列表)。"""
        buf: list[str] = []
        calls: list[dict] = []
        use_tools = None if force_text else tools
        for ev in chat.chat_stream_events(messages, tools=use_tools):
            if ev["type"] == "delta":
                buf.append(ev["text"])
                self._emit({"type": "delta", "text": ev["text"]})
            elif ev["type"] == "tool_calls":
                calls = ev["calls"]
        return "".join(buf), calls

    def _run_tool(self, messages: list[dict], call: dict) -> None:
        name = call.get("name") or ""
        args = call.get("arguments") or {}
        label = LABELS.get(name, name)
        self._emit({"type": "tool_start", "name": name, "label": label,
                    "args": args, "args_text": _args_text(args)})
        res = self.runner.run(name, args)
        self.used_tools = True
        self.tools_used.append({
            "name": name, "label": label, "args": args,
            "args_text": _args_text(args), "summary": res.summary,
            "ok": res.ok,
        })
        self.sources.extend(res.sources)
        self._tool_chars += len(res.content or "")
        self._emit({"type": "tool_done", "name": name, "label": label,
                    "summary": res.summary, "ok": res.ok,
                    "sources": res.sources})
        # 注意：不要带 name 字段。OpenAI 系接口对请求体做严格校验，
        # 工具响应消息只认 role / content / tool_call_id，多传字段会 400。
        messages.append({
            "role": "tool",
            "tool_call_id": call.get("id") or name,
            "content": res.content or "（工具没有返回内容）",
        })

    @staticmethod
    def _history(history: list[dict] | None) -> list[dict]:
        """取最近若干条历史，按字符数兜底裁剪，避免长会话撑爆上下文。"""
        items = [m for m in (history or [])
                 if m.get("role") in ("user", "assistant") and (m.get("content") or "")]
        picked: list[dict] = []
        total = 0
        for m in reversed(items[-_HISTORY_MAX_MSGS:]):
            total += len(m["content"])
            if total > _HISTORY_MAX_CHARS and picked:
                break
            picked.append({"role": m["role"], "content": m["content"]})
        return list(reversed(picked))

    @staticmethod
    def _dedup(sources: list[dict]) -> list[dict]:
        """去重但**保留全部字段**（引用材料需要 msg_id/group_id/quote/kind）。

        同一段资料可能被多个工具命中；聊天记录按消息号区分，
        文档按 (source, section) 区分，都带 day 兜底。
        """
        seen: set[tuple] = set()
        out: list[dict] = []
        for s in sources:
            key = (s.get("kind") or "",
                   s.get("source") or "",
                   s.get("section") or "",
                   s.get("msg_id") or 0,
                   s.get("day") or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(s))
        return out

    # ---------- 降级：模型不支持工具时 ----------
    def _fallback(self, question: str, history: list[dict] | None, model: str) -> dict:
        """旧流程：后端预判要不要检索，一次性把资料塞进提示词。"""
        from .intent import needs_search
        from .retriever import retrieve

        cfg = self.cfg
        rag = cfg.llm.get("rag") or {}
        top_k = int(rag.get("top_k") or 12)
        max_ctx = int(rag.get("max_context") or 60000)
        today = datetime.now().strftime("%Y-%m-%d")

        used: list[dict] = []
        if needs_search(question):
            hits: list[dict] = []
            if llm_ready(cfg, "embedding"):
                try:
                    hits = retrieve(self.db, cfg, self.runner._embedding(),
                                    question, top_k)
                except Exception as e:
                    log.warning("语义检索失败，降级关键词检索：%s", e)
            if not hits:
                # embedding 未配置/模型缺失时也别直接放弃：关键词检索仍能兜住
                from .retriever import retrieve_keyword
                hits = retrieve_keyword(self.db, cfg, question, top_k)
            if hits:
                context, used = P.build_context(hits, max_ctx)
                self.sources.extend(_doc_cite(c["source"], c.get("section", ""),
                                              c.get("day"),
                                              c.get("text") or "") for c in used)
                self.tools_used.append({
                    "name": "search_knowledge", "label": LABELS["search_knowledge"],
                    "args": {"query": question}, "args_text": f"query={question[:40]}",
                    "summary": f"命中 {len(used)} 段", "ok": True,
                })
                self._emit({"type": "tool_start", "name": "search_knowledge",
                            "label": LABELS["search_knowledge"],
                            "args": {"query": question},
                            "args_text": f"query={question[:40]}"})
                self._emit({"type": "tool_done", "name": "search_knowledge",
                            "label": LABELS["search_knowledge"],
                            "summary": f"命中 {len(used)} 段", "ok": True,
                            "sources": self.sources})

        user_profile = P.build_user_profile(getattr(self.cfg, "user", None))
        if used:
            system = P.RAG_SYSTEM.format(today=today, user_profile=user_profile)
            payload_user = P.RAG_USER.format(context=context, question=question)
        else:
            system = P.CHAT_SYSTEM.format(today=today, user_profile=user_profile)
            payload_user = question

        msgs: list[dict] = [{"role": "system", "content": system}]
        msgs += self._history(history)
        msgs.append({"role": "user", "content": payload_user})

        chat = build_client(cfg, "chat")
        text, _calls = self._call(chat, msgs, None)
        return {"text": (text or "").strip(), "tools": self.tools_used,
                "sources": self._dedup(self.sources), "model": model,
                "fallback": True}


def _assistant_tool_message(text: str, calls: list[dict]) -> dict:
    """把「模型要求调用工具」这一步还原成可回传的 assistant 消息。"""
    return {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [
            {
                "id": c.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": c.get("name") or "",
                    # arguments 必须是字符串；这里把解析过的 dict 原样序列化回去
                    "arguments": json.dumps(c.get("arguments") or {},
                                            ensure_ascii=False),
                },
            }
            for i, c in enumerate(calls)
        ],
    }
