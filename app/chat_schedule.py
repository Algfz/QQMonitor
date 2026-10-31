"""从 QQ 群聊记录中自动抽取日程。

每轮自动同步（默认 3 分钟）后调用：把当天全部白名单群的消息喂给模型，
让它只抽取明确的日期/截止/活动/提交事项，写入 schedule_items。

设计要点：
- 全量当天上下文：模型能看到一件事的前后讨论，减少断章取义；
- 允许查资料：给模型 search_chat / search_knowledge 等工具，遇到“老时间”
  “那个表”之类指代时鼓励它先查清楚；
- 只写明确的日期：相对日期由模型结合消息时间解析，缺失日期的条目不收录；
- 去重：同一日期+时间+标题合并来源，日历只显示一条。

补强（起因是一次漏检：分享卡片把活动时间藏在链接正文里，入库 content 只有
标题行，没有日期 → 按「日期不确定不输出」被整条丢掉，等到当天才发现它从
日历上消失了）：
- **卡片正文**：content 里含 http(s) 链接、且以 [分享]/[卡片…]/[微信小程序]/
  [QQ小程序] 开头时，抓一次链接正文附在该条消息后面再送模型
  （见 `_collect_link_texts`：过 `webpage.validate_url` 的 SSRF 白名单、
  限时限长、24 小时本地缓存、每轮限 5 条；抓不到就按原样送，绝不丢消息、
  绝不因为抓取失败中断整轮）；
- **跨天**：每行带 `MM-DD HH:MM`，相对日期按**该行**日期换算——单日窗口下
  每行日期都一样，行为与之前等价（补抽历史时才真的跨天）；
- **补抽**：`extract_day()` / `backfill()` 按日期区间重抽历史消息，
  只写 schedule_items，**不推进 last_msg_id**、不动 schedule_chat.state.json。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .db import Database
from .llm.agent import _looks_unsupported
from .llm.client import LLMError, build_client, llm_ready
from .llm.tools import TOOL_SCHEMAS, ToolRunner
from . import watchlist

log = logging.getLogger("chat_schedule")

_LOCK = threading.Lock()
_ALLOWED_TOOLS = {"search_chat", "search_knowledge", "recent_chat", "read_document", "kb_overview"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

# ---- 卡片正文抓取（2026-10-31）--------------------------------------------
# 约定：入库 content 里出现 http(s) 链接就说明「正文在链接里」。
# sync-ingest 的解析改动会把卡片链接追加在正文末尾（`[分享]标题 <URL>`），
# 这里按同一约定独立实现，不等它也能工作（库里没有就什么都不抓）。
_CARD_PREFIXES = ("[分享]", "[卡片", "[微信小程序]", "[QQ小程序]")
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】」』]+")
_LINK_MAX_CHARS = 3000          # 缓存与 prompt 里最多带多少字正文
_LINK_MAX_BYTES = 2 << 20       # 抓取字节上限 2MB（网页/知识库那边是 8MB）
_LINK_FETCH_TIMEOUT = 6.0       # 单条链接的墙钟上限（秒），超时按失败处理
_LINK_FETCH_MAX = 5             # 每轮最多抓几条新链接，剩下的留到下轮
_LINK_TTL_OK = 24 * 3600        # 抓成功缓存 24 小时
_LINK_TTL_FAIL = 30 * 60        # 抓失败（含公众号风控验证页）缓存 30 分钟再试
_LINK_MIN_TEXT = 80             # 正文短于这个字数就当没抓到（验证页/空壳页）
# 公众号风控页的特征词：mp.weixin.qq.com 对程序常返回「环境异常」验证页。
_LINK_BLOCK_MARKERS = ("环境异常", "访问过于频繁", "请完成验证", "去验证",
                       "该内容已被发布者删除", "此内容因违规无法查看")
_FETCH_GUARD = threading.Lock()  # 串行化抓取，避免多线程同时改网页模块的限额

# 置信度语义（三处 prompt 统一：录音 recording_ai / 文档 file_schedule / 群聊 chat_schedule；
# 前端把 <0.6 的条目标成「待确认」，所以别为了凑数把置信度抬高）：
#   0.85–1.0  有明确日期，或有不可变截止（通知原文写明「12月24日随堂考试」）
#   0.7–0.85  事项明确，时间需要推算（「明天」「下次课」）
#   0.5–0.7   事项明确，但时间或地点模糊
#   <0.5      不要输出（宁可漏，不要编）
PROMPT = """你是群信息整理助手。下面给你 QQ 群的消息，按时间排序；每条开头方括号里是数据库消息 id，紧接着的 MM-DD HH:MM 是**该条消息的发送日期与时刻**（同一批里可能跨天，以每行自己的日期为准）。有的消息后面还会跟着 `⟦卡片正文⟧`，那是这条消息里链接指向的网页正文（QQ 卡片本身只有一个标题），里面的时间、地点、报名方式同样有效。

