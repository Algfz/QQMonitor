"""每日摘要生成与 Markdown 渲染。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .db import Database

log = logging.getLogger("digest")


def generate_digest(db: Database, digest_dir: Path, day: str | None = None) -> dict:
    """生成某天的群消息摘要，写入 digest_dir，返回 {day, markdown, path}。"""
    day = day or datetime.now().strftime("%Y-%m-%d")
    groups = db.day_summary(day)

    lines: list[str] = []
    lines.append(f"# {day} 群消息摘要")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    if not groups:
        lines.append("（当天没有记录到任何群消息）")
    for g in groups:
        msgs = g["messages"]
        imp = [m for m in msgs if m["important"]]
        active = len(g["users"])
        lines.append(f"## {g['group_name']}（群号 {g['group_id']}）")
        lines.append("")
        lines.append(f"- 消息 {len(msgs)} 条，活跃成员 {active} 人，重要消息 {len(imp)} 条")
        lines.append("")
        if imp:
            lines.append("### [重要消息]")
            for m in imp:
                flag = f"（{m['important_reason']}）" if m["important_reason"] else ""
                lines.append(f"- [{m['time_str'][11:16]}] **{m['nickname']}**: {m['content']} {flag}")
            lines.append("")
        lines.append("### 全部消息")
        for m in msgs:
            star = "[重要] " if m["important"] else ""
            lines.append(f"- [{m['time_str'][11:16]}] {star}**{m['nickname']}**: {m['content']}")
        lines.append("")

    markdown = "\n".join(lines)
    digest_dir = Path(digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)
    out_path = digest_dir / f"{day}.md"
    out_path.write_text(markdown, encoding="utf-8")
    log.info("摘要已生成: %s", out_path)
    return {"day": day, "markdown": markdown, "path": str(out_path)}


def list_digests(digest_dir: Path) -> list[str]:
    digest_dir = Path(digest_dir)
    if not digest_dir.exists():
        return []
    return sorted((p.stem for p in digest_dir.glob("*.md")), reverse=True)


def md_to_html(md: str) -> str:
    """极简 Markdown 转 HTML（标题/列表/加粗/引用/代码/链接/图片/表格）。"""
    import html as _html
    import re

    _LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    _IMG_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp)$", re.I)
    # 协议白名单：http/https 外链，或无协议的相对路径（../media/...）。
    # 用白名单而非黑名单，否则 javascript/data/vbscript/file 等挡不全。
    _SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+\-.]*):")
    _SAFE_SCHEMES = {"http", "https"}

    def _normalize_href(href: str) -> str:
        """去掉浏览器解析 URL 时会忽略的控制字符。

        否则 "jav\\tascript:" 之类写法能绕过协议校验（浏览器会当 javascript: 执行）。
        """
        return re.sub(r"[\x00-\x20]", "", href.strip())

    def _is_safe_href(href: str) -> bool:
        """协议白名单校验。逐层 HTML 实体解码后再判，防止 &#106;avascript: 之类
        编码写法绕过（纵深防御：本函数上游已转义，但多判几层更稳妥）。"""
        h = href
        for _ in range(3):
            m = _SCHEME_RE.match(h)
            if m:  # 带协议：必须在白名单内
                return m.group(1).lower() in _SAFE_SCHEMES
            prev, h = h, _html.unescape(h)
            if h == prev:  # 解码已收敛，无更多实体
                break
        m = _SCHEME_RE.match(h)
        if m:
            return m.group(1).lower() in _SAFE_SCHEMES
        return True  # 无协议：相对路径或纯文件名

    def link_or_img(text: str) -> str:
        """[文字](相对路径) → <a>；图片扩展名 → 内联 <img>。

        约定：text 已过 _esc()，故不可再整体 escape（否则 & 会被二次转义成
        &amp;amp;，导致 a.pdf?x=1&y=2 这类链接失效）。只需额外把引号转成实体，
        防止从属性值里逃逸出标签。
        """
        def attr(s: str) -> str:
            return s.replace('"', "&quot;").replace("'", "&#39;")

        def thumb_href(href: str) -> str:
            """把媒体原图链接换成缩略图链接，避免一页聊天记录拖几百 MB 原图。"""
            m = re.search(r"(?:^|/)media/([^/]+)/(?:[^/]+/)?([^/]+)$", href.replace("\\", "/"))
            if not m:
                return href
            gid, name = m.group(1), m.group(2)
            from urllib.parse import quote

            return f"/kb/thumb/media/{gid}/{quote(name)}?w=560"

        def rep(m: re.Match) -> str:
            href = _normalize_href(m.group(2))
            if not _is_safe_href(href):
                return m.group(0)  # 危险协议：保持纯文本（已转义，安全）
            label = attr(m.group(1))
            href_attr = attr(href)
            if _IMG_EXT_RE.search(href):
                small = attr(thumb_href(href))
                return (f'<a href="{href_attr}" target="_blank" rel="noopener">'
                        f'<img src="{small}" alt="{label}" class="md-img" '
                        f'loading="lazy" decoding="async"></a>')
            return (f'<a href="{href_attr}" target="_blank" rel="noopener">'
                    f'{label}</a>')
        return _LINK_RE.sub(rep, text)

    def inline(text: str) -> str:
        """段落/标题/引用/列表项的通用行内渲染：转义 → 加粗 → 链接/图片。"""
        content = _esc(text)
        content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
        return link_or_img(content)

    def cell_html(text: str) -> str:
        """表格单元格内容：转义 → 加粗 → 链接。"""
        content = _esc(text)
        content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
        return link_or_img(content)

    def is_table_row(line: str) -> bool:
        """是否像表格数据行：包含至少两个 | 分隔段。"""
        return line.count("|") >= 2 and "|" in line.strip("|")

    def is_table_sep(line: str) -> bool:
        """表格分隔行：形如 | --- | ---: | :---: |，只允许 - : | 和空白。"""
        stripped = line.strip()
        return (is_table_row(stripped) and
                all(c in "-:| \t" for c in stripped))

    def parse_table(rows: list[str]) -> str:
        """把表头+分隔+数据行解析成 HTML table。rows[1] 为分隔行，不参与渲染。"""
        def split_cells(line: str) -> list[str]:
            return [c.strip() for c in line.strip().strip("|").split("|")]

        heads = split_cells(rows[0])
        cols = len(heads)
        html = ['<div style="overflow-x:auto"><table><thead><tr>']
        for h in heads:
            html.append(f"<th>{cell_html(h)}</th>")
        html.append('</tr></thead><tbody>')
        for row in rows[2:]:
            if not is_table_row(row):
                continue
            cells = split_cells(row)
            html.append('<tr>')
            for i in range(cols):
                cell = cells[i] if i < len(cells) else ""
                html.append(f"<td>{cell_html(cell)}</td>")
            html.append('</tr>')
        html.append('</tbody></table></div>')
        return "".join(html)

    lines = md.splitlines()
    out: list[str] = []
    in_list = False
    i = 0
    n = len(lines)

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < n:
        s = lines[i].rstrip()
        if not s:
            close_list()
            out.append("")
            i += 1
            continue

        # 表格：需要预读下一行确认是分隔线
        if is_table_row(s):
            j = i + 1
            while j < n and is_table_row(lines[j].rstrip()):
                j += 1
            rows = [lines[k].rstrip() for k in range(i, j)]
            if len(rows) >= 2 and is_table_sep(rows[1]):
                close_list()
                out.append(parse_table(rows))
                i = j
                continue

        if s.startswith("# "):
            close_list(); out.append(f"<h1>{inline(s[2:])}</h1>")
        elif s.startswith("## "):
            close_list(); out.append(f"<h2>{inline(s[3:])}</h2>")
        elif s.startswith("### "):
            close_list(); out.append(f"<h3>{inline(s[4:])}</h3>")
        elif s.startswith("> "):
            close_list(); out.append(f"<blockquote>{inline(s[2:])}</blockquote>")
        elif s.startswith("- ") or s.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(s[2:])}</li>")
        elif s.startswith("```"):
            close_list(); out.append("<pre>")
        else:
            close_list(); out.append(f"<p>{inline(s)}</p>")
        i += 1
    close_list()
    return "\n".join(out)


def _esc(s: str) -> str:
    import html
    return html.escape(s, quote=False)
