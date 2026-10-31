"""录音（会议 / 课堂 / 访谈）的 AI 总结与日程抽取。

输入是带置信度标记的转写文本；输出：
- summary_md: 结构化总结（带来源与时间码）
- schedule:  [{date, time, title, detail, source_time, source_quote, confidence}]
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .config import load_config
from .llm.client import LLMError, build_client, llm_ready

log = logging.getLogger(__name__)

_MAX_CHARS = 50000

SUMMARY_PROMPT = """你是笔记整理助手。请阅读这段录音转写，输出一份中文总结。

要求：
1. 先给 3–6 条「本讲要点」，每条一句话，尽量带定义、公式名称或结论。
2. 再给「脉络」：按时间顺序分 3–8 段概括讲解过程，保留关键例题/反例。
3. 最后给「需要课后确认」：只列转写里标有【中】【低】置信或明显公式听写不清的地方，说明为什么需要确认。
4. 不要编造转写里没有的内容；不确定就写“转写未明确”。
5. 引用时间码格式 [HH:MM:SS]，允许在条目末尾标注。
6. 直接输出 Markdown，不要额外解释。
"""

# 置信度语义（三处 prompt 统一：录音 recording_ai / 文档 file_schedule / 群聊 chat_schedule；
# 前端把 <0.6 的条目标成「待确认」，所以别为了凑数把置信度抬高）：
#   0.85–1.0  有明确日期，或有不可变截止（通知原文写明「12月24日随堂考试」）
#   0.7–0.85  事项明确，时间需要推算（「明天」「下次课」）
#   0.5–0.7   事项明确，但时间或地点模糊
#   <0.5      不要输出（宁可漏，不要编）
SCHEDULE_PROMPT = """你是会议/事务助手。请从这段录音转写中抽取“之后还需要本人去做、或本人必须到场参加”的事项。

**输出判据**（只收下面两类，其余一律不要输出）：
A. 之后还需要本人去做的事：作业、预习、复习、报名、提交材料、缴费、组队、准备物品等；
B. 有明确时间地点、本人需要到场参加的活动：考试、班会、例会、讲座、比赛、体检、集体活动等。

**不要输出**：
① 课上当场完成、当场交掉的事——「现在把这张表填了交上来」「随堂小册十分钟做完」；
② 已经发生、且没有后续动作的纯告知；
③ 纯寒暄、纯提醒（「记得带书」这类没有具体事项的）；
④ 同一件事被反复讲了几遍（只输出一条）；
另外也不要抽取纯知识讲解、例题、口号、闲聊。
凡是在当次课/当次现场就能做完的事，一律不输出。

**日期不确定就不要输出**：宁可漏，不要编。转写里只有「大概这周」「下次上课前」而没有
具体日期的，直接不输出，不要把课程日期当成事项日期硬填。

如果转写里出现“上次说的那个表”“老时间”等指代，或日期/对象不确定，
**先调用工具查**（search_chat / search_knowledge / read_document / recent_chat / kb_overview），
查清楚再写；查不到就在 detail 里写明“待确认”，不要编造。

输出严格 JSON（不要 Markdown 代码块）：
{"items":[{"date":"YYYY-MM-DD 或 空","time":"HH:MM 或 空","title":"短标题","detail":"需要做什么","source_time":"[HH:MM:SS]","source_quote":"转写原句（不超过 80 字）","confidence":0.9}]}

confidence 按档位给（**不要为了凑数抬高**）：0.85–1.0 有明确日期或不可变截止；
0.7–0.85 事项明确但时间需要推算（「明天」「下次课」）；0.5–0.7 事项明确但时间/地点模糊；
<0.5 不要输出这条。

