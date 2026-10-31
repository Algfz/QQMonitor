"""从知识库文档（群文件/PDF/Word/OCR 文本）中抽取日程。

文档在入库时已经统一转成 `data/knowledge/transcripts/*.md`，
这里只做增量扫描（按 mtime 记录状态），挑出含日期/截止/报名等关键词的段落，
交给模型抽成日程，写入 schedule_items（source_type=file）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import Database
from .llm.client import LLMError, build_client, llm_ready

log = logging.getLogger("file_schedule")

_LOCK = threading.Lock()
_KEYWORDS = re.compile(
    r"截止|报名|时间|日期|地点|会议|活动|提交|上交|领取|集合|开始|结束|"
    r"之前|以前|前完成|日程|安排|通知|请于|务必|统一"
)
_DATE_HINT = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}|本周|下周|周[一二三四五六日天]")

# 置信度语义（三处 prompt 统一：录音 recording_ai / 文档 file_schedule / 群聊 chat_schedule；
# 前端把 <0.6 的条目标成「待确认」，所以别为了凑数把置信度抬高）：
#   0.85–1.0  有明确日期，或有不可变截止（通知原文写明「12月24日随堂考试」）
#   0.7–0.85  事项明确，时间需要推算（「明天」「下次课」）
#   0.5–0.7   事项明确，但时间或地点模糊
#   <0.5      不要输出（宁可漏，不要编）
PROMPT = """你在阅读一份群文件/课程文档，请从中抽取**之后还需要本人去做、或本人必须到场参加**的日程。
文档名：{name}
文档记录日期：{doc_date}
今天是：{today}

**输出判据**（只收下面两类，其余一律不要输出）：
A. 之后还需要本人去做的事：作业、预习、复习、报名、提交材料、缴费、组队、准备物品等；
B. 有明确时间地点、本人需要到场参加的活动：考试、班会、例会、讲座、比赛、体检、集体活动等。

**不要输出**：
① 当次课/当次现场就能做完、当场交掉的事（「现在把这张表填了交上来」「随堂小册十分钟做完」）；
② 已经发生、且没有后续动作的纯告知；
③ 纯寒暄、纯提醒（「记得带书」这类没有具体事项的）；
④ 同一件事的重复表述（只输出一条）。
凡是在当次课/当次现场就能做完的事，一律不输出。

**日期不确定就不要输出**：宁可漏，不要编。只有「大概这周」「下周找个时间」而没有
具体日期的，不要硬凑一个日期，直接不输出。

**换算基准**：文中“今天”“明天”“后天”“本周三”“下周一”“下次课”等相对说法，
一律以**文档记录日期 {doc_date}** 为基准换算，**不要**以“今天是 {today}”为基准。
文档记录日期是这份文档产生的那一天（录音转写就是录制当天）。换算出来已经过去的日期，
也如实输出那个过去的日期，不要挪到最近。

只输出 JSON 数组，每项字段：
- date: YYYY-MM-DD（必须能确定；没有明确日期就不要输出）
- time: HH:MM 或 ""
- title: 不超过 30 字
- detail: 需要做什么/地点/对象/注意事项
- quote: 支撑该日程的原文一句（不超过 100 字）
- confidence: 0~1，按下面的档位给，**不要为了凑数抬高**：
  · 0.85–1.0：有明确日期或不可变截止（通知原文写明日期/时间）；
  · 0.7–0.85：事项明确，时间需要推算（「明天」「下次课」）；
  · 0.5–0.7：事项明确，但时间或地点模糊；
  · <0.5：不要输出这条。

规则：
1. 只收录「之后还需要本人去做」或「有明确时间地点需到场」的事项；截止日期、活动时间、会议时间、报名截止都算。
2. 不要编造；文档里没有日期就不要输出。
3. 同一件事只输出一条。
4. date 必须落在文档记录日期前后合理范围内（早不超过 7 天、晚不超过 120 天），否则不要输出。
5. 只输出 JSON 数组；没有就输出 []。

文档内容：
{content}
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

