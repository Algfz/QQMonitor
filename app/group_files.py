"""群文件自动入库（只利用本机已有副本，不联网下载）。

背景：QQ 消息里的「文件」元素只有在**本机点过下载**后才会带上本地路径
（``::NTOSFull::C:\\Users\\...\\xxx.docx``）。本模块负责：

1. 同步时把文件消息记录进 ``group_files`` 表（qq_sync 调用 ``record``）；
2. 对 ``pending/missing`` 的记录，按本地路径（或 Downloads 里同名同大小的文件）
   复制到 ``knowledge/documents/``；
3. PDF / DOCX / PPTX / DOC（需本机 Word）自动提取文字，纯文本类直接读，
   清洗成 ``knowledge/transcripts/<文件名>.md``，下次索引即被 AI 检索到；
   扫描版 PDF（没有文字层）会自动渲染成图片走本地 OCR；
4. 其余类型（zip / xls / 视频等）只归档，不提取。

不依赖 NapCat：没有本机副本的文件会标成 ``missing``（「未下载」），
在知识库页能看到清单；你在 QQ 里下载后再点一次「扫描群文件」即可入库。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .cleaning import clean_to_md, extract_document_text
from .config import Settings
from .db import Database
from .pipeline import mark_dirty
from .watchlist import allows_buddy, allows_group

log = logging.getLogger("group_files")

# 能自动提取文字的格式（.doc 走本机 Word COM，见 cleaning.extract_document_text）
_EXTRACTABLE = {".pdf", ".docx", ".doc", ".pptx"}
# 本身就是文本、直接读的格式
_TEXTUAL = {".txt", ".md", ".markdown", ".csv", ".json", ".srt", ".vtt", ".ass"}
# 明确只归档（给用户看的说明）
_ARCHIVE_ONLY = {
    ".xls": "旧版 .xls 暂不支持自动提取文字，已归档",
    ".xlsx": "表格已归档（暂不提取单元格文字）",
    ".zip": "压缩包已归档（不自动解压）",
    ".rar": "压缩包已归档（不自动解压）",
    ".7z": "压缩包已归档（不自动解压）",
}

MAX_NAME = 120


def _cfg(cfg: Settings) -> dict:
    conf = (cfg.knowledge or {}).get("group_files") or {}
    return {
        "enabled": bool(conf.get("enabled", True)),
        "max_mb": int(conf.get("max_mb", 50) or 50),
        "max_per_run": int(conf.get("max_per_run", 10) or 10),
        "scan_downloads": bool(conf.get("scan_downloads", True)),
    }


def _safe_name(name: str) -> str:
    """去掉目录成分与非法字符，避免写出知识库目录。"""
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = name.replace("\x00", "")
    if not name or name in (".", ".."):
        name = "未命名文件"
    if len(name) > MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: MAX_NAME - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_NAME]
    return name


def _unique_target(dir_: Path, name: str) -> Path:
    target = dir_ / name
    if not target.exists():
        return target
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 1
    while True:
        cand = dir_ / (f"{stem}_{n}.{ext}" if ext else f"{stem}_{n}")
        if not cand.exists():
            return cand
        n += 1


def _downloads_index() -> dict[tuple[str, int], Path]:
    """Downloads 里「文件名+大小」→ 路径，用于消息里没带本地路径的情况。"""
    out: dict[tuple[str, int], Path] = {}
    root = Path.home() / "Downloads"
    if not root.is_dir():
        return out
    try:
        for p in root.rglob("*"):
            if p.is_file():
                try:
                    out.setdefault((p.name, p.stat().st_size), p)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _index_by_name(dl: dict[tuple[str, int], Path]) -> dict[str, list[Path]]:
    """按文件名再建一层索引（QQ 另存为/改名后大小不变但名字可能被加后缀）。"""
    out: dict[str, list[Path]] = {}
    for (name, _size), p in dl.items():
        out.setdefault(name, []).append(p)
    return out


def _resolve(row: dict, dl: dict[tuple[str, int], Path],
             by_name: dict[str, list[Path]] | None,
             scan_downloads: bool) -> Path | None:
    local = (row.get("local_path") or "").strip()
    if local:
        p = Path(local)
        if p.is_file():
            return p
    if scan_downloads:
        name = row.get("name") or ""
        size = int(row.get("size") or 0)
        if name and size:
            p = dl.get((name, size))
            if p:
                return p
        # 同名只有一个候选时也认（QQ 给的 size 偶尔为 0，或文件被改过大小）
        cands = (by_name or {}).get(name) or []
        if len(cands) == 1:
            return cands[0]
    return None


def _write_md(cfg: Settings, src: Path, text: str) -> str:
    """清洗后的文字稿写进 transcripts/，返回相对路径。"""
    root = cfg.resolve((cfg.knowledge or {}).get("dir", "data/knowledge"))
    tdir = root / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    name = _unique_target(tdir, f"{src.stem}.md")
    name.write_text(clean_to_md(text, src.stem), encoding="utf-8")
    return f"transcripts/{name.name}"


def process_pending(db: Database, cfg: Settings, *,
                    group_id: int | None = None,
                    limit: int | None = None) -> dict:
    """把 pending/missing 的群文件尽量入库。返回统计。"""
    conf = _cfg(cfg)
    result = {"ok": True, "scanned": 0, "ingested": 0, "archived": 0,
              "missing": 0, "skipped": 0, "duplicate": 0, "errors": 0,
              "detail": ""}
    if not conf["enabled"]:
        result["detail"] = "群文件入库已在配置中关闭（knowledge.group_files.enabled=false）"
        return result

    cap = max(1, int(limit or conf["max_per_run"]))
    max_bytes = conf["max_mb"] * 1024 * 1024

    rows: list[dict] = []
    # 待处理的优先；名额用完后不再挤占，避免「一堆老的 missing 每轮都排前面、
    # 新收到的文件永远轮不到」——没下载过的旧文件单独轮转重试（按上次检查时间）。
    pending = db.list_group_files(status="pending", group_id=group_id, limit=1000)
    pending.sort(key=lambda r: (r.get("msg_ts") or 0, r.get("name") or ""))
    rows = pending[:cap]
    if len(rows) < cap:
        # 以前「只能归档」但现在能提取的（例如升级后支持 .doc / 扫描版 PDF OCR）：
        # 重新排队处理一次，成功后状态转为 ingested。放在 missing 之前，
        # 否则一堆旧的「未下载」会占满名额，新能力永远轮不到。
        archived = [r for r in db.list_group_files(status="archived",
                                                   group_id=group_id, limit=1000)
                    if Path(r.get("name") or "").suffix.lower() in _EXTRACTABLE]
        archived.sort(key=lambda r: (r.get("updated_at") or 0, r.get("msg_ts") or 0))
        rows.extend(archived[: cap - len(rows)])
    if len(rows) < cap:
        # 「复制失败」通常是文件被占用/临时权限问题，下一轮应该重试。
        errs = db.list_group_files(status="error", group_id=group_id, limit=1000)
        errs.sort(key=lambda r: (r.get("updated_at") or 0, r.get("msg_ts") or 0))
        rows.extend(errs[: cap - len(rows)])
    if len(rows) < cap:
        missing = db.list_group_files(status="missing", group_id=group_id,
                                      limit=1000)
        # 优先重试「本机已经有副本」的：用户可能刚在 QQ 里下载完。
        # 否则这些行会排在旧 missing 后面，要等好几轮才轮到。
        dl_probe = _downloads_index() if conf["scan_downloads"] else {}
        name_probe = _index_by_name(dl_probe)
        ready = []
        for r in missing:
            if r.get("local_path") and Path(r["local_path"]).is_file():
                ready.append(r)
            elif _resolve(r, dl_probe, name_probe, conf["scan_downloads"]):
                ready.append(r)
        ready.sort(key=lambda r: (r.get("msg_ts") or 0), reverse=True)
        if ready:
            picked = ready[: cap - len(rows)]
            rows.extend(picked)
            taken = {(r["group_id"], r["msg_ts"], r["name"]) for r in picked}
            missing = [r for r in missing
                       if (r["group_id"], r["msg_ts"], r["name"]) not in taken]
        missing.sort(key=lambda r: (r.get("updated_at") or 0,
                                    r.get("msg_ts") or 0))
        rows.extend(missing[: cap - len(rows)])

    dl = _downloads_index() if conf["scan_downloads"] else {}
    by_name = _index_by_name(dl)
    root = cfg.resolve((cfg.knowledge or {}).get("dir", "data/knowledge"))
    ddir = root / "documents"

    for row in rows:
        result["scanned"] += 1
        gid = int(row["group_id"])
        allowed = allows_buddy(cfg, gid) if gid < 0 else allows_group(cfg, gid)
        if not allowed:
            db.update_group_file(gid, row["msg_ts"], row["name"],
                                 status="skipped", detail="群/好友已不在白名单")
            result["skipped"] += 1
            continue
        src = _resolve(row, dl, by_name, conf["scan_downloads"])
        if not src:
            db.update_group_file(gid, row["msg_ts"], row["name"],
                                 status="missing",
                                 detail="本机没有找到该文件（未在 QQ 里下载过）")
            result["missing"] += 1
            continue
        size = row.get("size") or src.stat().st_size
        if size > max_bytes:
            db.update_group_file(gid, row["msg_ts"], row["name"],
                                 status="skipped",
                                 detail=f"文件 {size / 1024 / 1024:.1f}MB 超过上限"
                                        f" {conf['max_mb']}MB")
            result["skipped"] += 1
            continue
        if row.get("md5") and db.group_file_done_md5(
                row["md5"], exclude=(gid, row["msg_ts"], row["name"])):
            db.update_group_file(gid, row["msg_ts"], row["name"],
                                 status="duplicate",
                                 detail="同一文件（md5 相同）已入库过",
                                 local_path=str(src))
            result["duplicate"] += 1
            continue

        try:
            ddir.mkdir(parents=True, exist_ok=True)
            # 重试「已归档」时原件通常已经在 documents/ 里：直接复用，
            # 不要再复制一份 xxx_1 出来。
            reuse = None
            old_doc = row.get("doc_path") or ""
            if old_doc.startswith("documents/"):
                cand = root / old_doc
                if cand.is_file():
                    reuse = cand
            if reuse is not None:
                target = reuse
            else:
                target = _unique_target(ddir, _safe_name(row["name"]))
                shutil.copy2(src, target)
            doc_rel = f"documents/{target.name}"
        except OSError as e:
            log.warning("群文件复制失败 %s：%s", row["name"], e)
            db.update_group_file(gid, row["msg_ts"], row["name"],
                                 status="error", detail=f"复制失败：{e}",
                                 local_path=str(src))
            result["errors"] += 1
            continue

        ext = target.suffix.lower()
        text = ""
        detail = ""
        if ext in _TEXTUAL:
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                detail = f"读取失败：{e}"
        elif ext in _EXTRACTABLE:
            text = extract_document_text(target)
            if not text.strip():
                if ext == ".doc":
                    detail = "未提取到文字（.doc 需要本机安装 Word 与 pywin32），已归档"
                elif ext == ".pdf":
                    detail = "未提取到文字（扫描件 OCR 也没识别出内容），已归档"
                else:
                    detail = "未提取到文字，已归档"
        else:
            detail = _ARCHIVE_ONLY.get(ext, "该格式不提取文字，已归档")

        md_rel = ""
        if text.strip():
            try:
                md_rel = _write_md(cfg, target, text)
            except OSError as e:
                detail = f"文字稿写入失败：{e}"

        status = "ingested" if md_rel else "archived"
        db.update_group_file(gid, row["msg_ts"], row["name"], status=status,
                             doc_path=doc_rel, md_path=md_rel,
                             detail=detail, local_path=str(src))
        result["ingested" if md_rel else "archived"] += 1
        log.info("群文件入库：%s → %s（%s）", row["name"], doc_rel,
                 md_rel or detail or "已归档")

    if result["ingested"]:
        mark_dirty(cfg, "index")

    parts = [f"检查 {result['scanned']} 个"]
    for k, label in (("ingested", "提取文字"), ("archived", "仅归档"),
                     ("missing", "未找到"), ("duplicate", "重复跳过"),
                     ("skipped", "跳过"), ("errors", "失败")):
        if result[k]:
            parts.append(f"{label} {result[k]}")
    result["detail"] = "，".join(parts)
    return result
