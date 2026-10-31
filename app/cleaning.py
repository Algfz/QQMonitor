"""智能导入引擎：拖拽上传文件的自动分拣 + 清洗。

分类规则（classify_file）：
    chat      聊天记录（.csv/.json，或内容含 QQ 导出特征的 .txt/.bak）
              → 导入数据库，并重建聊天 Markdown（chat/）
    image     图片（.png/.jpg/.jpeg/.gif/.webp/.bmp）
              → 原图存 documents/；OCR 识别文字清洗成 md 存 transcripts/（同名 _OCR.md）
    document  可提取文档（.pdf/.docx/.pptx）
              → 原件存 documents/；提取文本清洗成 AI 可读 md 存 transcripts/
    text      文字稿（.md/.srt/.vtt/.ass / 无消息特征的 .txt）→ 直接进 transcripts/
    audio     会议录屏/录音（.mp4/.mkv/.m4a/.mp3/.wav 等）
              → 原件存 documents/；语音转写文字清洗成 md 存 transcripts/
    archive   其他（.zip/.rar/.xls 等）→ 原样存 documents/

可选依赖（缺失时优雅降级，仅影响对应清洗能力）：
    rapidocr-onnxruntime   图片 OCR
    pymupdf                PDF 文本提取
    python-docx            Word 文本提取
    python-pptx            PPT 文本提取
    funasr + torch         会议录屏/录音语音转写
    ffmpeg（系统命令）     从视频提取音频轨
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path

from .config import Settings
from .db import Database
from .importer import extract_meta, import_file, load_records
from .knowledge import sync_knowledge
from .meetings import RECORDING_EXTS
from .transcribe import (AUDIO_VIDEO_EXTS, configure, to_markdown,
                         transcribe_file)

log = logging.getLogger("cleaning")

_CHAT_EXTS = {".csv", ".json"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt"}
_TEXT_EXTS = {".md", ".srt", ".vtt", ".ass"}
# 会议录屏/录音：以 meetings 侧的录制格式为准，避免两处规则漂移
_AUDIO_EXTS = AUDIO_VIDEO_EXTS | RECORDING_EXTS

KIND_LABEL = {
    "chat": "聊天记录",
    "image": "图片 OCR",
    "document": "文档提取",
    "text": "文字稿",
    "audio": "语音转写",
    "archive": "原始存档",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")
# 页码 / 页眉页脚行：- 3 - / 第 3 页 / Page 3 of 12
_PAGE_LINE_RE = re.compile(
    r"^\s*(?:[-–—·]?\s*\d{1,4}\s*[-–—·]?|第\s*\d+\s*页[^\n]{0,8}|[Pp]age\s*\d+\s*(?:of\s*\d+)?)\s*$"
)


# ---------- 分类 ----------
def _looks_like_chat(path: Path) -> bool:
    """内容嗅探：判断文件是否是 QQ 聊天记录导出。"""
    ext = path.suffix.lower()
    try:
        if ext == ".json":
            head = path.read_text(encoding="utf-8-sig", errors="replace")[:5000]
            return any(k in head for k in
                       ('"messages"', '"content"', '"sender"',
                        '"nickname"', '"message"', '"text"'))
        if ext == ".csv":
            head = path.read_text(encoding="utf-8-sig", errors="replace")[:2000]
            return any(k in head for k in ("发送者", "昵称", "时间", "内容"))
        # .txt / .bak：QQ 官方文件头或记录特征
        # （老版文本 bak 走下面的通用解析；新版 ZIP 备份包单独认，
        #   让它进「聊天导入」分支，由 importer 给出「先恢复再同步本机库」指引）
        if ext == ".bak":
            try:
                with open(path, "rb") as f:
                    if f.read(4) == b"PK\x03\x04":
                        return True
            except OSError:
                pass
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if extract_meta(text).get("group_id") or "消息对象" in text[:2000]:
            return True
        records = load_records(path)
        if len(records) < 3:
            return False
        with_sender = sum(1 for r in records if r.get("user_id"))
        return with_sender >= max(2, len(records) // 3)
    except Exception:
        return False


def classify_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _CHAT_EXTS or ext in (".txt", ".bak"):
        if _looks_like_chat(path):
            return "chat"
        return "text" if ext == ".txt" else "archive"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _DOCUMENT_EXTS:
        return "document"
    if ext in _TEXT_EXTS:
        return "text"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "archive"


# ---------- OCR ----------
_ocr = None  # 懒加载单例


def ocr_boxes(path: Path) -> list[tuple]:
    """OCR 并返回带坐标的原始结果：`[(box, text, score), ...]`。

    box 为四点坐标 `[[x0,y0],[x1,y1],[x2,y2],[x3,y3]]`，text 为识别文字。
    版面还原（表格 / 段落 / 公式行）必须依赖坐标，所以这里**不做文本合并**，
    直接把原始条目交给 `ocr_layout.boxes_to_markdown` 处理。
    引擎不可用或识别失败时返回空列表（与 ocr_image 一致的降级策略：不抛异常）。
    """
    global _ocr
    try:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        result, _elapse = _ocr(str(path))
    except Exception as e:
        log.warning("OCR 失败 %s: %s", path.name, e)
        return []
    if not result:
        return []
    out: list[tuple] = []
    for item in result:
        # RapidOCR 返回 (box, text, score)；个别版本是 (text, score)，容错兼容
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        if isinstance(item[1], str) and item[1]:
            score = float(item[2]) if len(item) > 2 else 1.0
            out.append((item[0], item[1], score))
        elif isinstance(item[0], str) and item[0]:
            score = float(item[1]) if len(item) > 1 else 1.0
            out.append((None, item[0], score))
    return out


def ocr_image(path: Path) -> str:
    """RapidOCR 识别图片文字；OCR 不可用或失败时返回空串（不抛异常）。

    只取文字、丢弃坐标。需要表格等版面结构时请用 ocr_boxes + ocr_layout。
    """
    return "\n".join(t[1] for t in ocr_boxes(path))


# ---------- 文档提取 ----------
# 每页文字少于这么多字符，就当「扫描页/图片页」走 OCR（有些 PDF 只带页眉几个字）
_PDF_OCR_MIN_CHARS = 40
# 单份 PDF 最多 OCR 这么多页，避免几百页的扫描件把同步卡住
_PDF_OCR_MAX_PAGES = 30
# 渲染倍率：2x ≈ 144dpi，RapidOCR 在这个清晰度下识别率足够
_PDF_OCR_ZOOM = 2.0


def _extract_pdf(path: Path) -> str:
    """提取 PDF 文字；整页没有文字层（扫描版）时渲染成图走 RapidOCR。"""
    import fitz  # pymupdf

    parts: list[str] = []
    scanned = 0
    with fitz.open(str(path)) as doc:
        with tempfile.TemporaryDirectory(prefix="pdf_ocr_") as td:
            for idx, page in enumerate(doc):
                text = (page.get_text() or "").strip()
                if len(text) >= _PDF_OCR_MIN_CHARS:
                    parts.append(text)
                    continue
                if scanned >= _PDF_OCR_MAX_PAGES:
                    # 超过上限的页面：有多少文字层就留多少，不再 OCR
                    parts.append(text)
                    continue
                scanned += 1
                try:
                    pix = page.get_pixmap(
                        matrix=fitz.Matrix(_PDF_OCR_ZOOM, _PDF_OCR_ZOOM),
                        alpha=False)
                    png = Path(td) / f"p{idx + 1}.png"
                    pix.save(str(png))
                    ocr = ocr_image(png)
                except Exception as e:  # 渲染/OCR 失败不影响其它页
                    log.warning("PDF 页面 OCR 失败 %s 第 %d 页：%s",
                                path.name, idx + 1, e)
                    ocr = ""
                if text:
                    parts.append(text + "\n" + ocr)
                else:
                    parts.append(ocr)
    if scanned:
        log.info("PDF 有 %d 页没有文字层，已用 OCR 识别：%s", scanned, path.name)
    return "\n".join(parts)


_word_warned = False


def _extract_doc(path: Path) -> str:
    """用本机 Word（COM）读取旧版 .doc 文本；没有 Word/pywin32 时返回空串。

    只在 Windows 上可用：项目面向 Windows 本机 QQ，这条路径是可选增强，
    失败只记日志、不影响其它阶段。
    """
    global _word_warned
    try:
        import win32com.client as wc
    except Exception:
        if not _word_warned:
            _word_warned = True
            log.warning("未安装 pywin32，旧版 .doc 无法提取（安装 Word 与 pywin32 后重试）")
        return ""
    word = None
    try:
        word = wc.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(
            str(path.resolve()), ConfirmConversions=False, ReadOnly=True,
            AddToRecentFiles=False, Visible=False)
        try:
            raw = doc.Content.Text or ""
        finally:
            doc.Close(SaveChanges=False)
        # Word 的 Content.Text：\r 是段落、\x07 是表格单元格结束符，其余是控件字符
        raw = raw.replace("\x07", "\t").replace("\r\n", "\n").replace("\r", "\n")
        raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
        return raw
    except Exception as e:
        log.warning("Word 读取 .doc 失败 %s：%s", path.name, e)
        return ""
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass


def extract_document_text(path: Path) -> str:
    """从 PDF/Word/PPT 提取纯文本；不支持或失败返回空串。"""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return _extract_pdf(path)
        if ext == ".doc":
            return _extract_doc(path)
        if ext == ".docx":
            import docx
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs)
        if ext == ".pptx":
            from pptx import Presentation
            prs = Presentation(str(path))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        for para in shape.text_frame.paragraphs:
                            t = "".join(run.text for run in para.runs).strip()
                            if t:
                                parts.append(t)
            return "\n".join(parts)
    except Exception as e:
        log.warning("文档提取失败 %s: %s", path.name, e)
    return ""


# ---------- 清洗 ----------
def _merge_paragraphs(lines: list[str]) -> list[str]:
    """PDF/OCR 常把段落断成多行：行尾不是结束标点则与下一行合并。"""
    end = ("。", "！", "？", ".", "!", "?", "；", ";", "：", ":", "，", ",")
    out: list[str] = []
    buf = ""
    for raw in lines:
        s = raw.strip()
        if not s:
            if buf:
                out.append(buf)
                buf = ""
            continue
        if buf:
            if buf.endswith(end):
                out.append(buf)
                buf = s
            else:
                # 相邻两行都是中文则直接拼接，否则补空格（英文/数字）
                sep = "" if (_CJK.search(buf[-1]) and _CJK.search(s[0])) else " "
                buf += sep + s
        else:
            buf = s
    if buf:
        out.append(buf)
    return out


def clean_to_md(text: str, title: str) -> str:
    """原始文本 → AI 可读 Markdown：去页码/页眉、合并断行、结构化标题。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.splitlines() if ln.strip() and not _PAGE_LINE_RE.match(ln)]
    paras = _merge_paragraphs(lines)
    body = "\n\n".join(paras)
    return f"# {title}\n\n{body}\n"


