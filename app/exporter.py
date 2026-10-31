"""CSV 导出（utf-8-sig，Excel 直接打开不乱码）。"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from .db import Database

log = logging.getLogger("exporter")


# 单次导出上限。超过时截断并如实告知，避免"导出不全却无任何提示"误导使用者。
MAX_EXPORT_ROWS = 100000


def export_csv(db: Database, out_path: Path, group_id: int | None = None,
               keyword: str = "", date_from: str = "", date_to: str = "",
               max_rows: int = MAX_EXPORT_ROWS) -> dict:
    # 多查一条用于探测是否被截断（query_messages 按 ts DESC，故前 max_rows 条即最新的）
    rows = db.query_messages(
        group_id=group_id, keyword=keyword,
        date_from=date_from or None, date_to=date_to or None,
        limit=max_rows + 1,
    )
    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    rows = list(reversed(rows))  # 时间正序

    group_name = ""
    names = {g["group_id"]: g["name"] for g in db.get_groups()}
    if group_id:
        g = db.get_group(group_id)
        group_name = g["name"] if g else f"群{group_id}"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 列必须与 importer.parse_csv 对齐（审计 P0-6）：
    # 导出的 CSV 要能原样导回，所以群号/类型/重要/重要原因都要带上。
    # 多群一起导出时，群号/群名按行写各自的值，否则回导会全塞进同一个群。
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["时间", "群号", "群名", "发送者QQ", "昵称",
                         "类型", "内容", "重要", "重要原因"])
        for m in rows:
            gid = m["group_id"]
            writer.writerow([
                m["time_str"], gid, names.get(gid, group_name or f"群{gid}"),
                m["user_id"], m["nickname"], m["msg_type"], m["content"],
                "是" if m["important"] else "",
                m.get("important_reason") or "",
            ])
    if truncated:
        log.warning("导出被截断：匹配结果超过 %d 条，已只导出最新 %d 条，"
                    "建议缩小筛选范围（指定群 / 日期区间 / 关键词）", max_rows, max_rows)
    return {"rows": len(rows), "path": str(out_path),
            "truncated": truncated, "limit": max_rows}