若没有事项，输出 {"items": []}。今天日期是 {today}，课程日期是 {course_date}。
"""


def _transcript_for_ai(text: str) -> str:
    text = text.strip()
    if len(text) > _MAX_CHARS:
        return text[:_MAX_CHARS] + "\n…（转写过长，已截断）"
    return text


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"items": []}


def _chat_with_retry(client, messages: list[dict], *, temperature: float,
                     max_tokens: int) -> str:
    """总结/日程调用；Key 刚在设置页恢复时，401 自动重载配置重试一次。"""
    try:
        return client.chat(messages, temperature=temperature,
                           max_tokens=max_tokens)
    except LLMError as e:
        text = str(e)
        if not any(k in text for k in ("401", "Key", "无效", "未授权")):
            raise
        log.warning("总结模型鉴权失败，2 秒后重载配置重试：%s", e)
        time.sleep(2)
        fresh = build_client(load_config(), "summary")
        return fresh.chat(messages, temperature=temperature,
                          max_tokens=max_tokens)


_END_OK = "。！？!?…」』】》）)"


def _looks_truncated(text: str) -> bool:
    """粗略判断 Markdown 是否在句子/列表中途被截断。"""
    t = (text or "").strip()
    if not t:
        return True
    tail = t[-1]
    if tail in "：:，,、；;-—(":
        return True
    return tail not in _END_OK


def _summary_complete(client, messages: list[dict], *,
                      temperature: float, max_tokens: int,
                      max_rounds: int = 4) -> str:
    """生成总结；若 API 因输出上限截断，则让它从中断处续写。"""
    text = _chat_with_retry(client, messages, temperature=temperature,
                            max_tokens=max_tokens)
    for _ in range(max_rounds - 1):
        if not _looks_truncated(text):
            return text.strip()
        tail = text[-400:]
        cont = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": (
                "上一条总结在末尾被截断了。请严格从中断处继续输出剩余的 Markdown，"
                "不要重复已经输出的内容，不要重新开头，也不要解释。\n"
                f"已输出内容的末尾是：\n{tail}"
            )},
        ]
        part = _chat_with_retry(client, cont, temperature=temperature,
                                max_tokens=max_tokens)
        if not part.strip():
            break
        text = text.rstrip() + "\n" + part.strip()
        log.info("总结续写一轮，累计 %d 字符", len(text))
    return text.strip()


def generate(cfg, *, course: str, rec_date: str, transcript: str,
             source_name: str, recording_id: int, db=None) -> dict:
    """调用总结模型，返回 summary / schedule / error。"""
    today = __import__("datetime").date.today().isoformat()
    transcript = _transcript_for_ai(transcript)
    out: dict[str, Any] = {"summary": "", "schedule": [], "error": ""}

    if not llm_ready(cfg, "summary"):
        out["error"] = "总结模型未配置"
        return out
    try:
        client = build_client(cfg, "summary")
        head = (f"分类：{course}\n日期：{rec_date}\n来源：{source_name}\n"
                f"录音编号：{recording_id}\n\n===== 转写开始 =====\n")
        summary = _summary_complete(
            client,
            [{"role": "system", "content": SUMMARY_PROMPT},
             {"role": "user", "content": head + transcript + "\n===== 转写结束 ====="}],
            temperature=0.2, max_tokens=6000,
        )
        out["summary"] = summary.strip()
        raw = ""
        if db is not None:
            try:
                from .chat_schedule import _call_llm as _call_with_tools
                raw = _call_with_tools(
                    cfg,
                    SCHEDULE_PROMPT.replace("{today}", today).replace(
                        "{course_date}", rec_date or today)
                    + "\n\n" + head + transcript + "\n===== 转写结束 =====",
                    db,
                )
            except Exception as e:  # noqa: BLE001
                log.info("录音日程工具调用失败，回退纯文本：%s", e)
                raw = ""
        if not (raw or "").strip():
            raw = _chat_with_retry(
                client,
                [{"role": "system",
                  "content": SCHEDULE_PROMPT.replace("{today}", today).replace(
                      "{course_date}", rec_date or today)},
                 {"role": "user", "content": head + transcript + "\n===== 转写结束 ====="}],
                temperature=0.0, max_tokens=3000,
            )
        data = _extract_json(raw)
        items = data.get("items") if isinstance(data, dict) else None
        out["schedule"] = [x for x in (items or []) if isinstance(x, dict)]
    except LLMError as e:
        out["error"] = f"总结失败：{e}"
    except Exception as e:  # noqa: BLE001
        log.warning("录音总结失败", exc_info=True)
        out["error"] = f"总结失败：{type(e).__name__}: {e}"
    return out
