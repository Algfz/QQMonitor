"""网页抓取 → AI 可读 Markdown（支持微信公众号等）。

为什么单独一个模块
------------------
PDF/Word 的清洗走 `cleaning.clean_to_md`（合并断行），但**网页不能走那条路**：
网页本身已有结构（标题/列表/表格），`_merge_paragraphs` 会把表格行合并成
一段话，反而破坏信息。本模块保留原结构，只做轻度清洗。

抓取要点
--------
- **User-Agent 必带**：不带会被判为爬虫，返回验证页/空壳页
- **微信正文**在 `#js_content`；通用网页按 article/main/[role=main] 回退
- **微信图片是懒加载**：真实地址在 `data-src`，`src` 常为空或占位
- 图片可选下载到本地（默认开），Markdown 里用相对路径，网页能直接显示

用法：
    from app.webpage import fetch_markdown
    res = fetch_markdown(url, media_dir=Path("data/knowledge/media/web_xxx"))
    res["markdown"]   # str
"""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

log = __import__("logging").getLogger("webpage")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

_TIMEOUT = 25

# SSRF 防护（审计 P0-10）：只允许公网 http(s)，重定向逐跳校验
_MAX_HTML = 8 << 20      # 网页正文上限 8MB
_MAX_IMAGE = 8 << 20     # 单张图上限 8MB
_MAX_IMAGES = 60         # 单页最多下载 60 张
_MAX_REDIRECTS = 5

# 图片扩展名白名单（防把 .html/.svg 之类存成图片）
_IMG_EXT_MAP = {
    "jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "gif": ".gif",
    "webp": ".webp", "bmp": ".bmp",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def validate_url(url: str) -> tuple[bool, str]:
    """校验抓取地址：仅 http(s)，且解析出的每个 IP 都必须是公网地址。

    防 SSRF：没有这道校验，`/kb/from-url` 就能被用来读 127.0.0.1:8088、
    路由器后台、云元数据（审计 P0-10）。
    """
    try:
        p = urlparse((url or "").strip())
    except ValueError as e:
        return False, f"链接格式错误：{e}"
    if p.scheme not in ("http", "https"):
        return False, "只支持 http/https 链接"
    host = p.hostname or ""
    if not host:
        return False, "链接缺少域名"
    if host.lower() in ("localhost", "localhost.localdomain"):
        return False, "不允许抓取本机地址"
    try:
        infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return False, f"域名解析失败：{e}"
    ips = {info[4][0] for info in infos}
    for ip in ips:
        if not _is_public_ip(ip):
            return False, "不允许抓取内网/本机地址"
    return True, ""


def _read_limited(resp, limit: int) -> bytes:
    """按上限读取响应体，超限直接报错（不把超大文件读进内存）。"""
    total = 0
    chunks: list[bytes] = []
    for chunk in resp.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ValueError(f"内容超过 {limit // (1 << 20)}MB 上限")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_get(session, url: str, *, as_image: bool = False):
    """带 SSRF 校验的 GET：手动跟随重定向，逐跳校验目标地址。

    返回 (响应对象, 最终 URL, 响应体 bytes)。响应体受 _MAX_HTML/_MAX_IMAGE 限制。
    """
    for _ in range(_MAX_REDIRECTS + 1):
        ok, why = validate_url(url)
        if not ok:
            raise ValueError(why)
        r = session.get(url, timeout=_TIMEOUT, allow_redirects=False, stream=True)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            from urllib.parse import urljoin
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        limit = _MAX_IMAGE if as_image else _MAX_HTML
        try:
            body = _read_limited(r, limit)
        finally:
            r.close()
        return r, url, body
    raise ValueError("重定向次数过多")


def _clean_text(s: str) -> str:
    """去 HTML 实体残留与多余空白。"""
    import html as _h
    s = _h.unescape(s or "")
    return re.sub(r"\s+", " ", s).strip()


def slugify(text: str, maxlen: int = 24) -> str:
    """标题 → 安全文件名片段（中文保留，只替换非法字符）。

    长度要克制：微信标题常达 40+ 字，直接当文件名会导致路径过长、
    且 md 里图片链接难以阅读。
    """
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "").strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:maxlen] or "webpage"


