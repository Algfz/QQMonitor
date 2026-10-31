#!/usr/bin/env python
"""QQMonitor 知识库 MCP server（stdio 传输，供 MCP 客户端挂载）。

不依赖任何第三方库：只用标准库 urllib 调用 QQMonitor 的只读知识库 API。

环境变量:
    QQMONITOR_API_URL    形如 https://<机器名>.<tailnet>.ts.net:8444
                         或 http://127.0.0.1:8088（默认）
    QQMONITOR_API_TOKEN  config.yaml 里 api.token 的值（必填）

在 MCP 客户端的配置文件里挂载示例见 docs/MCP-INTEGRATION.md。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# 兼容旧环境变量名（QQMONITER_*，早期版本用的拼写）：新名优先，老名兜底，
# 这样已经按旧文档配好的 MCP 客户端不用改就能继续用。
def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


API_URL = _env("QQMONITOR_API_URL", "QQMONITER_API_URL", default="http://127.0.0.1:8088").rstrip("/")
TOKEN = _env("QQMONITOR_API_TOKEN", "QQMONITER_API_TOKEN").strip()
SERVER_NAME = "qqmonitor-kb"
SERVER_VERSION = "1.0.2"

# 全局 opener：显式禁用环境代理（见 call_api 注释）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_TOOLS = [
    {
        "name": "kb_search",
        "description": (
            "在本机构建的群知识库里检索聊天记录与文件资料，"
            "返回相关文字片段（只读，不会修改任何数据）。"
            "适合查通知、活动、名单、任务、资料等具体信息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或问题"},
                "k": {"type": "integer", "description": "返回条数，默认 8，最多 12"},
                "groups": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "只在这些群号里检索（可选，不传=全部群）",
                },
                "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD（可选）"},
                "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD（可选）"},
                "include_documents": {
                    "type": "boolean",
                    "description": "是否包含上传的文档/文字稿，默认 true",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_ask",
        "description": (
            "用知识库自带的 AI 回答问题，**并返回它引用的原始材料**。"
            "默认走 agent 模式：和网页端那个助手是同一个模型与工具，会自己决定查群聊、"
            "翻原文、读文档，多轮核对后再作答，回答里通常带日期/出处。"
            "适合「xx 活动什么时候」「谁负责 xx」「上次班会说的事是哪天定的」这类问题。"
            "返回 = 回答正文 + 引用材料（每条含原文片段、文件路径、群号/消息号）。"
            "回答仍可能有遗漏：结论重要时请用返回的引用材料复核，或再用 kb_search 查证。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问的问题"},
                "mode": {
                    "type": "string", "enum": ["agent", "fast"],
                    "description": ("agent（默认）= 多轮工具调用，慢但可靠；"
                                    "fast = 只检索一次就总结，快但只看前 k 条"),
                },
                "history": {
                    "type": "array",
                    "description": ("可选的对话历史（同一个话题追问时传，最多 20 条），"
                                    "形如 [{\"role\":\"user\",\"content\":\"...\"}]"),
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["user", "assistant"]},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
                "k": {"type": "integer",
                      "description": "mode=fast 时的检索条数，默认 8（agent 模式忽略）"},
                "groups": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "只在这些群号里检索（可选，仅 mode=fast 生效）",
                },
                "date_from": {"type": "string",
                              "description": "起始日期 YYYY-MM-DD（可选，仅 mode=fast 生效）"},
                "date_to": {"type": "string",
                            "description": "结束日期 YYYY-MM-DD（可选，仅 mode=fast 生效）"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "kb_meta",
        "description": "查看知识库统计信息与已收录的群列表（只读）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "kb_overview",
        "description": (
            "知识库总体情况：会话/群/好友数、消息总数与重要消息数、时间跨度、"
            "文件与片段数、索引模型、各分区片段数；detail=full 时附消息最多的会话。"
            "用户问「知识库里有什么」「数据到哪天了」「哪个群最活跃」时先用它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "detail": {"type": "string",
                           "enum": ["brief", "full"],
                           "description": "brief（默认）或 full"},
            },
        },
    },
    {
        "name": "kb_list_files",
        "description": (
            "列出知识库里已索引的文件：路径、分区（chat/documents/transcripts）、"
            "片段数、字符数、事件日期范围，支持分页。用于确认某份文件是否在库里、"
            "或找到 read_document 要读的准确路径。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": {"type": "string",
                            "description": "只列某个分区：chat / documents / transcripts"},
                "limit": {"type": "integer", "description": "本页条数，默认 20，最大 200"},
                "offset": {"type": "integer", "description": "从第几个开始，0 起"},
            },
        },
    },
    {
        "name": "kb_read_document",
        "description": (
            "读取知识库里某份文本文件的原文（md/txt/csv/json/srt/vtt/ass），"
            "可先用 keyword 只看包含关键词的段落，内容太长可用 offset 续读。"
            "source 用 kb_list_files 或 kb_search 返回的路径（如 documents/学生手册.md）。"
            "PDF/Word 请读已提取到 transcripts/ 的文字稿。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "知识库相对路径，如 transcripts/xx.md"},
                "keyword": {"type": "string", "description": "只看包含该词的段落（可选）"},
                "offset": {"type": "integer", "description": "从第几个字符开始，默认 0"},
                "max_chars": {"type": "integer",
                              "description": "本次最多返回多少字，默认 20000，最大 60000"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "kb_search_chat",
        "description": (
            "直接按条件检索 QQ 群聊/私聊原文（不经过向量索引），可按群名或群号、"
            "发言人 QQ、日期范围、是否重要筛选，返回真实总数并支持分页。"
            "适合查「谁在哪天说过什么」、核对通知原文。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词（可留空）"},
                "group": {"type": "string",
                          "description": "会话名（如「班级群」「项目组」）或群号；好友会话也可用名字"},
                "group_id": {"type": "integer", "description": "群号/好友 gid（负数）"},
                "user_id": {"type": "integer", "description": "发言人 QQ 号"},
                "important_only": {"type": "boolean", "description": "只看重要消息"},
                "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "本页条数，默认 20，最大 200"},
                "offset": {"type": "integer", "description": "分页偏移，0 起"},
            },
        },
    },
    {
        "name": "kb_recent_chat",
        "description": (
            "不带关键词看最近的 QQ 群聊/私聊消息（按时间倒序取最近的），"
            "可按群、日期范围、是否重要筛选。适合「最近群里在聊什么」这类问题。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "会话名或群号（可选）"},
                "group_id": {"type": "integer", "description": "群号/好友 gid（可选）"},
                "important_only": {"type": "boolean", "description": "只看重要消息"},
                "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "条数，默认 40，最大 200"},
                "offset": {"type": "integer", "description": "分页偏移，0 起"},
            },
        },
    },
    {
        "name": "rec_list",
        "description": "列出录音与处理状态（分类/日期/状态/文字稿/总结/日程），只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数，默认 20，最大 200"},
            },
        },
    },
    {
        "name": "rec_get",
        "description": (
            "读取一条录音的转写/总结/日程。转写带 [HH:MM:SS] 时间码、说话人、"
            "高/中/低置信标记；总结和日程通过 recording_id 与原始转写文件关联。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "录音 id（rec_list 返回）"},
                "kind": {"type": "string",
                         "enum": ["all", "transcript", "summary", "schedule"],
                         "description": "读取哪一部分，默认 all"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "schedule_list",
        "description": (
            "读取日历里的日程（来自群聊、文件、录音的统一抽取结果），"
            "返回日期/时间/标题/详情/原句/来源链接，可按日期范围筛选。只读。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "起始日期 YYYY-MM-DD（可选）"},
                "end": {"type": "string", "description": "结束日期 YYYY-MM-DD（可选）"},
                "limit": {"type": "integer", "description": "条数，默认 100，最大 2000"},
            },
        },
    },
]


def _log(msg: str) -> None:
    """日志只写 stderr，stdout 留给 MCP 协议。"""
    print(f"[qqmonitor-kb] {msg}", file=sys.stderr, flush=True)


def call_api(method: str, path: str, payload: dict | None = None,
             timeout: int = 120) -> dict:
    if not TOKEN:
        return {"ok": False,
                "error": "未设置 QQMONITOR_API_TOKEN 环境变量（值见 QQMonitor 的 config.yaml api.token）"}
    url = API_URL + path
    data = None
    headers = {"Authorization": f"Bearer {TOKEN}",
               "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # 直连，不读 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY：对端机器上常设着代理，
        # 走代理访问内网/Tailscale 域名可能被拦或连错目标。
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"error": raw[:300] or f"HTTP {e.code}"}
        detail.setdefault("ok", False)
        detail["http_status"] = e.code
        return detail
    except Exception as e:  # noqa: BLE001 - 网络问题都要转成可读错误
        return {"ok": False,
                "error": f"无法连接 QQMonitor API（{API_URL}）：{e}"}


def _fmt_hits(res: dict) -> str:
    hits = res.get("hits") or []
    if not hits:
        return "没有检索到相关内容。"
    lines = []
    for i, h in enumerate(hits, 1):
        head = f"[{i}] {h.get('source')}"
        if h.get("day"):
            head += f"（{h['day']}）"
        lines.append(head + "\n" + str(h.get("text") or ""))
    return "\n\n".join(lines)


def _fmt_overview(res: dict) -> str:
    kb = res.get("kb") or {}
    lines = [
        "【知识库概览】",
        f"- 会话：{res.get('conversations')} 个"
        f"（群 {res.get('groups')} · 好友 {res.get('buddies')}），"
        f"消息 {res.get('messages')} 条（重要 {res.get('important')} 条）",
        f"- 时间跨度：{res.get('first_day') or '—'} ~ {res.get('last_day') or '—'}",
        f"- 知识库：{kb.get('files', 0)} 个文件 / {kb.get('chunks', 0)} 个片段，"
        f"已向量化 {kb.get('with_vector', 0)} 个",
    ]
    if res.get("index_model"):
        lines.append(f"- 索引模型：{res['index_model']}")
    secs = kb.get("by_section") or {}
    if secs:
        lines.append("- 分区片段：" + " · ".join(f"{k} {v}" for k, v in sorted(secs.items())))
    tops = res.get("top_conversations") or []
    if tops:
        lines.append("- 消息最多的会话：")
        for t in tops:
            kind = "好友" if t.get("kind") == "buddy" else "群"
            lines.append(f"  · {t.get('name')}（{kind} {t.get('group_id')}）"
                         f" {t.get('messages')} 条 · 最近 {t.get('last_day') or '—'}")
    return "\n".join(lines)


def _fmt_files(res: dict) -> str:
    files = res.get("files") or []
    total = int(res.get("total") or 0)
    if not files:
        return f"知识库共 {total} 个文件，没有更多可列出的了。"
    start = int(res.get("offset") or 0) + 1
    end = start + len(files) - 1
    head = (f"【知识库文件清单】共 {total} 个文件"
            f"（本次第 {start}–{end} 个）：")
    lines = []
    for f in files:
        lines.append(f"- {f.get('source')} · {f.get('chunks', 0)} 段 · "
                     f"{f.get('chars', 0)} 字 · "
                     f"{(f.get('day_from') or '?')}~{(f.get('day_to') or '?')}")
    text = head + "\n" + "\n".join(lines)
    if res.get("has_more"):
        text += f"\n\n还有 {total - end} 个，可用 offset={end} 继续看。"
    return text


def _fmt_doc(res: dict) -> str:
    source = res.get("source") or ""
    total = int(res.get("total_chars") or 0)
    start = int(res.get("offset") or 0)
    body = str(res.get("text") or "")
    head = f"【文档】{source}（共 {total} 字 · 本次第 {start + 1}–{start + len(body)} 字）"
    if res.get("note"):
        head += f"\n（{res['note']}）"
    text = head + "\n\n" + body
    if res.get("has_more"):
        text += f"\n\n……后面还有 {total - (start + len(body))} 字，可用 offset={res.get('next_offset')} 继续读。"
    return text


def _fmt_chat(res: dict, title: str) -> str:
    msgs = res.get("messages") or []
    total = int(res.get("total") or 0)
    if not msgs:
        return f"【{title}】共 {total} 条，没有找到消息。"
    head = (f"【{title}】共 {total} 条 · "
            f"本次显示第 {int(res.get('offset') or 0) + 1}–"
            f"{int(res.get('offset') or 0) + len(msgs)} 条：")
    lines = []
    for m in msgs:
        who = m.get("nickname") or m.get("user_id") or "?"
        tag = "【重要】" if m.get("important") else ""
        lines.append(f"- {m.get('day') or ''} {who}：{tag}{m.get('content') or ''}")
    text = head + "\n" + "\n".join(lines)
    if res.get("has_more"):
        nxt = int(res.get("offset") or 0) + len(msgs)
        text += f"\n\n还有 {total - nxt} 条更早的，可用 offset={nxt} 继续。"
    return text


def _run_tool(name: str, args: dict) -> dict:
    if name == "kb_search":
        payload = {
            "query": args.get("query") or "",
            "k": args.get("k") or 8,
            "include_documents": args.get("include_documents", True),
        }
        for key in ("groups", "date_from", "date_to"):
            if args.get(key) not in (None, "", []):
                payload[key] = args[key]
        res = call_api("POST", "/api/kb/search", payload)
        text = _fmt_hits(res) if res.get("ok") else str(res.get("error") or res)
        return {"content": [{"type": "text", "text": text}],
                "isError": not bool(res.get("ok"))}
    if name == "kb_ask":
        mode = str(args.get("mode") or "agent").strip().lower()
        if mode not in ("agent", "fast"):
            mode = "agent"
        payload = {"question": args.get("question") or "", "mode": mode,
                   "k": args.get("k") or 8}
        hist = args.get("history")
        if isinstance(hist, list) and hist:
            payload["history"] = [
                {"role": str(m.get("role") or "user"),
                 "content": str(m.get("content") or "")}
                for m in hist if isinstance(m, dict) and m.get("content")][-20:]
        if mode == "fast":
            for key in ("groups", "date_from", "date_to"):
                if args.get(key) not in (None, "", []):
                    payload[key] = args[key]
        # agent 模式可能跑多轮工具调用，给足时间（服务端每轮 LLM 超时 30s）
        res = call_api("POST", "/api/kb/ask", payload,
                       timeout=300 if mode == "agent" else 120)
        if not res.get("ok"):
            text = str(res.get("error") or res)
            return {"content": [{"type": "text", "text": text}], "isError": True}
        text = str(res.get("answer") or "").strip() or "（模型没有给出回答）"
        cites = res.get("citations") or []
        if mode == "fast" and not cites:
            # 兼容旧服务端：fast 模式只返回 sources
            cites = [{"id": s.get("id"), "kind": "chat" if s.get("group_id") else "",
                      "title": str(s.get("source") or ""),
                      "day": s.get("day") or "",
                      "source": str(s.get("source") or "")}
                     for s in (res.get("sources") or [])]
        if cites:
            text += f"\n\n【引用材料 · {len(cites)} 条】"
            for c in cites:
                kind = c.get("kind_label") or c.get("kind") or "资料"
                title = str(c.get("title") or c.get("source") or "")
                head = f"\n[{c.get('id')}] {kind}｜{title}"
                if c.get("day") and c.get("day") not in title:
                    head += f" · {c['day']}"
                text += head
                if c.get("quote"):
                    text += f"\n    原句：{c['quote']}"
                src = str(c.get("source") or "")
                if src:
                    text += f'\n    原文：{src}（kb_read_document(source="{src}")）'
                elif c.get("msg_id"):
                    text += f"\n    原文：/msg/{c['msg_id']}（群 {c.get('group_id')}）"
            text += ("\n\n（引用材料是 AI 回答时实际用到的片段；"
                     "回答正文里的 [n] 是它当时看到的检索编号，和这里的 id 不一一对应，"
                     "请按内容对应。结论重要时请据此复核，或再调 kb_search 查证。）")
        else:
            text += "\n\n（本次回答没有引用到知识库材料，可信度有限。）"
        if res.get("fallback"):
            text += "\n（注：该模型不支持工具调用，已退回固定检索流程。）"
        took = res.get("took_ms")
        if isinstance(took, (int, float)) and took > 0:
            text += f"\n（耗时 {took / 1000:.1f}s，模式 {res.get('mode') or mode}）"
        return {"content": [{"type": "text", "text": text}], "isError": False}
    if name == "kb_meta":
        res = call_api("GET", "/api/kb/meta")
        text = (json.dumps(res, ensure_ascii=False, indent=2)
                if res.get("ok") else str(res.get("error") or res))
        return {"content": [{"type": "text", "text": text}],
                "isError": not bool(res.get("ok"))}
    if name == "kb_overview":
        detail = args.get("detail") or "brief"
        res = call_api("GET", "/api/kb/overview?" + urllib.parse.urlencode({"detail": detail}))
        text = _fmt_overview(res) if res.get("ok") else str(res.get("error") or res)
        return {"content": [{"type": "text", "text": text}],
                "isError": not bool(res.get("ok"))}
    if name == "kb_list_files":
        params = {}
        for key in ("section", "limit", "offset"):
            if args.get(key) not in (None, ""):
                params[key] = args[key]
        res = call_api("GET", "/api/kb/files?" + urllib.parse.urlencode(params))
        text = _fmt_files(res) if res.get("ok") else str(res.get("error") or res)
        return {"content": [{"type": "text", "text": text}],
                "isError": not bool(res.get("ok"))}
    if name == "kb_read_document":
        payload = {"source": args.get("source") or ""}
        for key in ("keyword", "offset", "max_chars"):
            if args.get(key) not in (None, ""):
                payload[key] = args[key]
        res = call_api("POST", "/api/kb/document", payload)
        text = _fmt_doc(res) if res.get("ok") else str(res.get("error") or res)
        return {"content": [{"type": "text", "text": text}],
                "isError": not bool(res.get("ok"))}
    if name in ("kb_search_chat", "kb_recent_chat"):
        payload = {}
        for key in ("keyword", "group", "group_id", "user_id", "important_only",
                    "date_from", "date_to", "limit", "offset"):
            if args.get(key) not in (None, "", []):
                payload[key] = args[key]
        if name == "kb_recent_chat":
            payload.pop("keyword", None)
            path, title = "/api/kb/chat/recent", "最近消息"
        else:
            path, title = "/api/kb/chat/search", "群消息检索"
        res = call_api("POST", path, payload)
        text = _fmt_chat(res, title) if res.get("ok") else str(res.get("error") or res)
        return {"content": [{"type": "text", "text": text}],
                "isError": not bool(res.get("ok"))}
    if name == "rec_list":
        res = call_api("GET", "/api/recordings?" + urllib.parse.urlencode(
            {"limit": args.get("limit") or 20}))
        if not res.get("ok"):
            return {"content": [{"type": "text", "text": str(res.get("error") or res)}],
                    "isError": True}
        rows = res.get("recordings") or []
        lines = ["【录音】"]
        for r in rows:
            lines.append(
                f"- #{r.get('id')} {r.get('course') or '未识别'} "
                f"{r.get('rec_date') or ''} {r.get('filename')} · {r.get('status')}"
                + (f" · 转写 {r['md_path']}" if r.get("md_path") else "")
                + (f" · 总结 {r['summary_path']}" if r.get("summary_path") else "")
                + (f" · 日程 {r['schedule_path']}" if r.get("schedule_path") else ""))
        if not rows:
            lines.append("（还没有录音）")
        return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": False}
    if name == "rec_get":
        rid = args.get("id")
        res = call_api("GET", f"/api/recordings/{int(rid)}")
        if not res.get("ok"):
            return {"content": [{"type": "text", "text": str(res.get("error") or res)}],
                    "isError": True}
        kind = args.get("kind") or "all"
        rec = res.get("recording") or {}
        parts = [f"【录音 #{rec.get('id')}】{rec.get('course')} {rec.get('rec_date')} "
                 f"{rec.get('filename')} · {rec.get('status')}"]
        if kind in ("all", "transcript") and res.get("transcript"):
            parts.append("\n===== 转写（含置信度标记）=====\n" + str(res["transcript"]))
        if kind in ("all", "summary") and res.get("summary"):
            parts.append("\n===== 总结 =====\n" + str(res["summary"]))
        if kind in ("all", "schedule") and res.get("schedule") is not None:
            parts.append("\n===== 日程 =====\n" + json.dumps(
                res["schedule"], ensure_ascii=False, indent=2))
        return {"content": [{"type": "text", "text": "\n".join(parts)}], "isError": False}
    if name == "schedule_list":
        qs = {}
        for k in ("start", "end", "limit"):
            if args.get(k) not in (None, ""):
                qs[k] = args[k]
        res = call_api("GET", "/api/schedules?" + urllib.parse.urlencode(qs))
        if not res.get("ok"):
            return {"content": [{"type": "text", "text": f"读取失败：{res.get('error')}"}],
                    "isError": True}
        items = res.get("items") or []
        lines = [f"共 {len(items)} 条日程"]
        for it in items:
            src = it.get("source_label") or it.get("source_type") or ""
            t = it.get("time") or "全天"
            lines.append(f"- {it.get('date')} {t} {it.get('title')}｜{src}")
            if it.get("detail"):
                lines.append(f"  {it.get('detail')}")
            if it.get("source_quote"):
                lines.append(f"  原句：{it.get('source_quote')}")
            ref = it.get("source_ref")
            if ref:
                lines.append(f"  来源：{ref}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": False}
    return {"content": [{"type": "text", "text": f"未知工具：{name}"}],
            "isError": True}


def _send(msg: dict) -> None:
    # MCP 规范要求 stdout 为 UTF-8。Windows 上子进程的 sys.stdout 默认是
    # 本地代码页（cp936），直接 write 中文会写出 GBK 字节，客户端按 UTF-8
    # 解码就会炸。所以优先写底层 buffer，并强制 UTF-8。
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(line.encode("utf-8"))
        buf.flush()
    else:  # 极端环境（被替换成纯文本流）
        sys.stdout.write(line)
        sys.stdout.flush()


def handle(msg: dict) -> None:
    method = msg.get("method") or ""
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }})
        return
    if method.startswith("notifications/"):
        return  # 通知不需要回复
    if method == "ping":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        return
    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _TOOLS}})
        return
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            result = _run_tool(name, args)
        except Exception as e:  # noqa: BLE001
            _log(f"tool {name} error: {e}")
            result = {"content": [{"type": "text", "text": f"工具执行失败：{e}"}],
                      "isError": True}
        _send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        return
    if method in ("resources/list", "prompts/list"):
        key = "resources" if method == "resources/list" else "prompts"
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {key: []}})
        return
    if msg_id is not None:
        _send({"jsonrpc": "2.0", "id": msg_id,
               "error": {"code": -32601, "message": f"method not found: {method}"}})


def _force_utf8_stdio() -> None:
    """把 stdio 固定为 UTF-8（Windows 默认代码页不是 UTF-8）。"""
    for stream, kwargs in (
        (sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
        (sys.stdout, {"encoding": "utf-8", "newline": "\n"}),
        (sys.stderr, {"encoding": "utf-8", "errors": "replace"}),
    ):
        try:
            stream.reconfigure(**kwargs)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main() -> int:
    _force_utf8_stdio()
    _log(f"started, api={API_URL}, token={'set' if TOKEN else 'MISSING'}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": f"parse error: {e}"}})
            continue
        if isinstance(msg, dict):
            handle(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