# ---------- 入库 ----------
def _unique_target(dir_: Path, name: str) -> Path:
    target = dir_ / name
    if not target.exists():
        return target
    stem, ext = Path(name).stem, Path(name).suffix
    for i in range(1, 10000):
        t = dir_ / f"{stem}_{i}{ext}"
        if not t.exists():
            return t
    return dir_ / f"{stem}_{Path(name).name[:6]}{ext}"


def ingest_one(db: Database, cfg: Settings, src: Path, group_id: int,
               kdir: Path) -> dict:
    """处理单个文件，返回 {name, kind, ok, detail, error, need_kb_sync}。"""
    kind = classify_file(src)
    base = {"name": src.name, "kind": kind, "need_kb_sync": False}

    if kind == "chat":
        result = import_file(db, cfg, src, group_id=group_id)
        if not result["ok"]:
            return {**base, "ok": False, "error": result["error"]}
        base["need_kb_sync"] = True
        # bak 导入是多群结构，自带 detail；单文件导入拼通用摘要
        detail = result.get("detail") or (
            f"导入 {result['inserted']} 条，跳过重复 {result['skipped']} 条")
        return {**base, "ok": True, "detail": detail}

    d_dir = kdir / "documents"
    t_dir = kdir / "transcripts"
    d_dir.mkdir(parents=True, exist_ok=True)
    t_dir.mkdir(parents=True, exist_ok=True)

    if kind == "image":
        dest = _unique_target(d_dir, src.name)
        shutil.copy2(src, dest)
        detail = f"原图 → documents/{dest.name}"
        # 优先用视觉语言模型（能描述内容、还原表格与公式），
        # 不可用时降级为 RapidOCR（app/vision.py）
        text, err = "", ""
        try:
            from .vision import analyze_image
            text, err = analyze_image(cfg, src)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if not text:
            text = ocr_image(src).strip()   # 降级：纯 OCR
        if text:
            md_name = _unique_target(t_dir, f"{Path(src.stem).name}_OCR.md").name
            t_dir.joinpath(md_name).write_text(
                clean_to_md(text, f"{src.stem}（图片理解）"), encoding="utf-8")
            detail += f"；图片理解 {len(text)} 字 → transcripts/{md_name}"
            if err:
                detail += f"（视觉模型不可用已降级 OCR：{err[:60]}）"
        else:
            detail += "；未识别到内容（视觉模型与 OCR 均不可用或图片无内容）"
        return {**base, "ok": True, "detail": detail}

    if kind == "document":
        dest = _unique_target(d_dir, src.name)
        shutil.copy2(src, dest)
        detail = f"原件 → documents/{dest.name}"
        text = extract_document_text(src).strip()
        if text:
            md_name = _unique_target(t_dir, f"{Path(src.stem).name}.md").name
            t_dir.joinpath(md_name).write_text(
                clean_to_md(text, f"{src.stem}（{dest.suffix.lstrip('.')} 提取）"),
                encoding="utf-8")
            detail += f"；提取清洗 {len(text)} 字 → transcripts/{md_name}"
        else:
            detail += "；未能提取文本（仅存档原件）"
        return {**base, "ok": True, "detail": detail}

    if kind == "text":
        dest = _unique_target(t_dir, src.name)
        shutil.copy2(src, dest)
        return {**base, "ok": True, "detail": f"文字稿 → transcripts/{dest.name}"}

    if kind == "audio":
        dest = _unique_target(d_dir, src.name)
        shutil.copy2(src, dest)
        detail = f"录屏/录音 → documents/{dest.name}"
        ing = getattr(cfg, "ingest", {}) or {}
        text = None
        reason = "已关闭语音转写（ingest.asr=false）"
        if not ing.get("asr", True):
            detail += f"；{reason}"
        else:
            # 转写较慢（长音频数分钟），临时 wav 放在知识库外避免污染
            tmp_dir = kdir.parent / "_ingest_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            configure(
                str(ing.get("asr_device", "auto") or "auto"),
                str(ing.get("asr_model", "ZhipuAI/GLM-ASR-Nano-2512")
                    or "ZhipuAI/GLM-ASR-Nano-2512"),
                int(ing.get("asr_batch_seconds", 120) or 120),
                float(ing.get("asr_glm_gpu_gb", 4.0) or 4.0),
            )
            text, reason = transcribe_file(
                src, tmp_dir,
                hotwords=str(ing.get("asr_hotwords", "") or ""),
                unload_after=bool(ing.get("asr_unload_after", True)),
            )
        if text:
            md_name = _unique_target(t_dir, f"{Path(src.stem).name}_转写.md").name
            t_dir.joinpath(md_name).write_text(
                to_markdown(text, f"{src.stem}（会议转写）", src.name),
                encoding="utf-8")
            detail += f"；转写 {len(text)} 字 → transcripts/{md_name}"
        else:
            detail += f"；未自动转写（{reason}）；可自行转写后把 .md 拖进来"
        return {**base, "ok": True, "detail": detail}

    # archive / 其他
    dest = _unique_target(d_dir, src.name)
    shutil.copy2(src, dest)
    return {**base, "ok": True, "detail": f"原件存档 → documents/{dest.name}"}


def ingest_files(db: Database, cfg: Settings, paths: list[Path],
                 group_id: int = 0, progress=None) -> list[dict]:
    """批量处理上传文件。有聊天记录导入成功时，最后统一重建一次知识库。

    progress: 可选回调，在后台线程中调用。
        progress(int)  -> 已完成文件数
        progress(str)  -> 当前处理阶段文本（如 "语音识别中（第 3/8 段）"）
    """
    kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "documents").mkdir(exist_ok=True)
    (kdir / "transcripts").mkdir(exist_ok=True)

    results: list[dict] = []
    need_sync = False
    for i, p in enumerate(paths):
        r = ingest_one(db, cfg, p, group_id, kdir)
        need_sync = need_sync or r.pop("need_kb_sync")
        results.append(r)
        if progress:
            progress(i + 1)

    if need_sync:
        log.info("聊天记录导入完成，重建知识库 …")
        try:
            sync_knowledge(db, cfg)
        except Exception as e:
            log.warning("知识库重建失败: %s", e)
            results.append({"name": "(知识库同步)", "kind": "text", "ok": False,
                            "error": f"聊天记录已入库，但知识库重建失败：{e}"})
    return results