def _pick_container(soup):
    """选正文容器：微信 #js_content 优先，其次 article/main。"""
    for sel in ("#js_content", "article", "main", '[role="main"]'):
        try:
            node = soup.select_one(sel)
        except Exception:
            continue
        if node and len(node.get_text(strip=True)) >= 80:
            return node
    return soup.body or soup


def _extract_title(soup, html: str) -> str:
    """按可靠性顺序取标题。"""
    # 微信：页面脚本里最准
    m = re.search(r"var\s+msg_title\s*=\s*['\"](.{1,200}?)['\"]", html)
    if m:
        return _clean_text(m.group(1))
    tag = soup.find("meta", property="og:title")
    if tag and tag.get("content"):
        return _clean_text(tag["content"])
    for sel in ("#activity-name", "h1"):
        node = soup.select_one(sel)
        if node:
            t = _clean_text(node.get_text())
            if t:
                return t
    if soup.title:
        return _clean_text(soup.title.get_text())
    return ""


def _fix_img_src(container) -> None:
    """微信图片真实地址在 data-src（懒加载），统一搬到 src。"""
    for img in container.find_all("img"):
        real = img.get("data-src") or img.get("data-original") or img.get("src")
        if real:
            img["src"] = real


def _guess_ext(url: str) -> str:
    """从 URL 猜图片扩展名（微信用 wx_fmt 参数）。"""
    m = re.search(r"wx_fmt=([a-zA-Z]+)", url or "")
    if m:
        key = m.group(1).lower()
        if key in _IMG_EXT_MAP:
            return _IMG_EXT_MAP[key]
    path = urlparse(url or "").path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def _download_images(container, img_dir: Path, session) -> int:
    """下载正文图片到 img_dir，并把 img[src] 改成本地文件名。

    返回**成功**下载张数（旧实现返回尝试数，统计虚高）。
    单张失败不影响整页；每张图同样走 SSRF 校验与大小上限。
    """
    img_dir.mkdir(parents=True, exist_ok=True)
    ok_n = 0
    seen = 0
    for img in container.find_all("img"):
        if ok_n >= _MAX_IMAGES:
            log.warning("图片超过 %d 张上限，其余保留外链", _MAX_IMAGES)
            break
        src = (img.get("src") or "").strip()
        if not src.startswith(("http://", "https://")):
            continue
        seen += 1
        name = f"{seen}{_guess_ext(src)}"
        try:
            r, _final, body = _safe_get(session, src, as_image=True)
            if r.status_code == 200 and body:
                (img_dir / name).write_bytes(body)
                img["src"] = name          # 转 md 前改成本地文件名
                ok_n += 1
        except Exception as e:
            log.debug("图片下载失败 %s: %s", src[:80], e)
    return ok_n


def _tidy_markdown(md: str) -> str:
    """轻度清洗：压缩空行、合并相邻加粗、去行尾空白。

    不做「合并断行」——那会破坏表格与列表（clean_to_md 只适合纯文本）。

    微信正文大量用嵌套 <span><strong>，会产出 `**A****B**` 这种相邻
    加粗块（如「**本期编辑****：某编辑**」），需合并成 `**A B**` 才可读。
    """
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    # 兜底：_flatten_inline 已处理 DOM 层，这里只清理残余的 `****`（相邻加粗边界）
    md = md.replace("****", "")
    lines = [re.sub(r"[ \t]+$", "", ln) for ln in md.split("\n")]
    # 3 个以上连续空行压成 2 个
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln.strip():
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank <= 2:
                out.append("")
    return "\n".join(out).strip() + "\n"


def _fill_img_alt(container) -> None:
    """给没有 alt 的图片补描述（微信常是空 alt），便于 AI 理解图片。"""
    n = 0
    for img in container.find_all("img"):
        n += 1
        if not (img.get("alt") or "").strip():
            img["alt"] = f"图片{n}"