# 文档「记录日期」的三个来源，按可信度排序：
#   1) 转写稿 front matter 的 date:（录音当天）
#   2) 文件名里的 2026-01-05 / 20260105 / IMG20260105... / VID_20260306_080000
#   3) 都没有才退回 mtime（导入当天）
# 绝不能直接用 mtime 当基准：批量转写历史音频时 mtime 全是处理当天，
# 曾把 09-21 的课后作业算成 10-31，日历当天堆了 11 条不属于当天的事（坑 #88）。
_FM_DATE = re.compile(r"^date:\s*(\d{4})-(\d{2})-(\d{2})\s*$", re.M)
_NAME_DATE = re.compile(
    r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)|(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"
)
# 相机/截图/归档文件名的日期：IMG20260305120000、VID_20260306_080000、20260307_090000
# （这些名字里的日期和时分秒连在一起，_NAME_DATE 的「前后不能是数字」会把它们挡掉）
_TS_DATE = re.compile(
    r"(?:IMG|VID)[_-]?(20\d{2})(\d{2})(\d{2})"
    r"|(?<!\d)(20\d{2})(\d{2})(\d{2})[_-]\d{6}(?!\d)"
)
# 允许的日期偏移范围（相对文档记录日期）
DOC_DATE_BACK_DAYS = 7
DOC_DATE_FWD_DAYS = 120


def _valid_ymd(y: int, m: int, d: int) -> str:
    try:
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _doc_date(path: Path, text: str, mtime: float) -> str:
    """取这份文档“所属的那一天”，用于换算文中相对日期。"""
    head = text[:2000]
    m = _FM_DATE.search(head)
    if m:
        got = _valid_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if got:
            return got
    m = _NAME_DATE.search(path.name)
    if m:
        g = [x for x in m.groups() if x]
        got = _valid_ymd(int(g[0]), int(g[1]), int(g[2]))
        if got:
            return got
    m = _TS_DATE.search(path.name)
    if m:
        g = [x for x in m.groups() if x]
        got = _valid_ymd(int(g[0]), int(g[1]), int(g[2]))
        if got:
            return got
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def _date_in_window(date: str, doc_date: str) -> bool:
    """模型给出的日期必须落在文档记录日期附近的合理窗口内。"""
    try:
        d = datetime.strptime(date, "%Y-%m-%d").toordinal()
        base = datetime.strptime(doc_date, "%Y-%m-%d").toordinal()
    except ValueError:
        return False
    return base - DOC_DATE_BACK_DAYS <= d <= base + DOC_DATE_FWD_DAYS


# “今天回去把这个作业做了”这类当堂布置的事项：模型若仍把日期写成今天，
# 而文档是几天前的课，则按文档记录日期纠正（否则历史课堂任务会堆到处理当天）。
_SAME_DAY_HINT = re.compile(r"今天|今晚|当天|随堂|课上|本次课|这节|下课前|现场")