任务：从中找出**之后还需要本人去做、或本人必须到场参加**的日程/截止/活动，写成 JSON 数组。

**输出判据**（只收下面两类，其余一律不要输出）：
A. 之后还需要本人去做的事：作业、预习、复习、报名、提交材料、缴费、组队、准备物品等；
B. 有明确时间地点、本人需要到场参加的活动：考试、班会、例会、讲座、比赛、体检、集体活动等。

**不要输出**：
① 课上/现场当场就能完成、当场交掉的（「现在把这张表填了交上来」「随堂小册十分钟做完」）；
② 已经发生、且没有后续动作的纯告知；
③ 纯寒暄、纯提醒（「记得带书」这类没有具体事项的）；
④ 同一件事的多条重复消息（只输出一条，quote 选最完整的那句）。

**日期**：
- 必须给出明确日期（YYYY-MM-DD）；**日期不确定就不要输出**——宁可漏，不要编。
  只有「大概这周」「过两天」而没有具体日期的，不要硬凑一个日期。
- 相对日期（明天、后天、下周三、本周五、月底前）必须按**该行开头的 MM-DD HH:MM** 换算，
  一批消息里各行的日期可能不同，每行只用自己的日期当基准；不许拿别的日子当基准，
  也不许把当天的消息当成「从别处听到的旧消息」。
- 年份按该行日期所在年份补齐（参考日期 {today} 只用来判断年份，不代表所有消息都是那一天）。
- 换算依据要写进 detail，例如「按 10-30 的消息推算」——日期是从哪条消息推出来的，就写清那一天。
- 换算不出来（消息里没有任何可确定的日期线索）就**丢弃该条**，不要猜成今天。
- 纯闲聊、表情、接龙、已过期事项不要收录。

规则：
1. 如果消息里提到“老时间”“那个表”“上次说的”等指代，或你无法确定具体日期/要求，**先调用工具查**（search_chat 搜索相关聊天、search_knowledge 查知识库文件、read_document 读文档、recent_chat 看最近消息），查清楚再写；查不到就降低 confidence，并在 detail 里说明不确定点。
2. 同一件事如果在多个群/多条消息出现，只输出一条，quote 选最完整的那句，并在 msg_ids 里列出相关消息 id。
3. 每条必须包含：
   - date: "YYYY-MM-DD"
   - time: "HH:MM" 或 ""（没有具体时间就空）
   - title: 不超过 30 字的短标题（如“提交助学金申请表”）
   - detail: 需要做什么、地点、对象、注意点；相对日期要写出推算依据
   - quote: 原文中最能支撑该日程的一句话（不超过 100 字）
   - msg_ids: 相关消息的**数据库整数 id 数组**（例如 [12345, 12346]），必须来自上面每行开头的方括号；**不要填时间字符串**
   - confidence: 0~1，按下面的档位给，**不要为了凑数抬高**：
     · 0.85–1.0：有明确日期或不可变截止（通知原文写明日期/时间）；
     · 0.7–0.85：事项明确，时间需要推算（「明天」「下次课」）；
     · 0.5–0.7：事项明确，但时间或地点模糊；
     · <0.5：不要输出这条。
4. 不要编造日期、地点或要求；不确定就写清楚不确定在哪里，并给低 confidence。
5. 只输出 JSON 数组，不要 Markdown、不要解释。没有可收录的事项就输出 []。