def _flatten_inline(container) -> None:
    """扁平化微信的嵌套 span/strong，避免产出碎片加粗。

    微信正文常用 `<span><strong>A</strong></span><span><strong>B</strong></span>`
    排版，直接转 md 会得到 `**A****B**`——邮箱被拆成 `edu.**c**n`、
    「本期编辑：某某」被拆成两段，**AI 读起来是错的**。
    这里在 DOM 层根治：unwrap span → unwrap 嵌套 strong → 合并相邻 strong。
    """
    # 1) span 只是样式容器，去掉不影响语义
    for sp in list(container.find_all("span")):
        sp.unwrap()
    # 2) 嵌套 strong 保留最外层
    for st in list(container.find_all(["strong", "b"])):
        p = st.parent
        if p is not None and p.name in ("strong", "b"):
            st.unwrap()
    # 3) 合并相邻 strong（可能 3 个以上相连，循环到稳定）
    for _ in range(6):
        merged = False
        for st in list(container.find_all(["strong", "b"])):
            nxt = st.next_sibling
            while nxt is not None and isinstance(nxt, str) and not nxt.strip():
                nxt = nxt.next_sibling
            if nxt is not None and getattr(nxt, "name", None) in ("strong", "b"):
                st.append(nxt.get_text())
                nxt.decompose()
                merged = True
        if not merged:
            break


def fetch_markdown(url: str, media_dir: Path | None = None,
                   rel_prefix: str = "../media",
                   download_images: bool = True) -> dict:
    """抓取网页并转成 AI 可读 Markdown。

    返回 {ok, title, markdown, url, images, error}
    - media_dir: 图片保存目录（如 data/knowledge/media/web_xxx）；None 则不下载
    - rel_prefix: Markdown 里图片的相对路径前缀（md 在 transcripts/ 时用 ../media）
    """
    res = {"ok": False, "title": "", "markdown": "", "url": url,
           "images": 0, "error": ""}
    try:
        import requests
        from bs4 import BeautifulSoup
        from markdownify import markdownify
    except ImportError as e:
        res["error"] = f"缺少依赖：{e}（pip install requests beautifulsoup4 markdownify）"
        return res

    ok, why = validate_url(url)
    if not ok:
        res["error"] = why
        return res

    session = requests.Session()
    session.headers.update({
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    try:
        r, final_url, body = _safe_get(session, url)
        if r.status_code != 200:
            res["error"] = f"网页返回 {r.status_code}"
            return res
        # 编码：优先响应头，其次自动探测（微信等中文站点常缺 charset）
        enc = r.encoding or ""
        if not enc:
            try:
                from charset_normalizer import from_bytes
                best = from_bytes(body).best()
                enc = (best.encoding if best else "") or "utf-8"
            except Exception:  # noqa: BLE001
                enc = "utf-8"
        try:
            html = body.decode(enc, "replace")
        except LookupError:
            html = body.decode("utf-8", "replace")
        res["url"] = final_url
    except Exception as e:
        res["error"] = f"抓取失败：{type(e).__name__}: {e}"
        return res

    try:
        soup = BeautifulSoup(html, "lxml")
        title = _extract_title(soup, html)
        container = _pick_container(soup)

        # 去掉脚本/样式等噪声
        for tag in container.find_all(
                ["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()

        _fix_img_src(container)
        _fill_img_alt(container)
        _flatten_inline(container)   # 必须在转 md 前做，否则产生碎片加粗

        img_count = 0
        if download_images and media_dir is not None:
            img_count = _download_images(container, media_dir, session)

        md = markdownify(
            str(container),
            heading_style="ATX",
            bullets="-",
        )
        md = _tidy_markdown(md)

        # 图片路径拼上前缀（下载成功的现在是纯文件名）
        if img_count and media_dir is not None:
            folder = media_dir.name
            md = re.sub(
                r"!\[([^\]]*)\]\((?!http)([^)]+)\)",
                lambda m: f"![{m.group(1)}]({rel_prefix}/{folder}/{m.group(2)})",
                md,
            )

        res.update({"ok": True, "title": title, "markdown": md,
                    "images": img_count})
        return res
    except Exception as e:
        res["error"] = f"解析失败：{type(e).__name__}: {e}"
        return res
