"""课堂录音上传：校验文件名、去重、落盘、后台转写入库。

手机端 App 只负责「文件名合规的录音」上传；PC 端在这里完成：
1. 解析 `<课程或会议名>YYYY年MM月DD日.mp3`
2. md5 去重（同一录音重复上传直接返回 duplicate，并删掉自己的临时文件）
3. 落到 data/inbox/，后台用 FireRedASR2 转写 → 总结/日程 → 写 knowledge/transcripts/
4. 转写成功后把原件归档到 knowledge/documents/（一条录音一个固定文件名），
   归档成功才删 inbox 原件；归档失败则保留原件以便重试
5. 记录状态，供 App / 网页轮询
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger("recordings")

# 日期分隔符兼容中文、短横线、点、斜杠、下划线：
# 2026年1月5日 / 2026-01-05 / 2026.01.05 / 2026_01_05
_DATE_PAT = r"(?P<y>\d{4})[年./_\-](?P<m>\d{1,2})[月./_\-](?P<d>\d{1,2})日?"
_NAME_RE = re.compile(
    rf"^(?P<course>.+?){_DATE_PAT}(?:[_\\\-\s]?\d{{1,2}})?\.(?P<ext>[A-Za-z0-9]{{2,5}})$",
    re.IGNORECASE,
)
_DATE_RE = re.compile(_DATE_PAT)


def _item_title(it: dict) -> str:
    for key in ("title", "事项", "summary"):
        v = str(it.get(key) or "").strip()
        if v:
            return v
    return str(it.get("detail") or "").strip()[:30]


def _norm_title(s: str) -> str:
    return re.sub(r"[\s，。；：、,.;:!?！？()（）\[\]【】\"'“”]+", "", s or "").lower()


# 置信度语义（三处 prompt 统一：录音 recording_ai / 文档 file_schedule / 群聊 chat_schedule；
# 前端把 <0.6 的条目标成「待确认」，所以别为了凑数把置信度抬高）：
#   0.85–1.0  有明确日期，或有不可变截止（通知原文写明「12月24日随堂考试」）
#   0.7–0.85  事项明确，时间需要推算（「明天」「下次课」）
#   0.5–0.7   事项明确，但时间或地点模糊
#   <0.5      不要输出（宁可漏，不要编）
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


# 手机录音常见的可解码格式（ffmpeg 会统一转 16k 单声道）
SUPPORTED_EXTS = {
    ".mp3", ".m4a", ".m4b", ".wav", ".amr", ".aac", ".ogg", ".opus",
    ".flac", ".wma", ".aiff", ".aif", ".caf", ".mp2",
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".3gp", ".3gpp",
}

UNRECOGNIZED_COURSE = "未识别"

# 临时文件（data/_upload_tmp、data/_ingest_tmp）超过这么多小时视为陈旧、可清理。
# 正常上传/转写在几分钟内结束，留 6 小时余量避免误删正在处理的文件。
TMP_MAX_AGE_HOURS_DEFAULT = 6
# 两次清理之间的最小间隔（秒）：清理挂在每次上传/每次转写结束上，必须限频
TMP_CLEAN_INTERVAL_S = 1800
_TMP_CLEAN_LOCK = threading.Lock()
_last_tmp_clean = 0.0

# 未在 config.yaml 里配 recordings.courses 时用的示例清单。
# 只是让首次运行有东西可选，请按自己的课表改（设置页或 config.yaml 均可）。
DEFAULT_COURSES = [
    "高等数学", "线性代数", "大学英语", "大学物理",
    "程序设计", "体育",
]


def recording_cfg(cfg: Settings) -> dict:
    raw = (cfg.recordings or {}) if isinstance(cfg.recordings, dict) else {}
    courses = [str(c).strip() for c in (raw.get("courses") or DEFAULT_COURSES) if str(c).strip()]
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_mb": int(raw.get("max_mb", 500) or 500),
        "courses": courses,
        "allow_meetings": bool(raw.get("allow_meetings", True)),
        "inbox_subdir": str(raw.get("inbox_subdir", "课堂录音")),
        # 转写+索引完成后是否删除电脑上的音频原件（文字稿保留；删前会先归档到 documents/）
        "delete_source_after_transcribe": bool(
            raw.get("delete_source_after_transcribe", True)),
        # 临时文件（_upload_tmp / _ingest_tmp）保留多少小时
        "tmp_max_age_hours": float(
            raw.get("tmp_max_age_hours", TMP_MAX_AGE_HOURS_DEFAULT)
            or TMP_MAX_AGE_HOURS_DEFAULT),
        # 独立转写进程的内存上限（GB）：超了就把这条标失败并退出，免得把整机拖死
        "max_rss_gb": float(raw.get("max_rss_gb", 10) or 10),
    }


def parse_name(filename: str, cfg: Settings, lenient: bool = True) -> dict:
    """解析文件名。

    lenient=True（默认，未知录音按 B 策略照常转写）：
      - 严格命名且课程在名单内 → recognized=True
      - 其它合法音视频扩展名 → course=未识别、recognized=False、error=原因；
        日期取文件名中的日期，取不到留空由调用方用 mtime 补
    lenient=False 时保持旧的严格校验（供测试/兼容用）。
    """
    rc = recording_cfg(cfg)
    name = Path(filename).name
    m = _NAME_RE.match(name)
    if m:
        course = m.group("course").strip().rstrip("_-./ \\")
        ext = "." + m.group("ext").lower()
        date = f"{m.group('y')}-{int(m.group('m')):02d}-{int(m.group('d')):02d}"
        if ext not in SUPPORTED_EXTS:
            return {"ok": False,
                    "error": f"不支持的音频格式「{ext or '无扩展名'}」"}
        if not course:
            if lenient:
                return {"ok": True, "recognized": False, "course": UNRECOGNIZED_COURSE,
                        "date": date, "ext": ext, "name": name,
                        "error": "缺少课程/会议名，按未识别录音处理"}
            return {"ok": False, "error": "缺少课程/会议名"}
        allowed = course in rc["courses"] or (
            rc["allow_meetings"] and "会议" in course)
        if allowed:
            return {"ok": True, "recognized": True, "course": course,
                    "date": date, "ext": ext, "name": name, "error": ""}
        reason = f"未知课程「{course}」，按未识别录音处理"
        if lenient:
            return {"ok": True, "recognized": False, "course": UNRECOGNIZED_COURSE,
                    "date": date, "ext": ext, "name": name, "error": reason}
        return {"ok": False, "error": f"未知课程「{course}」，请在课程名单里添加"}

    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return {"ok": False, "error": f"不支持的音频格式「{ext or '无扩展名'}」"}
    if not lenient:
        return {"ok": False, "error": "文件名不符合「课程名YYYY年MM月DD日.mp3」"}
    d = _DATE_RE.search(name)
    date = f"{d.group('y')}-{int(d.group('m')):02d}-{int(d.group('d')):02d}" if d else ""
    return {"ok": True, "recognized": False, "course": UNRECOGNIZED_COURSE,
            "date": date, "ext": ext, "name": name,
            "error": "文件名未匹配「课程名YYYY年MM月DD日」，按未识别录音处理"}


def _md5(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _safe_stem(course: str, date: str) -> str:
    stem = f"{course}_{date}".replace("/", "_").replace("\\", "_").strip()
    return stem[:120]


def _out_stem(course: str, date: str, rec_id: int) -> str:
    """产物文件名主干：`{课程}_{日期}_r{录音id}`。

    必须带 id：同一课程同一天的多次上传（手机重传/分片）会得到两条 recordings，
    只用 `{课程}_{日期}` 会让后转写的覆盖前一条的转写/总结/日程文件
    （历史数据里出现过大量记录的 md_path 完全相同、互相覆盖）。
    rec_id 是主键，天然唯一且稳定：同一条录音重跑仍写回自己那套文件，不会产生 _2/_3。
    """
    return f"{_safe_stem(course, date)[:110]}_r{int(rec_id)}"


def _fmt_ts(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _write_outputs(kdir: Path, stem: str, row: dict, res: dict,
                   ai: dict, db: Database | None = None) -> dict:
    """写转写 md/json、总结 md、日程 json，返回相对路径。"""
    t_dir = kdir / "transcripts"
    t_dir.mkdir(parents=True, exist_ok=True)
    md_name = f"{stem}_转写.md"
    json_name = f"{stem}_转写.json"
    sum_name = f"{stem}_总结.md"
    sched_name = f"{stem}_日程.json"

    stats = res.get("stats") or {}
    head = [
        "---",
        f"recording_id: {row['id']}",
        f"source: {row.get('filename', '')}",
        f"course: {row.get('course', '')}",
        f"date: {row.get('rec_date', '')}",
        f"model: {res.get('model', '')}",
        f"confidence_high: {stats.get('high', 0)}",
        f"confidence_mid: {stats.get('mid', 0)}",
        f"confidence_low: {stats.get('low', 0)}",
        "---",
        "",
        f"# {row.get('course') or '未识别'} · {row.get('rec_date') or ''} 课堂转写",
        "",
        f"> 来源：`{row.get('filename', '')}`；自动转写，"
        f"高 {stats.get('high', 0)} 句 / 中 {stats.get('mid', 0)} 句 / 低 {stats.get('low', 0)} 句。",
        "> 标记说明：**（高）** 可直接使用；**（中）** 建议扫一眼；**（低）** 建议重点核对。",
        "",
        "## 转写全文",
        "",
    ]
    ai_lines = []
    for s in res["sentences"]:
        tag = {"high": "高", "mid": "中", "low": "低"}.get(s.get("tier"), "高")
        head.append(f"- `[{_fmt_ts(s['start_ms'])}]` **{s.get('speaker', '老师')}**（{tag}）：{s['text']}")
        ai_lines.append(f"[{_fmt_ts(s['start_ms'])}] {s.get('speaker', '老师')}"
                        f"{'【' + tag + '】' if tag != '高' else ''}：{s['text']}")
    head.append("")
    (t_dir / md_name).write_text("\n".join(head), encoding="utf-8")
    (t_dir / json_name).write_text(
        json.dumps({"recording_id": row["id"], "source": row.get("filename", ""),
                    "course": row.get("course", ""), "date": row.get("rec_date", ""),
                    "sentences": res["sentences"], "stats": stats,
                    "elapsed_s": res.get("elapsed_s")},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    summary = (ai.get("summary") or "").strip()
    if not summary:
        summary = "_（总结未生成）_"
        if ai.get("error"):
            summary += f"\n\n> {ai['error']}"
    sm = [
        "---",
        f"recording_id: {row['id']}",
        f"source: transcripts/{md_name}",
        f"course: {row.get('course', '')}",
        f"date: {row.get('rec_date', '')}",
        "---",
        "",
        f"# {row.get('course') or '未识别'} · {row.get('rec_date') or ''} 课堂总结",
        "",
        f"> 依据：`transcripts/{md_name}`（原始转写，含置信度标记）。",
        "",
        summary,
        "",
    ]
    items = ai.get("schedule") or []
    if items:
        sm += ["## 可记入日记的日程", "",
               "| 日期 | 时间 | 事项 | 依据 |", "|---|---|---|---|"]
        for it in items:
            title = str(it.get("title") or "").replace("|", "\\|")
            sm.append(f"| {it.get('date') or '—'} | {it.get('time') or '—'} | "
                      f"{title} | {it.get('source_time') or ''} |")
        sm.append("")
    (t_dir / sum_name).write_text("\n".join(sm), encoding="utf-8")
    (t_dir / sched_name).write_text(json.dumps({
        "recording_id": row["id"],
        "source": f"transcripts/{md_name}",
        "course": row.get("course", ""),
        "date": row.get("rec_date", ""),
        "items": items,
        "error": ai.get("error", ""),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    if db is not None and items:
        for it in items:
            date = str(it.get("date") or "").strip()
            title = _item_title(it)
            if not date or not title:
                continue
            conf = _confidence(it, title)
            if conf < _CONF_MIN:
                log.info("丢弃低置信日程（%.2f < %.2f）#%s: %s",
                         conf, _CONF_MIN, row["id"], title)
                continue
            tm = str(it.get("time") or "").strip()
            source = {
                "type": "recording",
                "id": str(row["id"]),
                "ref": f"/kb/file/transcripts/{md_name}",
                "quote": str(it.get("source_quote") or "")[:200],
                "label": row.get("course") or "课堂录音",
            }
            db.upsert_schedule_item(
                date=date, time=tm, title=title,
                detail=str(it.get("detail") or "").strip(),
                source=source, confidence=conf,
                dedupe_key=f"{date}|{tm}|{_norm_title(title)}",
            )

    return {
        "md": f"transcripts/{md_name}",
        "json": f"transcripts/{json_name}",
        "summary": f"transcripts/{sum_name}",
        "schedule": f"transcripts/{sched_name}",
        "ai_text": "\n".join(ai_lines),
    }


def _set_stored_path(db: Database, rec_id: int, path: Path) -> None:
    """把 recordings.stored_path 改指归档副本。

    走 db.update_recording 的 stored_path 参数（db.py:1008，COALESCE 语义：
    不传=不动，传 ""=清空）。失败只记日志，不影响转写结果。
    """
    try:
        db.update_recording(int(rec_id), stored_path=str(path))
    except Exception:  # noqa: BLE001
        log.warning("更新录音原件归档路径失败 #%s", rec_id, exc_info=True)


def _archive_source(kdir: Path, row: dict, rec_id: int,
                    src: Path) -> tuple[Path | None, bool]:
    """转写成功后把原件归档进 knowledge/documents/。

    返回 (归档文件路径, 是否可删源文件)：
      - 归档成功 → (documents/xxx, True)，调用方可以删 inbox 里的那份；
      - 源本来就在 documents/ 里 → (src, False)，绝不能当副本删掉；
      - 归档失败 → (None, False)，调用方必须保留原件（否则这条录音永久不可重试）。

    归档名固定为 `{课程}_{日期}_r{rec_id}{ext}`：同一条录音重复转写幂等，
    不会像 `_unique_target` 那样每次重试多出一个 _1/_2 副本。
    """
    d_dir = kdir / "documents"
    try:
        d_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("无法创建录音归档目录 %s", d_dir, exc_info=True)
        return None, False
    try:
        if src.parent.resolve() == d_dir.resolve():
            return src, False
    except OSError:
        pass
    ext = (src.suffix or "").lower() or ".bin"
    stem = _out_stem(row.get("course") or UNRECOGNIZED_COURSE,
                     row.get("rec_date") or "未知日期", rec_id)
    dst = d_dir / f"{stem}{ext}"
    try:
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            return dst, True  # 之前已经归档过（重试重跑），不用再复制
    except OSError:
        pass
    part = dst.with_name(dst.name + ".part")
    try:
        if part.exists():
            part.unlink()
        shutil.copy2(src, part)
        if part.stat().st_size != src.stat().st_size:
            raise OSError("归档副本大小与原文件不一致")
        os.replace(part, dst)
    except OSError:
        log.warning("归档录音原件失败：%s → %s", src, dst, exc_info=True)
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return None, False
    return dst, True


def cleanup_tmp(cfg: Settings, max_age_hours: float | None = None,
                now: float | None = None) -> dict:
    """清理 data/_upload_tmp 与 data/_ingest_tmp 里的陈旧临时文件。

    `_upload_tmp` 是网端分块接收的 rec_*.mp3；`_ingest_tmp` 是 ffmpeg 转的 16k wav
    与说话人切片（含 spk/ 子目录）。两处都没有别的清理机制，长时间积累会吃掉几个 GB。
    只删 mtime 早于 `max_age_hours` 的普通文件（含子目录），正在写入的不会命中。
    """
    age_h = float(max_age_hours if max_age_hours is not None
                  else TMP_MAX_AGE_HOURS_DEFAULT)
    now_ts = time.time() if now is None else float(now)
    cutoff = now_ts - age_h * 3600.0
    kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
    dirs = [cfg.resolve("data/_upload_tmp"), kdir.parent / "_ingest_tmp"]
    deleted, freed, errors = 0, 0, 0
    scanned: list[str] = []
    for d in dirs:
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        scanned.append(str(d))
        try:
            targets = [p for p in d.rglob("*") if p.is_file()]
        except OSError:
            continue
        for p in targets:
            try:
                st = p.stat()
                if st.st_mtime > cutoff:
                    continue
                p.unlink()
                deleted += 1
                freed += int(st.st_size)
            except OSError:
                errors += 1
    if deleted:
        log.info("清理临时文件 %d 个（%.1f MB，阈值 %sh）", deleted, freed / 1048576.0, age_h)
    return {"dirs": scanned, "deleted": deleted, "freed_bytes": freed,
            "errors": errors, "max_age_hours": age_h}


def _maybe_cleanup_tmp(cfg: Settings) -> None:
    """限频调用 cleanup_tmp：最多每 TMP_CLEAN_INTERVAL_S 秒扫一次，异常不外抛。"""
    global _last_tmp_clean
    if time.monotonic() - _last_tmp_clean < TMP_CLEAN_INTERVAL_S:
        return
    if not _TMP_CLEAN_LOCK.acquire(blocking=False):
        return
    try:
        if time.monotonic() - _last_tmp_clean < TMP_CLEAN_INTERVAL_S:
            return
        _last_tmp_clean = time.monotonic()
        cleanup_tmp(cfg, recording_cfg(cfg).get("tmp_max_age_hours"))
    except Exception:  # noqa: BLE001
        log.warning("清理陈旧临时文件失败", exc_info=True)
    finally:
        _TMP_CLEAN_LOCK.release()


def _process(rec_id: int, path: Path, db: Database, cfg: Settings) -> None:
    """后台线程：FireRed 转写 → 说话人/置信度 → 总结/日程 → 入库。"""
    from .asr_firered import transcribe as asr_transcribe
    from .recording_ai import generate as ai_generate

    kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
    try:
        row = db.get_recording(rec_id) or {}
        # 产物名带 rec_id：同课程同日期的两条录音不会再互相覆盖
        stem = _out_stem(row.get("course") or UNRECOGNIZED_COURSE,
                         row.get("rec_date") or "未知日期", rec_id)

        def progress(msg: str) -> None:
            db.update_recording(rec_id, detail=msg)

        db.update_recording(rec_id, status="transcribing", detail="排队中…")
        tmp_dir = kdir.parent / "_ingest_tmp"
        res = asr_transcribe(path, tmp_dir, progress=progress, unload_after=True)
        ai = ai_generate(cfg, course=row.get("course") or "",
                         rec_date=row.get("rec_date") or "",
                         transcript="\n".join(
                             f"[{_fmt_ts(s['start_ms'])}] {s.get('speaker', '老师')}"
                             f"{'【' + {'high': '高', 'mid': '中', 'low': '低'}.get(s.get('tier'), '高') + '】' if s.get('tier') != 'high' else ''}：{s['text']}"
                             for s in res["sentences"]),
                         source_name=row.get("filename") or path.name,
                         recording_id=rec_id, db=db)
        out = _write_outputs(kdir, stem, {**row, "id": rec_id}, res, ai, db)
        stats = res.get("stats") or {}
        # 删原件之前必须先归档：否则删掉的是这份录音在电脑上的唯一副本，
        # 之后 retry_recording / 换模型重跑都无源可用（历史 65 条就是这么废掉的）。
        delete_src = bool(recording_cfg(cfg).get("delete_source_after_transcribe", True))
        archived: Path | None = None
        can_prune = False
        if delete_src:
            archived, can_prune = _archive_source(kdir, row, rec_id, path)
            if archived is not None:
                _set_stored_path(db, rec_id, archived)
        if archived is not None:
            src_note = f"；原件归档 → documents/{archived.name}"
        elif delete_src:
            src_note = "；原件归档失败，已保留 inbox 原件（可重试）"
        else:
            src_note = "；按配置保留原件"
        detail = (f"转写 {stats.get('chars', 0)} 字"
                  f"（高{stats.get('high', 0)}/中{stats.get('mid', 0)}/低{stats.get('low', 0)}）"
                  f" → {out['md']}；总结 → {out['summary']}{src_note}")
        if ai.get("error"):
            detail += f"；{ai['error']}"
        db.update_recording(rec_id, status="transcribed", detail=detail,
                            md_path=out["md"], summary_path=out["summary"],
                            schedule_path=out["schedule"])
        try:
            from .pipeline import mark_dirty
            mark_dirty(cfg, "index")
        except Exception:  # noqa: BLE001
            log.warning("标记索引待更新失败", exc_info=True)
        # 归档成功才删 inbox 里的那份；手机端不受影响
        if can_prune and archived is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("无法删除录音原件：%s", path)
        log.info("录音处理完成 #%s: %s", rec_id, detail)
    except Exception as e:  # noqa: BLE001
        log.exception("录音转写失败 #%s", rec_id)
        db.update_recording(rec_id, status="error", detail=f"处理失败：{e}")
    finally:
        # 顺手清掉陈旧的临时文件（限频、异常不影响转写结果）
        _maybe_cleanup_tmp(cfg)


def _resolve_source(row: dict, cfg: Settings) -> Path | None:
    """找出录音对应的源文件。

    历史记录可能没有 stored_path，且 filename 是中文日期（2026年1月5日），
    而 inbox 实际文件名是下划线日期（某课程_2026-01-05.mp3）。因此：
    1) stored_path / inbox/filename / documents/filename 直接命中；
    2) 按记录 id 拼出归档名 documents/{课程}_{日期}_r{id}.{ext} 精确命中；
    3) 否则按「课程名 + 日期」扫描 inbox 与 documents。
    """
    kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
    inbox = cfg.resolve("data/inbox")
    name = str(row.get("filename") or "")
    direct = []
    stored = str(row.get("stored_path") or "").strip()
    if stored:
        direct.append(Path(stored))
    if name:
        direct.append(inbox / name)
        direct.append(kdir / "documents" / name)
    for p in direct:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    # 2) 归档副本：名字固定为 {课程}_{日期}_r{id}{ext}，按 id 精确拼出来（不靠模糊匹配，
    #    归档名的课程段是库里存的 course，未识别录音也能对上）
    try:
        rid = int(row.get("id"))
    except (TypeError, ValueError):
        rid = 0
    if rid:
        stem_id = _out_stem(row.get("course") or UNRECOGNIZED_COURSE,
                            row.get("rec_date") or "未知日期", rid)
        prefix = f"{stem_id}."
        try:
            for p in sorted((kdir / "documents").iterdir()):
                if p.is_file() and p.name.startswith(prefix) and not p.name.endswith(".part"):
                    return p
        except OSError:
            pass
    parsed = parse_name(name, cfg) if name else None
    if not parsed or not parsed.get("ok"):
        return None
    target = (parsed.get("course"), parsed.get("date"))
    for root in (inbox, kdir / "documents"):
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        for p in root.iterdir():
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            q = parse_name(p.name, cfg)
            if q.get("ok") and (q.get("course"), q.get("date")) == target:
                return p
    return None


def recover_pending(db: Database, cfg: Settings) -> int:
    """WebUI 启动时恢复上次中断/未开始的录音任务。"""
    n = 0
    rows = db.list_recordings(limit=200)
    rows.sort(key=lambda r: (str(r.get("rec_date") or ""), int(r["id"])), reverse=True)
    for r in rows:
        status = str(r.get("status") or "")
        detail = str(r.get("detail") or "")
        # 兼容此前被错误标成「源文件不存在」的历史记录（stored_path 为空、文件名日期格式不同）
        recoverable_error = status == "error" and "源文件不存在" in detail
        if status not in ("pending", "transcribing") and not recoverable_error:
            continue
        path = _resolve_source(r, cfg)
        if path is None:
            db.update_recording(int(r["id"]), status="error",
                                detail="源文件不存在，无法转写")
            continue
        db.update_recording(int(r["id"]), status="pending",
                            detail="重启后重新排队…")
        if spawn_transcribe(int(r["id"]), db, cfg):
            n += 1
        else:
            break     # 已有一个转写进程在跑，剩下的留给它收尾后接力
    if n:
        log.info("恢复 %d 个未完成的录音任务", n)
    return n


def _move_with_retry(src: Path, dst: Path, tries: int = 6) -> None:
    """Windows 上新写入的文件常被杀毒/搜索短暂占用，移动失败时重试并退化为复制。"""
    last: Exception | None = None
    for i in range(tries):
        try:
            shutil.move(str(src), str(dst))
            return
        except OSError as e:  # PermissionError 是 OSError 子类
            last = e
            time.sleep(0.4 * (i + 1))
    try:
        shutil.copy2(src, dst)
        for i in range(tries):
            try:
                src.unlink(missing_ok=True)
                return
            except OSError:
                time.sleep(0.4 * (i + 1))
    except OSError:
        pass
    raise last or RuntimeError("无法移动上传文件")


def retry_recording(rec_id: int, db: Database, cfg: Settings) -> dict:
    """把已归档/失败的录音重新排队转写（源文件从 documents 找回复制到 inbox）。"""
    row = db.get_recording(int(rec_id))
    if not row:
        return {"ok": False, "status": 404, "error": "录音记录不存在"}
    if row.get("status") in ("pending", "transcribing"):
        return {"ok": False, "status": 409, "error": "这条录音正在处理中"}
    src = _resolve_source(row, cfg)
    if src is None:
        return {"ok": False, "status": 409,
                "error": "电脑上已找不到这条录音的音频文件，请在手机里重新上传"}
    inbox = cfg.resolve(cfg.storage["inbox_dir"])
    retry_dir = inbox / ".retry"
    retry_dir.mkdir(parents=True, exist_ok=True)
    target = retry_dir / Path(row.get("filename") or src.name).name
    try:
        if target.exists():
            target.unlink()
        shutil.copy2(src, target)
    except OSError as e:
        return {"ok": False, "status": 500, "error": f"复制音频失败：{e}"}
    db.update_recording(rec_id, status="pending", detail="重新排队转写…", md_path="")
    spawn_transcribe(int(rec_id), db, cfg)
    return {"ok": True, "recording": db.get_recording(int(rec_id)),
            "message": "已重新排队转写"}


def _safe_unlink(p: Path) -> None:
    """尽力删除；Windows 上文件被杀毒/预览占用时可能失败，只记日志。"""
    try:
        p.unlink(missing_ok=True)
    except OSError:
        log.warning("临时文件暂无法删除：%s", p)


def save_upload(tmp_path: Path, filename: str, db: Database, cfg: Settings) -> dict:
    """校验并接收一个上传文件；成功返回记录信息，后台开始转写。

    临时文件由本函数兜底清理：成功时已被 move 走（unlink 无副作用），
    重复/被拒时在这里删掉——之前重复分支直接 return，`data/_upload_tmp`
    没有任何清理机制，实测堆积了 500MB+ 的 rec_*.mp3。
    """
    _maybe_cleanup_tmp(cfg)
    try:
        return _accept_upload(tmp_path, filename, db, cfg)
    finally:
        _safe_unlink(tmp_path)


def _accept_upload(tmp_path: Path, filename: str, db: Database, cfg: Settings) -> dict:
    """save_upload 的实现体（不负责清理临时文件）。"""
    rc = recording_cfg(cfg)
    if not rc["enabled"]:
        return {"ok": False, "status": 503, "error": "录音上传未启用（recordings.enabled=false）"}
    parsed = parse_name(filename, cfg, lenient=True)
    if not parsed["ok"]:
        return {"ok": False, "status": 400, "error": parsed["error"]}
    rec_date = parsed["date"] or time.strftime(
        "%Y-%m-%d", time.localtime(tmp_path.stat().st_mtime))
    size = tmp_path.stat().st_size
    if size <= 0:
        return {"ok": False, "status": 400, "error": "空文件"}
    if rc["max_mb"] and size > rc["max_mb"] * 1024 * 1024:
        return {"ok": False, "status": 413,
                "error": f"文件超过 {rc['max_mb']}MB 上限"}
    md5 = _md5(tmp_path)
    old = db.recording_by_md5(md5)
    if old:
        # 重复上传：不落盘、不转写；临时文件由 save_upload 的 finally 删掉
        return {"ok": True, "duplicate": True, "recording": old,
                "message": "同一录音已上传过，跳过"}
    inbox = cfg.resolve(cfg.storage["inbox_dir"])
    inbox.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(parsed["course"], rec_date)
    target = inbox / f"{stem}.{parsed['ext'].lstrip('.')}"
    n = 2
    while target.exists():
        target = inbox / f"{stem}_{n}.{parsed['ext'].lstrip('.')}"
        n += 1
    _move_with_retry(tmp_path, target)
    if parsed.get("recognized"):
        detail = "已接收，排队转写"
    else:
        detail = "未识别录音（%s），排队转写" % (parsed.get("error") or "文件名不规范")
    rec_id = db.add_recording(
        filename=filename, course=parsed["course"], rec_date=rec_date,
        md5=md5, size=size, status="pending", stored_path=str(target),
        detail=detail,
    )
    spawn_transcribe(rec_id, db, cfg)
    row = db.get_recording(rec_id)
    return {"ok": True, "duplicate": False, "recording": row,
            "message": "已接收，排队转写中"}


# ---------------------------------------------------------------------------
# 转写进程隔离
#
# 转写要加载 FireRedASR2（约 7GB）+ CAM++，一条两小时的录音实测吃到 8.3GB。
# 以前它跑在 Web 进程里的一个线程上，实际出现过「上传一条大录音，
# 网页服务被一起带走」：手机端表现为突然连不上，而人完全不知道发生了什么。
#
# 现在 Web 只做「落盘 + 写库 + 起一个独立进程」，转写崩溃只影响自己：
#   · data/.transcribe.lock 里存 pid，保证同时只有一个转写进程（两个 8GB 会爆内存）
#   · 进程内看门狗盯内存，超过 recordings.max_rss_gb（默认 10GB）就把这条标失败并退出
#   · 一个进程收尾后接力下一个 pending（kick_pending），队列不会因为崩一次就卡死
# ---------------------------------------------------------------------------

_LOCK_NAME = ".transcribe.lock"
_RSS_CHECK_SECONDS = 15


def _lock_path(cfg: Settings) -> Path:
    return cfg.resolve(cfg.storage["db_path"]).parent / _LOCK_NAME


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（Windows 下不能用 os.kill(pid, 0)，那会真的杀进程）。"""
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def current_worker(cfg: Settings) -> int:
    """正在跑的转写进程 pid；没有则 0（顺手清掉进程已死的陈旧锁）。"""
    p = _lock_path(cfg)
    try:
        pid = int((p.read_text(encoding="utf-8") or "").strip() or 0)
    except (OSError, ValueError):
        return 0
    if _pid_alive(pid):
        return pid
    try:
        p.unlink()
    except OSError:
        pass
    return 0