def _snap_same_day(date: str, quote: str, doc_date: str,
                   today: str | None = None) -> tuple[str, bool]:
    """把「被错写成今天」的当堂事项纠正到文档记录日期。返回 (date, 是否纠正)。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    if date != today or doc_date == today:
        return date, False
    try:
        gap = (datetime.strptime(today, "%Y-%m-%d")
               - datetime.strptime(doc_date, "%Y-%m-%d")).days
    except ValueError:
        return date, False
    if gap < 1 or not _SAME_DAY_HINT.search(quote or ""):
        return date, False
    return doc_date, True


def _state_path(cfg: Settings) -> Path:
    return cfg.resolve("data/schedule_files.json")


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


def _paragraphs(text: str, limit: int = 9000) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    out: list[str] = []
    buf = ""
    for p in parts:
        if not (_KEYWORDS.search(p) and _DATE_HINT.search(p)):
            continue
        if len(buf) + len(p) + 2 > limit and buf:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out


def _parse(raw: str) -> list[dict]:
    text = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        lo, hi = text.find("["), text.rfind("]")
        if lo < 0 or hi <= lo:
            return []
        try:
            data = json.loads(text[lo:hi + 1])
        except ValueError:
            return []
    return data if isinstance(data, list) else []


def extract_files(db: Database, cfg: Settings) -> dict:
    sc = getattr(cfg, "schedule", None) or {}
    if not sc.get("enabled", True) or not sc.get("file_enabled", True):
        return {"ok": False, "error": "文件日程抽取未启用"}
    if not llm_ready(cfg, "summary"):
        return {"ok": False, "error": "未配置可用的 AI 模型"}
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "上一轮文件日程抽取仍在进行"}
    try:
        kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
        tdir = kdir / "transcripts"
        if not tdir.is_dir():
            return {"ok": True, "files": 0, "inserted": 0}
        state = _load_state(cfg)
        max_files = int(sc.get("file_max_files", 20))
        # 同一堂课的「总结」和「转写」是同一份内容的两层派生物：两个都抽会得到两条
        # 标题略异的同义日程（dedupe_key 不同，日历里看着就是重复）。有总结时只读总结，
        # 它的「可记入日记的日程」表本身就是带绝对日期的干净输入。
        summarized = set()
        for p in tdir.glob("*_转写.md"):
            base = p.name.rsplit("_转写.md", 1)[0]
            if (tdir / f"{base}_总结.md").exists():
                summarized.add(p.name)
        client = build_client(cfg, "summary")
        inserted = 0
        scanned = 0
        for path in sorted(tdir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            if scanned >= max_files:
                break
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            key = str(path.name)
            if key in summarized:
                continue
            if float(state.get(key, 0)) >= mtime:
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            doc_date = _doc_date(path, text, mtime)
            failed_llm = False
            for para in _paragraphs(text):
                prompt = PROMPT.format(name=path.name, doc_date=doc_date,
                                       today=datetime.now().strftime("%Y-%m-%d"),
                                       content=para)
                try:
                    raw = client.chat([{"role": "user", "content": prompt}],
                                      temperature=0.1, max_tokens=4000)
                except LLMError as e:
                    log.warning("文件日程抽取失败 %s: %s", path.name, e)
                    failed_llm = True
                    continue
                for it in _parse(raw):
                    if not isinstance(it, dict):
                        continue
                    date = str(it.get("date") or "").strip()
                    title = str(it.get("title") or "").strip()
                    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date) or not title:
                        continue
                    if not _date_in_window(date, doc_date):
                        log.info("丢弃超窗日程 %s: date=%s 超出 %s 的 ±%d/%d 天窗口",
                                 path.name, date, doc_date,
                                 DOC_DATE_BACK_DAYS, DOC_DATE_FWD_DAYS)
                        continue
                    tm = str(it.get("time") or "").strip()
                    if tm and not re.match(r"^\d{1,2}:\d{2}$", tm):
                        tm = ""
                    conf = _confidence(it, title)
                    if conf < _CONF_MIN:
                        log.info("丢弃低置信日程（%.2f < %.2f）%s: %s",
                                 conf, _CONF_MIN, path.name, title)
                        continue
                    quote = str(it.get("quote") or "").strip()[:200]
                    date, snapped = _snap_same_day(date, quote, doc_date)
                    if snapped:
                        log.info("当堂事项日期纠正 %s: 今天 → %s（%s）",
                                 path.name, date, title)
                    ref = "/kb/file/transcripts/" + path.name
                    source = {"type": "file", "id": ref, "ref": ref,
                              "quote": quote, "label": path.name}
                    norm_title = re.sub(r"[\s，。；：、,.；:!?！？]+", "", title).lower()[:40]
                    db.upsert_schedule_item(
                        date=date, time=tm, title=title,
                        detail=str(it.get("detail") or "").strip(),
                        source=source, confidence=conf,
                        dedupe_key=f"{date}|{tm}|{norm_title}",
                    )
                    inserted += 1
            # 只有 LLM 调用失败才不记 mtime（下轮重试）；
            # 「调通了但抽到 0 条日程」必须记下来，否则每轮重读重跑 LLM 并占满名额。
            if not failed_llm:
                state[key] = mtime
        _save_state(cfg, state)
        if inserted:
            log.info("文件日程抽取：%d 个文件 → %d 条日程", scanned, inserted)
        return {"ok": True, "files": scanned, "inserted": inserted}
    except Exception as e:  # noqa: BLE001
        log.exception("文件日程抽取异常：%s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _LOCK.release()