参考日期：{today}（用来补全年份；每行开头的 MM-DD HH:MM 才是该条消息的真实发送日期）
白名单群：{groups}
"""

_CONF_FALLBACK = 0.6   # 模型没给/给坏时兜底：不等于「不确定」，但要能从日志里发现 prompt 退化
_CONF_MIN = 0.5        # 低于此值不入库


def _confidence(it: dict, title: str) -> float:
    """取条目置信度：给了合法数值就用原值，否则 0.6 兜底并 warning。

    三处（recording_ai / file_schedule / chat_schedule）的常量、日志文案必须一致，
    改动时三份一起改；_local/tmp/test_sched_rules.py 会校验三者是否同步。
    返回 < _CONF_MIN 的条目调用方不要入库。
    """
    raw = it.get("confidence", None)
    try:
        conf = float(raw)
    except (TypeError, ValueError):
        conf = None
    if conf is None or not (0.0 <= conf <= 1.0):
        log.warning("日程条目缺少置信度，按 0.6 处理：%s", title)
        return _CONF_FALLBACK
    return conf


def _norm(s: str) -> str:
    return re.sub(r"[\s，。；：、,.;:!?！？()（）\[\]【】\"'“”]+", "", s or "").lower()


def _dedupe_key(date: str, tm: str, title: str) -> str:
    return f"{date}|{tm or ''}|{_norm(title)[:40]}"


def _today_bounds() -> tuple[float, float]:
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), now.timestamp()


def _state_path(cfg: Settings):
    return cfg.resolve("data/schedule_chat.state.json")


def _load_state(cfg: Settings) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(cfg: Settings, state: dict) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _format_messages(rows: list[dict], max_chars: int,
                     link_texts: dict[int, str] | None = None
                     ) -> tuple[str, dict[int, dict]]:
    """把消息行拼成 prompt 正文。

    每行形如 `[123] 群名 10-30 14:32 昵称: 内容`；日期必须逐行给——补抽历史时
    一批消息会跨天，只写 HH:MM 会让模型把「明天」算到错误的基准日上。
    `link_texts` 是 msg_id → 卡片链接正文，附在该行末尾（见 `_collect_link_texts`）。
    """
    lines: list[str] = []
    by_id: dict[int, dict] = {}
    total = 0
    for m in reversed(rows):
        mid = int(m["id"])
        ts = datetime.fromtimestamp(int(m["ts"]))
        gname = m.get("group_name") or f"群{m['group_id']}"
        content = (m.get("content") or "").replace("\n", " ").strip()
        url = _find_link(content)
        if len(content) > 300:
            content = content[:300] + "…"
            # 卡片链接通常正好落在被截掉的部分，补回来，否则模型看不到正文出处
            if url and url not in content:
                content += f" <{url}>"
        # user_id 不在 chat_messages_between 的返回列里；昵称可能为空（系统消息/匿名），
        # 这里只能用 .get 兜底，否则生产数据一遇到空昵称就 KeyError（2026-10-31 实测）。
        nick = m.get("nickname") or str(m.get("user_id") or "") or "未知"
        line = (f"[{mid}] {gname} {ts:%m-%d %H:%M} {nick}: {content}")
        text = (link_texts or {}).get(mid) or ""
        if text:
            line += " ⟦卡片正文⟧ " + _one_line(text)
        if total + len(line) > max_chars and lines:
            break
        lines.append(line)
        total += len(line) + 1
        by_id[mid] = m
    lines.reverse()
    return "\n".join(lines), by_id


def _one_line(text: str) -> str:
    """压成一行：卡片正文里的换行/空行对模型没用，还会打乱「一行一条消息」。"""
    return re.sub(r"\s+", " ", (text or "")).strip()[:_LINK_MAX_CHARS]


def _find_link(content: str) -> str:
    """取 content 里第一个 http(s) 链接（正则已排除中文标点尾巴）。"""
    m = _URL_RE.search(content or "")
    return m.group(0) if m else ""


def _looks_like_card(content: str) -> bool:
    """是否「分享卡片」消息：只看开头特征，位置/表情包/合并转发之类不抓。"""
    head = (content or "").lstrip()
    return any(head.startswith(p) for p in _CARD_PREFIXES)


def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()


def _link_cache_path(cfg: Settings) -> Path:
    return cfg.resolve("data/link_cache.json")


def _load_link_cache(cfg: Settings) -> dict:
    """读链接缓存；文件缺失/损坏一律当空缓存（下次保存自动重建，能自愈）。"""
    try:
        data = json.loads(_link_cache_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_link_cache(cfg: Settings, cache: dict) -> None:
    """原子写（tmp + replace），顺手丢过期项并限制 800 条，避免无界增长。"""
    now = time.time()
    keep = {k: v for k, v in cache.items()
            if isinstance(v, dict) and now - float(v.get("ts") or 0) < _LINK_TTL_OK}
    if len(keep) > 800:
        keep = dict(sorted(keep.items(),
                           key=lambda kv: float(kv[1].get("ts") or 0),
                           reverse=True)[:800])
    p = _link_cache_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:  # 缓存写不进去不该影响抽取
        log.info("链接缓存写入失败（忽略）：%s", e)


def _fetch_link_text(url: str) -> tuple[bool, str]:
    """抓一个链接的正文，返回 (是否成功, 正文)。**失败一律静默**，绝不抛异常。

    三道闸：
    1. `webpage.validate_url` —— SSRF 白名单（只放行公网 http/https，拒本机/内网）；
    2. 工作线程 join 超时 `_LINK_FETCH_TIMEOUT` —— `fetch_markdown` 自带 25s 超时太慢，
       每轮同步不能被一条外链拖住；
    3. 2MB 字节上限 + 3000 字正文上限（`_LINK_MAX_BYTES` 临时改网页模块的读取上限，
       见下方注释）。

    公众号（mp.weixin.qq.com）对程序常返回「环境异常」验证页，这类结果按**失败**处理、
    不缓存成成功：用户在电脑上点开能看到，程序抓不到就抓不到，属预期行为不是 bug
    （缓存 30 分钟后会再试一次，见 `_LINK_TTL_FAIL`）。
    """
    from . import webpage  # 局部导入：只有真的遇到卡片链接才用到 requests/bs4

    ok, why = webpage.validate_url(url)
    if not ok:
        log.info("卡片链接未通过白名单，跳过：%s（%s）", url[:160], why)
        return False, ""

    box: dict = {}

    def work() -> None:
        # fetch_markdown 没有 timeout/限长参数，这里临时改模块级上限（8MB → 2MB）。
        # 风险：web 进程里别处也在用 fetch_markdown，几秒的窗口内会共用 2MB 上限；
        # 由 _FETCH_GUARD 保证同一时刻只有本模块在改，且 finally 一定还原。
        # 实测记录见开发时的自测脚本。
        old_max = getattr(webpage, "_MAX_HTML", None)
        try:
            if old_max is not None:
                webpage._MAX_HTML = _LINK_MAX_BYTES
            box["res"] = webpage.fetch_markdown(url, media_dir=None,
                                                download_images=False)
        except Exception as e:  # noqa: BLE001
            box["err"] = f"{type(e).__name__}: {e}"
        finally:
            if old_max is not None:
                webpage._MAX_HTML = old_max

    with _FETCH_GUARD:
        th = threading.Thread(target=work, name="link-fetch", daemon=True)
        th.start()
        th.join(_LINK_FETCH_TIMEOUT)
        alive = th.is_alive()
    if alive:
        log.info("卡片链接抓取超时（>%.1fs），按失败处理：%s",
                 _LINK_FETCH_TIMEOUT, url[:160])
        return False, ""
    if box.get("err"):
        log.info("卡片链接抓取失败：%s（%s）", url[:160], box["err"])
        return False, ""
    res = box.get("res") or {}
    if not res.get("ok"):
        log.info("卡片链接抓取失败：%s（%s）", url[:160], res.get("error") or "")
        return False, ""
    text = _one_line(res.get("markdown") or "")
    if len(text) < _LINK_MIN_TEXT:
        log.info("卡片链接正文过短（%d 字），当没抓到：%s", len(text), url[:160])
        return False, ""
    for marker in _LINK_BLOCK_MARKERS:
        if marker in text:
            log.info("卡片链接是风控/失效页（含「%s」），当没抓到：%s",
                     marker, url[:160])
            return False, ""
    return True, text


def _collect_link_texts(cfg: Settings, rows: list[dict]) -> dict[int, str]:
    """挑出带链接的分享卡片并抓正文，返回 `msg_id → 正文`。

    只处理这一轮真要送进 prompt 的消息（最新优先）；每轮最多抓 `_LINK_FETCH_MAX`
    条新链接，超预算的留到下轮（下一轮命中同一批消息时会再挑到）。
    命中缓存直接复用，不占预算。**任何异常都在这里兜住**：最坏情况返回空字典，
    消息按原样送进 prompt。
    """
    out: dict[int, str] = {}
    try:
        sc = getattr(cfg, "schedule", None) or {}
        try:
            budget = int(sc.get("chat_link_fetch_max", _LINK_FETCH_MAX))
        except (TypeError, ValueError):
            budget = _LINK_FETCH_MAX
        if budget <= 0:
            return out
        cache = _load_link_cache(cfg)
        now = time.time()
        used = 0
        dirty = False
        for m in reversed(rows):
            content = m.get("content") or ""
            if not _looks_like_card(content):
                continue
            url = _find_link(content)
            if not url:
                continue
            key = _cache_key(url)
            ent = cache.get(key)
            if isinstance(ent, dict):
                ttl = _LINK_TTL_OK if ent.get("ok") else _LINK_TTL_FAIL
                if now - float(ent.get("ts") or 0) < ttl:
                    if ent.get("ok") and ent.get("text"):
                        out[int(m["id"])] = str(ent["text"])
                    continue
            if used >= budget:
                continue          # 预算用完，这条留到下轮
            used += 1
            ok, text = _fetch_link_text(url)
            cache[key] = {"ts": now, "ok": ok, "text": text if ok else ""}
            dirty = True
            if ok:
                out[int(m["id"])] = text
        if dirty:
            _save_link_cache(cfg, cache)
        if out:
            log.info("卡片正文：%d 条消息带上了链接正文（本轮新抓 %d 条）",
                     len(out), used)
    except Exception as e:  # noqa: BLE001
        log.warning("卡片正文采集异常（忽略，消息照原样送模型）：%s: %s",
                    type(e).__name__, e)
    return out


def _call_llm(cfg: Settings, prompt: str, db: Database) -> str:
    chat = build_client(cfg, "summary")
    tools = [t for t in TOOL_SCHEMAS if t["function"]["name"] in _ALLOWED_TOOLS]
    runner = ToolRunner(db, cfg)
    messages = [{"role": "system", "content": prompt}]
    for _round in range(3):
        text = ""
        calls: list[dict] = []
        try:
            for ev in chat.chat_stream_events(messages, tools=tools or None):
                if ev["type"] == "delta":
                    text += ev["text"]
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
        except LLMError as e:
            if tools and _looks_unsupported(str(e)):
                log.info("模型不支持工具调用，退回纯文本抽取：%s", e)
                return chat.chat(messages, temperature=0.1, max_tokens=6000)
            raise
        if not calls:
            return text
        messages.append({
            "role": "assistant",
            "content": text or "",
            "tool_calls": [{
                "id": c.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": c.get("name") or "",
                             "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)},
            } for i, c in enumerate(calls)],
        })
        for i, c in enumerate(calls):
            res = runner.run(c.get("name") or "", c.get("arguments") or {})
            messages.append({
                "role": "tool",
                "tool_call_id": c.get("id") or f"call_{i}",
                "content": res.content or "（没有查到内容）",
            })
    messages.append({"role": "user", "content": "请只输出最终 JSON 数组，不要解释。"})
    return chat.chat(messages, temperature=0.1, max_tokens=6000)


def _match_source_rows(ids: list, quote: str, msg_time: str,
                       by_id: dict[int, dict]) -> list[dict]:
    """把模型给的 msg_ids 映射回消息行。

    模型偶尔会填时间字符串、或直接省略有效 id；依次尝试：
    1) 纯数字 id；2) 日期时间/时分字符串就近匹配；3) 用 quote 文本匹配；
    4) 只有一条消息时兜底。
    """
    rows = list(by_id.values())
    out: list[dict] = []
    seen: set[int] = set()

    def add(m: dict) -> None:
        mid = int(m["id"])
        if mid not in seen:
            seen.add(mid)
            out.append(m)

    for x in ids:
        s = str(x).strip()
        if s.isdigit() and int(s) in by_id:
            add(by_id[int(s)])
    if out:
        return out

    # 时间字符串：接受 "2026-10-30 11:16:11" / "11:16" 之类
    for x in ids:
        s = str(x)
        m = re.search(r"(\d{2}):(\d{2})(?::(\d{2}))?", s)
        if not m:
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        for row in rows:
            dt = datetime.fromtimestamp(int(row["ts"]))
            if dt.hour == hh and dt.minute == mm:
                add(row)
                break
    if out:
        return out

    # quote 文本匹配
    qn = _norm(quote)
    if len(qn) >= 6:
        for row in rows:
            if qn in _norm(row.get("content") or ""):
                add(row)
                break
    if out:
        return out

    # 单条消息或 msg_time 唯一匹配时兜底
    if len(rows) == 1:
        add(rows[0])
    elif msg_time:
        for row in rows:
            if datetime.fromtimestamp(int(row["ts"])).strftime("%H:%M") == msg_time[:5]:
                add(row)
                break
    return out


def _parse_items(raw: str) -> list[dict]:
    text = (raw or "").strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        lo, hi = text.find("["), text.rfind("]")
        if lo < 0 or hi <= lo:
            lo, hi = text.find("{"), text.rfind("}")
        if lo < 0 or hi <= lo:
            raise
        data = json.loads(text[lo:hi + 1])
    if isinstance(data, dict):
        data = data.get("items") or []
    return data if isinstance(data, list) else []


def _existing_keys(db: Database, date: str) -> tuple[set[str], set[str]]:
    """某天已存在日程的 (dedupe_key 集合, 来源 id 集合)。--dry 判「已存在」用。"""
    keys: set[str] = set()
    ids: set[str] = set()
    try:
        for ex in db.list_schedule_items(start=date, end=date):
            keys.add(str(ex.get("dedupe_key") or ""))
            ids.add(str(ex.get("source_id") or ""))
            for s in (ex.get("sources") or []):
                ids.add(str(s.get("id") or ""))
    except Exception:  # noqa: BLE001
        pass
    return keys, ids


def _apply_items(db: Database, items: list, by_id: dict[int, dict],
                 *, dry_run: bool = False) -> dict:
    """把模型给的条目写库（或只统计）。逐条校验日期/标题/置信度/来源消息。

    dry_run=True：只判断「会新增 / 已存在」，一个字节都不写（`--dry` 用）。
    幂等性靠 `db.upsert_schedule_item` 的 dedupe_key + 同日标题相似度合并 + 墓碑；
    这里再加一层「来源消息已经挂在别的日程上 → 复用那条的 dedupe_key」，避免同一批
    消息重跑时模型换个标题就在日历上多出一条（见下面 reuse_key 注释）。
    """
    stat = {"inserted": 0, "merged": 0, "planned": 0, "exists": 0,
            "no_source": 0, "low_conf": 0, "dismissed": 0}
    for it in items:
        if not isinstance(it, dict):
            continue
        date = str(it.get("date") or "").strip()
        if not _DATE_RE.match(date):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        tm = str(it.get("time") or "").strip()
        if tm and not _TIME_RE.match(tm):
            tm = ""
        ids = it.get("msg_ids") or []
        if not isinstance(ids, list):
            ids = [ids]
        quote = str(it.get("quote") or "").strip()[:200]
        src_rows = _match_source_rows(ids, quote, tm, by_id)
        if not src_rows:
            log.warning("日程「%s」找不到来源消息 ids=%r", title, ids)
            stat["no_source"] += 1
            continue
        src_rows.sort(key=lambda m: int(m["ts"]))
        m0 = src_rows[0]
        conf = _confidence(it, title)
        if conf < _CONF_MIN:
            log.info("丢弃低置信日程（%.2f < %.2f）：%s", conf, _CONF_MIN, title)
            stat["low_conf"] += 1
            continue
        detail = str(it.get("detail") or "").strip()
        source = {
            "type": "chat",
            "id": f"{m0['group_id']}:{m0['id']}",
            "ref": f"/msg/{m0['id']}",
            "quote": quote,
            "label": m0.get("group_name") or f"群{m0['group_id']}",
        }
        sources = []
        for m in src_rows:
            sources.append({
                "type": "chat",
                "id": f"{m['group_id']}:{m['id']}",
                "ref": f"/msg/{m['id']}",
                "quote": quote,
                "label": m.get("group_name") or f"群{m['group_id']}",
            })
        # 同一条消息重跑时不要产生第二条日程：上一轮可能已经写库但没来得及推进
        # last_msg_id（中途异常/超时），重跑时模型换个标题 → dedupe_key 不同 → 日历出现两条
        # （例：同一件事被抽出两种措辞的标题，如「参加某活动」与「某活动」）。
        reuse_key = ""
        try:
            for ex in db.list_schedule_items(start=date, end=date):
                seen_ids = {str(s.get("id") or "") for s in (ex.get("sources") or [])}
                seen_ids.add(str(ex.get("source_id") or ""))
                if source["id"] in seen_ids:
                    reuse_key = str(ex.get("dedupe_key") or "")
                    break
        except Exception:  # noqa: BLE001
            reuse_key = ""
        if dry_run:
            keys, srcs = _existing_keys(db, date)
            if (reuse_key or _dedupe_key(date, tm, title) in keys
                    or source["id"] in srcs):
                stat["exists"] += 1
            else:
                stat["planned"] += 1
            continue
        if reuse_key:
            rid = db.upsert_schedule_item(date=date, time=tm, title=title,
                                          detail=detail, source=source,
                                          confidence=conf, dedupe_key=reuse_key)
            if not rid:
                stat["dismissed"] += 1      # 命中用户墓碑：不复活，也不算 merged
                continue
            for s in sources[1:]:
                db.upsert_schedule_item(date=date, time=tm, title=title,
                                        detail="", source=s, confidence=conf,
                                        dedupe_key=reuse_key)
            stat["merged"] += 1
            continue
        rid = db.upsert_schedule_item(
            date=date, time=tm, title=title, detail=detail, source=source,
            confidence=conf, dedupe_key=_dedupe_key(date, tm, title),
        )
        if not rid:
            stat["dismissed"] += 1          # 用户删过（墓碑）：不复活
            continue
        # 合并同一条日程的其他消息来源
        for s in sources[1:]:
            db.upsert_schedule_item(date=date, time=tm, title=title,
                                    detail="", source=s, confidence=conf,
                                    dedupe_key=_dedupe_key(date, tm, title))
        stat["inserted"] += 1
    return stat


def _extract_rows(db: Database, cfg: Settings, rows: list[dict], *,
                  ref_day: str, dry_run: bool = False) -> dict:
    """把一批消息喂给模型抽日程并（可选）写库。**不碰 last_msg_id / state 文件。**

    ref_day：这批消息的参考日期（YYYY-MM-DD），只用于补全年份；
    真正的相对日期基准是每行开头的 MM-DD HH:MM。
    """
    sc = getattr(cfg, "schedule", None) or {}
    if not rows:
        return {"ok": True, "messages": 0, "items": 0, "inserted": 0}
    link_texts = _collect_link_texts(cfg, rows)
    dump, by_id = _format_messages(rows, int(sc.get("chat_max_chars", 90000)),
                                   link_texts)
    names = sorted({m.get("group_name") or str(m["group_id"]) for m in rows})
    # 2026-10-31 修：`dump` 以前只算不用——拼好的消息行从来没进过 system prompt，
    # 模型只能自己调 recent_chat/search_chat 去捞（捞多少、捞到哪天全凭它），
    # 于是挂在消息行上的 `⟦卡片正文⟧` 永远送不到模型眼前。
    prompt = (PROMPT.format(today=ref_day, groups="、".join(names[:60]))
              + f"\n\n消息如下（共 {len(rows)} 条，按时间正序）：\n" + dump)
    raw = _call_llm(cfg, prompt, db)
    items = _parse_items(raw)
    stat = _apply_items(db, items, by_id, dry_run=dry_run)
    if dry_run:
        log.info("日程抽取（试运行）：%d 条消息 → %d 条日程（将新增 %d，已存在 %d，"
                 "无来源 %d，低置信 %d，墓碑 %d）",
                 len(rows), len(items), stat["planned"], stat["exists"],
                 stat["no_source"], stat["low_conf"], stat["dismissed"])
    else:
        log.info("日程抽取：%d 条消息 → %d 条日程（新增/合并 %d，复用已有 %d）",
                 len(rows), stat["inserted"], len(items), stat["merged"])
    out = {"ok": True, "messages": len(rows), "items": len(items),
           "raw": raw[:500], "links": len(link_texts), "prompt_chars": len(dump)}
    out.update(stat)
    return out


def extract_today(db: Database, cfg: Settings) -> dict:
    """抽取当天白名单群的日程并写库。返回统计信息。"""
    sc = getattr(cfg, "schedule", None) or {}
    if not sc.get("enabled", True) or not sc.get("chat_enabled", True):
        return {"ok": False, "error": "schedule 抽取未启用"}
    if not llm_ready(cfg, "summary"):
        return {"ok": False, "error": "未配置可用的 AI 模型"}
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "上一轮日程抽取仍在进行"}
    try:
        start, end = _today_bounds()
        groups = watchlist.effective(cfg)["groups"]
        rows = db.chat_messages_between(
            start, end, group_ids=groups or None,
            limit=int(sc.get("chat_max_messages", 6000)),
        )
        if not rows:
            return {"ok": True, "messages": 0, "items": 0, "inserted": 0}
        max_id = max(int(m["id"]) for m in rows)
        state = _load_state(cfg)
        if int(state.get("last_msg_id") or 0) >= max_id:
            return {"ok": True, "skipped": "无新消息", "messages": len(rows),
                    "items": 0, "inserted": 0}
        res = _extract_rows(db, cfg, rows,
                            ref_day=datetime.now().strftime("%Y-%m-%d"))
        if res.get("ok"):
            state["last_msg_id"] = max_id
            state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _save_state(cfg, state)
        return res
    except Exception as e:  # noqa: BLE001
        log.exception("日程抽取失败：%s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _LOCK.release()


# ---- 按日期区间补抽（2026-10-31）------------------------------------------
# 起因：last_msg_id 游标只会往前走。10-29 那条分享卡片当时没抽出日程，等代码
# 修好（能抓卡片正文）时游标早已越过它，历史消息里的日程就再也拿不回来。
# backfill 绕过游标：按天重查、重抽，走同一套 upsert（幂等）。

def _day_bounds(day: str) -> tuple[float, float]:
    """某天 00:00:00 ≤ ts < 次日 00:00:00（本地时区）。"""
    d = datetime.strptime(day, "%Y-%m-%d")
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def extract_day(db: Database, cfg: Settings, day: str, *,
                limit: int = 0, dry_run: bool = False) -> dict:
    """重抽**某一天**的白名单群消息。不推进 last_msg_id、不改 state 文件。"""
    sc = getattr(cfg, "schedule", None) or {}
    if not sc.get("enabled", True) or not sc.get("chat_enabled", True):
        return {"ok": False, "error": "schedule 抽取未启用"}
    if not llm_ready(cfg, "summary"):
        return {"ok": False, "error": "未配置可用的 AI 模型"}
    if not _DATE_RE.match(day or ""):
        return {"ok": False, "error": f"日期格式不对：{day!r}（要 YYYY-MM-DD）"}
    start, end = _day_bounds(day)
    groups = watchlist.effective(cfg)["groups"]
    rows = db.chat_messages_between(
        start, end, group_ids=groups or None,
        limit=int(limit or sc.get("chat_max_messages", 6000)),
    )
    if not rows:
        return {"ok": True, "day": day, "messages": 0, "items": 0, "inserted": 0}
    return _extract_rows(db, cfg, rows, ref_day=day, dry_run=dry_run)


def backfill(db: Database, cfg: Settings, date_from: str, date_to: str, *,
             limit: int = 0, dry_run: bool = False) -> dict:
    """按天补抽 [date_from, date_to] 的群聊日程。

    幂等：同一天重复跑不会产生重复条目——dedupe_key（date|time|规范标题）命中即合并、
    同日标题相似度 ≥0.72 也会并回原条目（db._similar_schedule_row）、用户手动编辑过的
    行（manual=1）只并来源、用户删过的有墓碑不复活。不推进 last_msg_id。
    """
    if not _DATE_RE.match(date_from or "") or not _DATE_RE.match(date_to or ""):
        return {"ok": False, "error": "日期格式不对，要 YYYY-MM-DD"}
    try:
        d0 = datetime.strptime(date_from, "%Y-%m-%d")
        d1 = datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError as e:
        return {"ok": False, "error": f"日期解析失败：{e}"}
    if d0 > d1:
        return {"ok": False, "error": f"起始日期晚于结束日期：{date_from} > {date_to}"}
    span = (d1 - d0).days + 1
    if span > 62:
        return {"ok": False, "error": f"一次最多补抽 62 天，当前 {span} 天"}
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "上一轮日程抽取仍在进行"}
    total = {"days": 0, "days_with_messages": 0, "messages": 0, "items": 0,
             "inserted": 0, "merged": 0, "planned": 0, "exists": 0,
             "no_source": 0, "low_conf": 0, "dismissed": 0, "failed": 0}
    errors: list[str] = []
    try:
        day = d0
        while day <= d1:
            label = day.strftime("%Y-%m-%d")
            day += timedelta(days=1)
            total["days"] += 1
            try:
                res = extract_day(db, cfg, label, limit=limit, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001  单天失败不影响其它天
                log.warning("补抽 %s 失败：%s: %s", label, type(e).__name__, e)
                errors.append(f"{label}: {type(e).__name__}: {e}")
                total["failed"] += 1
                continue
            if not res.get("ok"):
                log.info("补抽 %s 跳过：%s", label, res.get("error") or "")
                errors.append(f"{label}: {res.get('error')}")
                total["failed"] += 1
                continue
            n_msg = int(res.get("messages") or 0)
            total["messages"] += n_msg
            total["items"] += int(res.get("items") or 0)
            for k in ("inserted", "merged", "planned", "exists",
                      "no_source", "low_conf", "dismissed"):
                total[k] += int(res.get(k) or 0)
            if n_msg:
                total["days_with_messages"] += 1
                if dry_run:
                    log.info("补抽 %s：%d 条消息 → %d 条日程"
                             "（试运行，未写库；已存在 %d）", label, n_msg,
                             int(res.get("planned") or 0),
                             int(res.get("exists") or 0))
                else:
                    log.info("补抽 %s：%d 条消息 → %d 条日程", label, n_msg,
                             int(res.get("inserted") or 0))
            else:
                log.info("补抽 %s：0 条消息", label)
        total["ok"] = True
        total["dry_run"] = bool(dry_run)
        total["from"] = date_from
        total["to"] = date_to
        if errors:
            total["errors"] = errors
        log.info("补抽结束 %s~%s：%d 天有消息，%d 条消息 → 新增 %d 条"
                 "（复用 %d，已存在 %d，失败 %d 天）",
                 date_from, date_to, total["days_with_messages"],
                 total["messages"], total["inserted"], total["merged"],
                 total["exists"], total["failed"])
        return total
    finally:
        _LOCK.release()
