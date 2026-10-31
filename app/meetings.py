"""腾讯会议录制文件归档与会议记录。"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger("meetings")

# 录制/录音文件扩展名（只放真正的媒体文件）
#
# 注意：这里不要放 .txt / .srt / .vtt —— 它们与 knowledge._TRANSCRIPT_EXTS 重叠，
# 会导致 inbox 分拣时被 sync_inbox() 与 archive_inbox() 争抢，去向取决于执行顺序。
# 按使用约定：聊天记录只经 bak 导入，故 .txt 一律是可读文档/文字稿 → 归 transcripts/；
# 会议转写是拖入录屏/录音（下面的媒体格式）后自动转写出的 AI 可读版本。
RECORDING_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi",
    ".wemv", ".wem", ".m4a", ".mp3", ".wav", ".aac", ".ogg",
    ".m3u8",
}

# 腾讯会议本地录制文件名形如 recording_20260830_193000_xxxx.wemv
_TC_DATE_RE = re.compile(r"(?:recording|meeting)_(\d{8})", re.IGNORECASE)


def extract_date_from_name(name: str) -> datetime | None:
    m = _TC_DATE_RE.search(name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d")
        except ValueError:
            return None
    return None


def archive_inbox(inbox_dir: Path, meeting_dir: Path, dry_run: bool = False) -> list[dict]:
    """扫描入站目录，把录制文件按日期移动到归档目录。

    返回处理记录列表：[{name, target, moved}]。
    """
    inbox_dir = Path(inbox_dir)
    if not inbox_dir.exists():
        log.warning("入站目录不存在: %s", inbox_dir)
        return []
    results: list[dict] = []
    for f in sorted(inbox_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in RECORDING_EXTS:
            continue
        dt = extract_date_from_name(f.name)
        if dt is None:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            dt = mtime
        day_dir = meeting_dir / dt.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        target = day_dir / f.name
        if target.exists():
            # 同名避免覆盖，加时间后缀
            target = day_dir / f"{f.stem}_{datetime.now().strftime('%H%M%S')}{f.suffix}"
        if dry_run:
            log.info("[试运行] %s -> %s", f.name, target)
            results.append({"name": f.name, "target": str(target), "moved": False})
        else:
            shutil.move(str(f), str(target))
            log.info("归档 %s -> %s", f.name, target)
            results.append({"name": f.name, "target": str(target), "moved": True})
    return results


def find_tencent_meeting_recordings() -> list[Path]:
    """尝试定位腾讯会议默认录制目录（Documents\\Tencent Meeting\\Recording）。"""
    candidates = [
        Path.home() / "Documents" / "Tencent Meeting" / "Recording",
        Path.home() / "Documents" / "TencentMeeting" / "Recording",
        Path.home() / "OneDrive" / "Documents" / "Tencent Meeting" / "Recording",
    ]
    found = [p for p in candidates if p.exists()]
    return found
