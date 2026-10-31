"""图片缩略图生成与缓存。

群里同步下来的照片动辄 5–15MB，知识库页/消息页直接铺原图会让浏览器一次拉几百 MB，
滚动和内存都会卡。这里按「源文件绝对路径 + mtime + 目标宽度」做磁盘缓存，
只生成一次，后续命中缓存直接返回。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

from PIL import Image, ImageOps

from .config import Settings

log = logging.getLogger("thumbs")

_CACHE_SUBDIR = Path("data") / "cache" / "thumbs"
_MAX_CACHE_BYTES = 512 * 1024 * 1024  # 缓存上限，超出后按 mtime 清旧
_JPEG_QUALITY = 82


def _cache_dir(cfg: Settings) -> Path:
    d = cfg.resolve(_CACHE_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(d: Path, extra: int) -> None:
    """缓存超过上限时，从最旧的文件开始删。"""
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in d.glob("*.jpg")]
    except OSError:
        return
    total = sum(s for _, s, _ in files) + extra
    if total <= _MAX_CACHE_BYTES:
        return
    files.sort()
    for _, size, p in files:
        if total <= _MAX_CACHE_BYTES:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass


def thumb_for(cfg: Settings, src: Path, width: int = 320) -> Path | None:
    """返回缩略图路径；无法生成（非图片/损坏）返回 None。"""
    try:
        st = src.stat()
    except OSError:
        return None
    width = max(120, min(720, int(width)))
    key = hashlib.sha1(
        f"{src.resolve()}|{st.st_mtime_ns}|{width}".encode("utf-8", "replace")
    ).hexdigest()[:24]
    out = _cache_dir(cfg) / f"{key}_{width}.jpg"
    if out.is_file() and out.stat().st_size > 0:
        return out

    tmp = out.with_suffix(".tmp")
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            im.thumbnail((width, width * 3), Image.LANCZOS)
            im.save(tmp, "JPEG", quality=_JPEG_QUALITY, optimize=True, progressive=True)
        os.replace(tmp, out)
        _prune(out.parent, out.stat().st_size)
        return out
    except Exception as e:  # 损坏图/缺解码器：退回原图即可
        log.warning("缩略图生成失败 %s：%s", src, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def thumb_url(section: str, name: str, width: int = 320, mtime: float | None = None) -> str:
    """模板里拼缩略图 URL 用（media 分区另走 /kb/thumb/media/...）。"""
    from urllib.parse import quote

    url = f"/kb/thumb/{section}/{quote(name)}?w={int(width)}"
    if mtime:
        url += f"&v={int(mtime)}"
    return url
