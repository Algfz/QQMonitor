"""知识库同步：把已导入的消息与媒体整理成结构化的本地知识库。

知识库目录（config.knowledge.dir，默认 data/knowledge/）：
    chat/           AI 可读：聊天记录原文 Markdown（每群一个文件，按天分节）
    media/          原始媒体：聊天图片/语音/文件，按 <群号>/<年月>/ 存放
    documents/      原始文档：群文件 / 通知附件（从 inbox 分拣）
    transcripts/    AI 可读：会议文字识别稿（从 inbox 分拣）

只同步数据库中已导入的群（白名单天然就是"你导入过的群"），
不会把 NTQQ 原始库里的其他群带进来。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger("knowledge")

# NTQQ 40800 消息体里的本地媒体绝对路径（::NTOSFull::C:\...）
# 排除控制字符，允许中文等非 ASCII 路径（旧版 [\x20-\x7e] 会在中文处截断）
_PATH_RE = re.compile(rb"::NTOSFull::([A-Za-z]:\\[^\x00-\x1f]{5,})")
# 从 extract_content 输出的占位符中提取媒体文件名，例如 [图片:abc.jpg]
_MEDIA_LINK_RE = re.compile(r"\[(图片|语音|视频|文件):([^\]]+)\]")

# inbox 分拣规则
_TRANSCRIPT_EXTS = {".txt", ".md", ".srt", ".vtt", ".ass"}
_DOCUMENT_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".zip", ".rar", ".7z", ".jpg", ".jpeg", ".png", ".gif"}


# ---------- 聊天记录 Markdown ----------

def _esc(text: str) -> str:
    """Markdown 特殊字符转义（保留原文可读性，避免破坏结构）。"""
    return text.replace("\r", "").replace("|", "\\|").replace("`", "\\`")


# 媒体占位符：[图片:xxx.png] / [文件:报告.pdf] / [语音:a.amr] / [视频:b.mp4]
_MEDIA_RE = re.compile(r"\[(图片|视频|语音|文件):([^\]]+)\]")


def _linkify_media(content: str, media_map: dict[str, str],
                   month: str = "") -> str:
    """把媒体占位符转成指向 media/ 原始文件的相对链接。

    media_map 同时带两种键：``"<年月>/<文件名>"``（精确）与 ``"<文件名>"``（兜底），
    因此不同月份的同名文件不会互相指错（审计 P0-7）。找不到时保留原样，避免死链。
    """
    def rep(m: re.Match) -> str:
        kind, name = m.group(1), m.group(2).strip()
        rel = media_map.get(f"{month}/{name}") if month else None
        rel = rel or media_map.get(name)
        if not rel:
            return m.group(0)
        return f"[{kind}]({rel})"
    return _MEDIA_RE.sub(rep, content)


def _build_media_map(kdir: Path, gid: int) -> dict[str, str]:
    """扫描 media/<群号>/ 目录，建立 文件键 → chat 相对链接 的映射。

    chat Markdown 位于 knowledge/chat/，媒体位于 knowledge/media/<群号>/<年月>/，
    因此链接统一用 ../media/... 前缀。键同时提供「年月/文件名」和「文件名」。
    """
    mdir = kdir / "media" / str(gid)
    if not mdir.is_dir():
        return {}
    mp: dict[str, str] = {}
    for p in sorted(mdir.rglob("*")):
        if p.is_file():
            rel = f"../media/{gid}/{p.parent.name}/{p.name}"
            mp[f"{p.parent.name}/{p.name}"] = rel
            mp.setdefault(p.name, rel)
    return mp


def render_group_md(rows: list[dict], group_name: str, group_id: int,
                    media_map: dict[str, str] | None = None,
                    ocr_map: dict[str, str] | None = None) -> str:
    """把一整个群的消息渲染成 Markdown（按天分节，时间正序）。

    media_map 非空时，图片/语音/视频/文件占位符会转成指向 media/ 的链接。
    ocr_map 非空（文件名 → Markdown）时，**图片内的文字会内联在该条消息下方**，
    缩进 2 空格以保持在列表项内；表格能被渲染器识别成表格。
    """
    media_map = media_map or {}
    ocr_map = ocr_map or {}
    lines = [f"# {group_name} ({group_id})", ""]
    cur_day = ""
    for r in rows:
        ts = r.get("ts") or 0
        dt = datetime.fromtimestamp(ts) if ts else None
        day = dt.strftime("%Y-%m-%d") if dt else "未知时间"
        month = dt.strftime("%Y-%m") if dt else "unknown"
        if day != cur_day:
            lines.append(f"## {day}")
            lines.append("")
            cur_day = day
        t = dt.strftime("%H:%M") if dt else "--:--"
        who = (r.get("nickname") or "未知").strip()
        uid = r.get("user_id") or 0
        content = (r.get("content") or "").strip().replace("\n", " / ")
        # 必须在 linkify 之前取文件名：替换后就只剩链接了
        img_names = [m.group(2).strip() for m in _MEDIA_RE.finditer(content)
                     if m.group(1) == "图片"]
        content = _linkify_media(content, media_map, month)
        imp = r.get("important")
        mark = " ⭐" if imp else ""
        lines.append(f"- **{t}** {who}({uid}){mark}: {_esc(content)}")
        for name in img_names:
            text = ocr_map.get(f"{month}/{name}") or ocr_map.get(name)
            if not text:
                continue
            lines.append("  图内文字（OCR）：")
            for seg in text.splitlines():
                lines.append(f"  {seg}" if seg.strip() else "")
    lines.append("")
    return "\n".join(lines)


def sync_chat_group(db: Database, kdir: Path, gid: int,
                    dry_run: bool = False, cfg: Settings | None = None,
                    ocr: bool | None = None, ocr_force: bool = False,
                    on_progress=None) -> str | None:
    """重渲染单个群的聊天 Markdown。返回生成的文件名；无消息时返回 None。

    渲染前会先补跑该群的图片 OCR（结果缓存进 media_ocr 表，已识别过的跳过），
    使图片里的文字随消息一起进入 Markdown。开关：`knowledge.ocr_chat_images`。
    传 cfg=None 则不 OCR（保持旧行为）。
    """
    g = db.get_group(gid)
    if not g:
        return None
    chat_dir = kdir / "chat"
    if not dry_run:
        chat_dir.mkdir(parents=True, exist_ok=True)
    gname = (g.get("name") or f"群{gid}").strip()
    # 安全文件名：只保留群号 + 清理过的群名
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", gname)[:40] or str(gid)
    rows = db.query_messages(group_id=gid, limit=10_000_000)
    rows = list(reversed(rows))  # 库中按 ts 倒序，渲染需正序
    if not rows:
        return None

    ocr_map: dict[str, str] = {}
    if cfg is not None and (ocr if ocr is not None
                            else bool(cfg.knowledge.get("ocr_chat_images", True))):
        if not dry_run:
            build_media_ocr(db, kdir, gid=gid, cfg=cfg, force=ocr_force,
                            on_progress=on_progress)
        ocr_map = db.ocr_group_map(gid)

    media_map = _build_media_map(kdir, gid)
    md = render_group_md(rows, gname, gid, media_map, ocr_map)
    out = chat_dir / f"{gid}_{safe}.md"
    if dry_run:
        log.info("[试运行] 生成 %s (%d 条)", out.name, len(rows))
    else:
        # 群名变化时，删除同 gid 的旧文件（只保留当前群名的新文件）
        for old in chat_dir.glob(f"{gid}_*.md"):
            if old.name != out.name:
                try:
                    old.unlink()
                    log.info("清理旧群名文件：%s → %s", old.name, out.name)
                except OSError as e:
                    log.warning("删除旧文件 %s 失败：%s", old.name, e)
        # 内容没变就不写盘：保住 mtime，索引器的「按 mtime 增量」才真正生效。
        # 旧实现每次同步都重写全部 chat md（mtime 全变）→ 每轮全量重嵌入（审计 ⑯）。
        try:
            if out.exists() and out.read_text(encoding="utf-8") == md:
                log.debug("聊天 md 内容未变，保留 mtime：%s", out.name)
                return out.name
        except OSError:
            pass
        out.write_text(md, encoding="utf-8")
        log.info("生成 %s (%d 条)", out.name, len(rows))
    return out.name


def sync_chat(db: Database, kdir: Path, dry_run: bool = False,
              watch: list[int] | None = None, cfg: Settings | None = None,
              ocr_force: bool = False, on_progress=None) -> list[str]:
    """把数据库里「生效白名单内」的会话渲染为 Markdown。返回生成的文件名列表。

    统一走 watchlist.allows_conversation()（审计批次 2 ⑩）：
    - 群：白名单为空 = 不限制；非空只渲染名单内的群
    - 好友（负数 gid）：只有在 watch_buddies 里才渲染；空名单 = 一个都不渲染
      （旧实现无条件渲染好友，被移出白名单的私聊仍会进知识库，审计 P0-2）
    未通过白名单的会话，其旧 chat md 会被删除，避免幽灵知识。
    """
    from .watchlist import allows_conversation

    legacy = {int(g) for g in (watch or [])}
    produced: list[str] = []
    chat_dir = kdir / "chat"
    for g in db.get_groups():
        gid = g["group_id"]
        if cfg is not None:
            ok = allows_conversation(cfg, gid)
        else:
            # 旧调用（未传 cfg）：群按 watch 过滤，好友不过滤
            ok = not (gid > 0 and legacy and gid not in legacy)
        if not ok:
            log.info("会话 %s 不在生效白名单内，跳过聊天同步", gid)
            if not dry_run:
                for old in chat_dir.glob(f"{gid}_*.md"):
                    try:
                        old.unlink()
                        log.info("删除已移出白名单的聊天文件：%s", old.name)
                    except OSError as e:
                        log.warning("删除 %s 失败：%s", old.name, e)
            continue
        name = sync_chat_group(db, kdir, gid, dry_run, cfg=cfg,
                               ocr_force=ocr_force, on_progress=on_progress)
        if name:
            produced.append(name)
    return produced


# ---------- 删除 / 移出白名单：级联清理派生数据（审计 P0-2） ----------

def purge_conversation(db: Database, cfg: Settings, gid: int, *,
                       drop_messages: bool = True,
                       drop_media: bool = True) -> dict:
    """删会话（或移出白名单）时，把文件与向量索引一起清掉。

    - 聊天 md：``chat/<gid>_*.md`` → 删除文件，并清对应 kb_chunks / kb_meta
    - 媒体：``media/<gid>/`` 目录 + media_ocr 缓存（drop_media=True 时）
    - drop_messages=True 时连消息/成员/群记录/会话记录一起删
    - 最后标记索引 dirty，让下一轮自动同步刷新向量库

    旧实现只删了 md / 只 unlink 文件，向量块与 OCR 缓存留在库里，
    AI 仍能检索到已删除的内容（审计 P0-2）。
    """
    kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
    names: list[str] = []
    chat_dir = kdir / "chat"
    if chat_dir.is_dir():
        for p in chat_dir.glob(f"{gid}_*.md"):
            names.append(p.name)
            try:
                p.unlink()
            except OSError as e:
                log.warning("删除聊天文件 %s 失败：%s", p.name, e)
    if drop_media:
        mdir = kdir / "media" / str(gid)
        if mdir.is_dir():
            shutil.rmtree(mdir, ignore_errors=True)
    res = db.purge_conversation(gid, drop_messages=drop_messages,
                                drop_ocr=drop_media, chat_md_names=names)
    log.info("清理会话 %s：消息 %d、索引块 %d、OCR %d、聊天文件 %d",
             gid, res["messages"], res["chunks"], res["ocr"], len(names))
    try:
        from .pipeline import mark_dirty
        mark_dirty(cfg, "index")
    except Exception:  # noqa: BLE001
        pass
    return res


# ---------- 聊天图片 OCR ----------
# 图片里的通知、课表、费用清单是群里最有价值的信息之一，
# 但 AI 只能看到「[图片](链接)」。这里把图片文字 OCR 出来缓存进 media_ocr 表，
# 再由 render_group_md 内联进聊天 Markdown。

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


def ocr_available() -> bool:
    """本机是否装了 OCR 引擎（rapidocr-onnxruntime）。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def build_media_ocr(db: Database, kdir: Path, gid: int | None = None,
                    cfg: Settings | None = None, force: bool = False,
                    on_progress=None) -> dict:
    """对 media/<群号>/ 下的图片做 OCR 并缓存，返回统计。

    设计要点
    --------
    - **增量**：已缓存且 size/mtime 未变的直接跳过；`force=True` 清空后全量重跑。
    - **先数目标再加载模型**：没有待识别图片时立即返回，不加载 7GB 视觉模型
      （审计 P0-11：旧实现零目标也会加载模型，几秒钟白花）。
    - **失败不写缓存**：识别失败（超时/报错）不落空结果，下一轮会重试；
      只有「真的识别了但没文字」（表情包等）才缓存空串，避免永久固化失败态。
    - **进度回调** `on_progress(done, total, filename)`。
    """
    from .ocr_layout import ocr_image_markdown

    stat = {"total": 0, "ocr": 0, "empty": 0, "skipped": 0, "failed": 0,
            "chars": 0, "error": ""}
    mroot = kdir / "media"
    if not mroot.is_dir():
        stat["error"] = "media/ 目录不存在，请先做一次媒体同步"
        return stat

    if force:
        db.ocr_del_group(gid) if gid else db.ocr_clear()
    known = db.ocr_known()

    # 先收集目标与「真正需要识别」的图片
    targets: list[Path] = []
    for p in sorted(mroot.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _IMG_EXTS:
            continue
        if gid is not None:
            parts = p.relative_to(mroot).parts
            if not parts or parts[0] != str(gid):
                continue
        targets.append(p)
    stat["total"] = len(targets)

    pending: list[tuple[Path, str, os.stat_result]] = []
    for p in targets:
        rel = p.relative_to(kdir).as_posix()
        try:
            st = p.stat()
        except OSError:
            stat["failed"] += 1
            continue
        if not force and known.get(rel) == (st.st_size, st.st_mtime):
            stat["skipped"] += 1
            continue
        pending.append((p, rel, st))

    if not pending:
        stat["engine"] = "无需识别（全部已缓存）"
        return stat

    # 有活才加载引擎：优先视觉语言模型，不可用时降级 RapidOCR
    from .vision import get_engine

    eng = get_engine(cfg) if cfg is not None else None
    vlm_ok, vlm_why = (eng.available() if eng else (False, "未传入配置"))
    if vlm_ok:
        stat["engine"] = f"vlm:{eng.backend}"
    elif ocr_available():
        stat["engine"] = "rapidocr"
        if (cfg.vision or {}).get("backend", "local") != "off":
            stat["error"] = f"（视觉模型不可用，已降级 OCR：{vlm_why}）"
    else:
        stat["error"] = ("没有可用的图片识别引擎：视觉模型不可用"
                         f"（{vlm_why}），且未安装 rapidocr-onnxruntime")
        return stat

    formula_wrap = bool((cfg.knowledge if cfg else {})
                        .get("ocr_formula_wrap", False))
    for i, (p, rel, st) in enumerate(pending, 1):
        text = ""
        failed = False
        try:
            if vlm_ok:
                text, err = eng.analyze(p)
                if err:
                    log.warning("视觉识别失败 %s: %s", p.name, err)
                    failed = True
                    text = ""
            # VLM 没输出（表情包/纯图）或失败时，用本地 OCR 兜底
            if (failed or not text) and ocr_available():
                try:
                    text = ocr_image_markdown(p, formula_wrap)
                    failed = False
                except Exception as e:  # noqa: BLE001
                    log.warning("OCR 兜底失败 %s: %s", p.name, e)
                    failed = True
        except Exception as e:  # noqa: BLE001
            log.warning("图片识别失败 %s: %s", p.name, e)
            failed = True
        if failed:
            # 不写缓存：下一轮还会重试（审计 P0-11）
            stat["failed"] += 1
            if on_progress:
                on_progress(i, len(pending), p.name)
            continue
        try:
            db.ocr_put(rel, st.st_size, st.st_mtime, text)
        except Exception as e:  # 单条写库失败不应中断整批
            log.warning("OCR 结果入库失败 %s: %s", rel, e)
            stat["failed"] += 1
            continue
        if text:
            stat["ocr"] += 1
            stat["chars"] += len(text)
        else:
            stat["empty"] += 1
        if on_progress:
            on_progress(i, len(pending), p.name)
    return stat


# ---------- 媒体同步（从 NTQQ 原始库）----------

def _open_ntdb(db_path: Path, key: str):
    """以 SQLCipher 打开 NTQQ 原始库（复用 qq_sync 的 WAL 一致性拷贝）。

    旧实现只拷主库、不拷 -wal（最近消息可能缺失），且明文临时文件放在
    知识库目录里，异常时可能残留。现在整份拷贝 + 剥头 + 解密都放进
    TemporaryDirectory，返回 (conn, tempdir)，由调用方 finally 清理。
    """
    import tempfile

    from .qq_sync import _copy_with_wal, _open_cleartext

    td = tempfile.TemporaryDirectory(prefix="qqmonitor_kb_")
    try:
        raw = Path(td.name) / "raw.db"
        clear = Path(td.name) / "clear.db"
        _copy_with_wal(db_path, raw)
        conn = _open_cleartext(db_path, key, raw, clear)
        conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
    except Exception:
        td.cleanup()
        raise
    return conn, td


def _month_dir(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m") if ts else "unknown"


# 媒体类型 → NTQQ 本地缓存子目录（相对 nt_data/）
# 图片除了 Pic/ 还要找 Emoji/emoji-recv/——群里的表情包也是图片消息，
# 网页应能显示；但它们没有文字，OCR 阶段会跳过（见 sync_media）
_MEDIA_KIND_DIRS = {
    "图片": ("Pic", "Emoji/emoji-recv"),
    "语音": ("Ptt",),
    "视频": ("Video",),
    "文件": ("File",),
}


def _is_emoji(p: Path) -> bool:
    """是否表情包缓存（Emoji 目录下）：可显示，但不必 OCR。"""
    s = str(p).replace("\\", "/")
    return "/Emoji/" in s or s.startswith("Emoji/")


def _resolve_media_src(name: str, kind: str, ts: int,
                       nt_root: Path) -> Path | None:
    r"""根据聊天消息里的媒体文件名/云 ID，找 NTQQ 本地缓存源文件。

    QQ NT 老格式消息体带 ``::NTOSFull::C:\...\Ori\<name>`` 原图路径；
    新格式只给 ``$<32hex>.jpg`` 云 ID，本地只有 ``Thumb\<id>_720.jpg``
    （或 ``_0.jpg``）缩略图。本函数优先找 Ori 原图，没有则用 720/0
    缩略图兜底，让网页至少能看到图、OCR 能识别文字。
    """
    if not name:
        return None
    stem, ext = os.path.splitext(name)
    if not ext:
        return None
    ext = ext.lower()
    dirs = _MEDIA_KIND_DIRS.get(kind)
    if not dirs:
        return None
    nt_data = Path(nt_root) / "nt_data"
    # 消息月份优先；缓存目录月份未必等于消息月份（图片可能更早下载，
    # 实测有跨月的情况），故跨月兜底搜索
    months = [_month_dir(ts)]
    for d in dirs:
        for m in _list_month_dirs(nt_data, d):
            if m not in months:
                months.append(m)
    for month in months:
        for d in dirs:
            base = nt_data / d / month
            # 老格式原图/直接存放
            for p in (base / name, base / "Ori" / name):
                if p.exists():
                    return p
        # 原图没找到再找缩略图（新格式通常只有 Thumb）
        for d in dirs:
            base = nt_data / d / month / "Thumb"
            for suf in ("_720", "_0", "_198", "_200", ""):
                p = base / f"{stem}{suf}{ext}"
                if p.exists():
                    return p
    # 扩展名兜底：消息里的扩展名未必等于缓存里的实际扩展名
    # （实测同一张图消息记为 .png、缓存实际存为 .jpg），此时按文件名主干
    # 忽略扩展名再找一次，避免白白显示「媒体未同步」
    for month in months:
        for d in dirs:
            base = nt_data / d / month
            for sub in (base, base / "Ori", base / "Thumb"):
                if not sub.is_dir():
                    continue
                for p in sub.iterdir():
                    if not p.is_file():
                        continue
                    # 文件名主干相同；仅剥离已知缩略图后缀（不能无差别剥数字，
                    # 否则 hash 末尾的数字会被误吃掉）
                    pstem = os.path.splitext(p.name)[0]
                    for suf in ("_720", "_0", "_198", "_200"):
                        if pstem.endswith(suf):
                            pstem = pstem[: -len(suf)]
                            break
                    if pstem == stem:
                        return p
    return None


_MONTH_DIR_CACHE: dict[str, list[str]] = {}


def _list_month_dirs(nt_data: Path, kind_dir: str) -> list[str]:
    """列出 nt_data/Pic 等目录下的月份目录，按时间倒序（新 → 旧）。"""
    key = str(nt_data / kind_dir)
    if key in _MONTH_DIR_CACHE:
        return _MONTH_DIR_CACHE[key]
    base = nt_data / kind_dir
    months: list[str] = []
    if base.is_dir():
        try:
            months = sorted((p.name for p in base.iterdir() if p.is_dir()),
                            reverse=True)
        except OSError:
            months = []
    _MONTH_DIR_CACHE[key] = months
    return months


def sync_media(db: Database, cfg: Settings, kdir: Path,
               group_ids: list[int] | None = None,
               dry_run: bool = False) -> dict:
    """从 NTQQ 原始库提取已导入群的媒体文件到 knowledge/media/。

    group_ids 为 None 时同步全部已导入群；传群号列表则只同步这些群。
    需要 config.knowledge 配置了 nt_db（原始库路径）与 key（解密密钥）。
    """
    nt_db = (cfg.knowledge.get("nt_db") or "").strip()
    key = (cfg.knowledge.get("key") or "").strip()
    if not nt_db or not key:
        log.info("未配置 knowledge.nt_db / knowledge.key，跳过媒体同步")
        return {"copied": 0, "skipped": 0, "groups": 0}
    db_path = Path(nt_db)
    if not db_path.exists():
        log.warning("knowledge.nt_db 不存在: %s", db_path)
        return {"copied": 0, "skipped": 0, "groups": 0}

    # 只处理已导入数据库的群（白名单）；显式指定群时取交集
    # 防御：group_ids 必须是可迭代的群号列表，否则（如误传 bool）按 None 处理，
    # 避免签名变动导致位置参数错位时整个同步崩溃
    if group_ids is not None and not isinstance(group_ids, (list, tuple, set)):
        log.warning("sync_media 收到非法的 group_ids=%r，已忽略该参数", group_ids)
        group_ids = None
    all_ids = [g["group_id"] for g in db.get_groups()]
    # 白名单与实时监听、聊天导入共用同一套语义（审计批次 2 ⑩）：
    # 群名单为空 = 不限制；好友必须在 watch_buddies 里（空 = 一个都不同步）
    from .watchlist import allows_conversation
    all_ids = [gid for gid in all_ids if allows_conversation(cfg, gid)]
    if group_ids is not None:
        group_ids = [gid for gid in group_ids if gid in all_ids]
    else:
        group_ids = all_ids
    if not group_ids:
        return {"copied": 0, "skipped": 0, "groups": 0}

    try:
        import sqlcipher3  # noqa: F401  提前检查，缺失时优雅降级而非抛异常
    except ImportError:
        log.warning("未安装 sqlcipher3（pip install sqlcipher3），媒体同步跳过")
        return {"copied": 0, "skipped": 0, "groups": 0}

    # 复用 tools/ntqq_export.py 的 protobuf 解析（含新格式云 ID 提取）
    from tools import ntqq_export as nt

    # .../nt_qq/nt_db/nt_msg.db -> .../nt_qq（本地缓存在其下的 nt_data/）
    nt_root = db_path.parent.parent
    conn, cleanup = _open_ntdb(db_path, key)
    copied = skipped = 0
    try:
        for gid in group_ids:
            if gid < 0:
                # 好友（负数 gid = -好友QQ）：消息在 c2c_msg_table，[40030]=对方QQ
                rows = conn.execute(
                    "SELECT [40050],[40800] FROM c2c_msg_table "
                    "WHERE [40030]=? AND [40800] IS NOT NULL", (-gid,)).fetchall()
            else:
                # 群号字段回退：新创建群/小号群 [40030] 可能为 0（坑 #21）
                rows = conn.execute(
                    "SELECT [40050],[40800] FROM group_msg_table "
                    "WHERE COALESCE(NULLIF([40030],0), NULLIF([40027],0), [40021])=?"
                    " AND [40800] IS NOT NULL", (gid,)).fetchall()
            seen: set[str] = set()
            for ts, blob in rows:
                text = nt.extract_content(blob)
                for m in _MEDIA_LINK_RE.finditer(text):
                    kind, name = m.group(1), m.group(2).strip()
                    if not name:
                        continue
                    src = _resolve_media_src(name, kind, ts or 0, nt_root)
                    if not src:
                        skipped += 1
                        continue
                    # 目标文件名：去掉缩略图后缀 _720/_0，与聊天 md 占位符一致
                    dst_name = name
                    stem, ext = os.path.splitext(name)
                    if src.stem.endswith(("_720", "_0")):
                        dst_name = f"{stem}{ext}"
                    month = _month_dir(ts or 0)
                    dst = kdir / "media" / str(gid) / month / dst_name
                    # 去重：同一文件多次出现只复制一次
                    fid = f"{gid}/{dst_name}/{src.stat().st_size}"
                    if fid in seen:
                        continue
                    seen.add(fid)
                    if dry_run:
                        log.info("[试运行] %s -> %s", src.name, dst)
                        copied += 1
                        continue
                    if dst.exists():
                        # 同名文件：大小一致视为已复制；大小不同说明同名不同内容
                        # （跨月/源文件被替换），覆盖它以保证链接指向最新内容（审计 P0-7）
                        try:
                            if dst.stat().st_size == src.stat().st_size:
                                skipped += 1
                                continue
                            log.warning("媒体同名但大小不同，覆盖旧文件：%s", dst)
                        except OSError:
                            pass
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, dst)
                        copied += 1
                        # 表情包写入空 OCR 缓存：网页能显示，但 OCR 阶段
                        # 会因"已缓存"直接跳过（media_ocr 空结果也缓存），
                        # 省下大量无意义的识别时间
                        if _is_emoji(src):
                            st = dst.stat()
                            db.ocr_put(dst.relative_to(kdir).as_posix(),
                                       st.st_size, st.st_mtime, "")
                    except OSError as e:
                        log.warning("复制失败 %s: %s", src, e)
                        skipped += 1
    finally:
        conn.close()
        # cleanup 是可关闭的临时目录：整份明文拷贝随目录一起删掉
        try:
            cleanup.cleanup()
        except Exception:  # noqa: BLE001
            pass
    log.info("媒体同步完成: 复制 %d, 跳过 %d", copied, skipped)
    return {"copied": copied, "skipped": skipped, "groups": len(group_ids)}


# ---------- inbox 分拣（文档 / 会议文字稿）----------

def sync_inbox(cfg: Settings, kdir: Path, dry_run: bool = False) -> dict:
    """把 inbox 里的文字稿/文档分拣到知识库对应目录。

    - 文字稿（.txt/.md/.srt/.vtt/.ass）→ transcripts/
    - 文档/图片（.pdf/.docx/.xlsx/图片等）→ documents/
    分拣后原文件从 inbox 移出（保留在知识库）。
    """
    inbox = cfg.resolve(cfg.storage["inbox_dir"])
    if not inbox.exists():
        return {"transcripts": 0, "documents": 0}
    t_dir = kdir / "transcripts"
    d_dir = kdir / "documents"
    t_cnt = d_cnt = 0
    for f in sorted(inbox.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        target_dir = None
        if ext in _TRANSCRIPT_EXTS:
            target_dir = t_dir
        elif ext in _DOCUMENT_EXTS:
            target_dir = d_dir
        else:
            continue  # 录制文件等留给 archive 命令
        if dry_run:
            log.info("[试运行] %s -> %s", f.name, target_dir.name)
            if ext in _TRANSCRIPT_EXTS:
                t_cnt += 1
            else:
                d_cnt += 1
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f.name
        if target.exists():
            target = target_dir / f"{f.stem}_{datetime.now().strftime('%H%M%S')}{ext}"
        shutil.move(str(f), str(target))
        log.info("分拣 %s -> %s", f.name, target)
        if ext in _TRANSCRIPT_EXTS:
            t_cnt += 1
        else:
            d_cnt += 1
    return {"transcripts": t_cnt, "documents": d_cnt}


# ---------- 总入口 ----------

def sync_audio_inbox(db: Database, cfg: Settings, kdir: Path,
                     dry_run: bool = False, on_progress=None) -> dict:
    """把 inbox 里的录音/录屏转写进知识库。

    原件复制到 documents/，文字稿写入 transcripts/<名字>_转写.md；
    处理成功后从 inbox 移除，避免下一轮重复转写。
    """
    from .cleaning import ingest_one
    from .transcribe import AUDIO_VIDEO_EXTS

    inbox = cfg.resolve(cfg.storage["inbox_dir"])
    out = {"scanned": 0, "transcribed": 0, "archived": 0, "failed": 0, "files": []}
    if not inbox.exists():
        return out
    files = sorted(f for f in inbox.iterdir()
                   if f.is_file() and f.suffix.lower() in AUDIO_VIDEO_EXTS)
    out["scanned"] = len(files)
    for f in files:
        if dry_run:
            log.info("[试运行] 录音转写 %s", f.name)
            out["files"].append({"name": f.name, "ok": True, "detail": "试运行"})
            continue
        if on_progress:
            on_progress(f"语音转写 {f.name}")
        try:
            res = ingest_one(db, cfg, f, 0, kdir)
        except Exception as e:  # 单文件失败不影响其他录音
            log.exception("录音入库失败 %s", f)
            out["failed"] += 1
            out["files"].append({"name": f.name, "ok": False, "detail": str(e)})
            continue
        detail = str(res.get("detail") or "")
        out["files"].append({"name": f.name, "ok": bool(res.get("ok")), "detail": detail})
        if not res.get("ok"):
            out["failed"] += 1
            continue
        # 原件已复制进 documents/，从 inbox 移除避免重复处理
        try:
            f.unlink()
        except OSError:
            log.warning("无法删除 inbox 原件：%s", f)
        if "转写" in detail and "未自动转写" not in detail:
            out["transcribed"] += 1
        else:
            out["archived"] += 1
    if out["transcribed"] and not dry_run:
        mark_dirty(cfg, "index")
    return out


def sync_knowledge(db: Database, cfg: Settings, dry_run: bool = False,
                   with_media: bool = True, ocr_force: bool = False,
                   on_progress=None) -> dict:
    """同步全部：聊天 Markdown（含图片 OCR）+ 媒体 + inbox 分拣。"""
    kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
    if not dry_run:
        kdir.mkdir(parents=True, exist_ok=True)
        for sub in ("chat", "media", "documents", "transcripts"):
            (kdir / sub).mkdir(exist_ok=True)
        (kdir / "README.md").write_text(
            "# 本地知识库\n\n"
            "- `chat/` AI 可读的聊天记录原文（每群一个 Markdown，按天分节）\n"
            "- `media/` 聊天中的原始图片/语音/文件（按群/日期存放）\n"
            "- `documents/` 群文件与通知附件（手动放入 data/inbox/ 后自动分拣）\n"
            "- `transcripts/` 会议文字识别稿（手动放入 data/inbox/ 后自动分拣）\n",
            encoding="utf-8")

    # 顺序很重要：**先复制媒体，再渲染聊天 Markdown**。
    # 渲染时靠扫描 media/<群号>/ 建「文件名 → 相对路径」映射来生成图片链接，
    # 若先渲染后复制，本次新增的图片链接会全部缺失（要等下次同步才补上），
    # 且 OCR 也扫不到刚复制进来的图（坑 #16）。
    media = sync_media(db, cfg, kdir, dry_run=dry_run) if with_media else \
        {"copied": 0, "skipped": 0, "groups": 0}
    # 白名单统一作用于「导入 / 实时监听 / 知识库同步」四条线
    watch = [int(g) for g in (cfg.watch_groups or [])]
    chat_files = sync_chat(db, kdir, dry_run, watch=watch, cfg=cfg,
                           ocr_force=ocr_force, on_progress=on_progress)
    inbox = sync_inbox(cfg, kdir, dry_run)
    audio = {"scanned": 0, "transcribed": 0, "archived": 0, "failed": 0, "files": []}
    if cfg.knowledge.get("audio_inbox_auto") and not dry_run:
        audio = sync_audio_inbox(db, cfg, kdir, dry_run=False)
        if audio.get("transcribed"):
            log.info("inbox 录音转写：%s 个成功，%s 个仅归档",
                     audio["transcribed"], audio["archived"])
    return {
        "chat": chat_files,
        "media": media,
        "inbox": inbox,
        "audio": audio,
        "ocr": db.ocr_stats(),
        "dir": str(kdir),
    }