def _acquire_lock(cfg: Settings) -> bool:
    if current_worker(cfg):
        return False
    p = _lock_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def _release_lock(cfg: Settings) -> None:
    p = _lock_path(cfg)
    try:
        if p.exists() and (p.read_text(encoding="utf-8") or "").strip() == str(os.getpid()):
            p.unlink()
    except OSError:
        pass


def _rss_gb() -> float:
    """当前进程占用内存（GB），取不到就返回 0。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        if fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / 1073741824.0
    except Exception:  # noqa: BLE001  统计失败不影响转写
        pass
    return 0.0


def _start_rss_watchdog(cfg: Settings, rec_id: int, db: Database) -> threading.Thread:
    limit = float(recording_cfg(cfg).get("max_rss_gb") or 10)

    def loop() -> None:
        while True:
            time.sleep(_RSS_CHECK_SECONDS)
            used = _rss_gb()
            if used and used > limit:
                msg = (f"转写占用 {used:.1f}GB，超过上限 {limit:g}GB，已中止以免影响电脑其他程序；"
                       f"这条录音没转写完，可在录音列表点「重试」")
                log.error("录音 #%s %s", rec_id, msg)
                try:
                    db.update_recording(int(rec_id), status="error", detail=msg)
                except Exception:  # noqa: BLE001
                    log.warning("标记超限失败", exc_info=True)
                os._exit(3)

    t = threading.Thread(target=loop, daemon=True, name="rss-watchdog")
    t.start()
    return t


def spawn_transcribe(rec_id: int, db: Database, cfg: Settings) -> bool:
    """起一个独立进程去转写这条录音；已有转写进程在跑则保持排队。"""
    if current_worker(cfg):
        log.info("已有转写进程在跑，录音 #%s 保持排队", rec_id)
        return False
    root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "-X", "utf8", str(root / "main.py"),
           "transcribe-one", str(int(rec_id))]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x08000000      # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(cmd, cwd=str(root), close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except OSError as e:
        log.error("启动转写进程失败：%s", e)
        return False
    log.info("已启动独立转写进程：录音 #%s", rec_id)
    return True


def kick_pending(db: Database, cfg: Settings, limit: int = 50) -> int:
    """队列接力：没有转写进程在跑时，挑最老的 pending 起一个。"""
    if current_worker(cfg):
        return 0
    rows = db.list_recordings(limit=limit)
    pend = [r for r in rows if str(r.get("status") or "") == "pending"]
    pend.sort(key=lambda r: int(r["id"]))
    for r in pend:
        if spawn_transcribe(int(r["id"]), db, cfg):
            return 1
    return 0


def run_worker(rec_id: int) -> int:
    """独立转写进程入口（main.py transcribe-one）。返回退出码。"""
    from .config import load_config

    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    if not _acquire_lock(cfg):
        log.info("已有转写进程在跑，本进程退出（录音 #%s 留队）", rec_id)
        return 0
    try:
        _start_rss_watchdog(cfg, rec_id, db)
        row = db.get_recording(int(rec_id)) or {}
        path = _resolve_source(row, cfg)
        if path is None:
            db.update_recording(int(rec_id), status="error",
                                detail="源文件不存在，无法转写")
            return 1
        _process(int(rec_id), path, db, cfg)
        return 0
    finally:
        _release_lock(cfg)
        try:
            kick_pending(db, cfg)
        except Exception:  # noqa: BLE001
            log.warning("接力下一条转写失败", exc_info=True)



