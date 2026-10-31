"""把 OCR 的文字块还原成 Markdown 结构（表格 / 列表 / 段落 / 公式行）。

RapidOCR 只输出「文字 + 坐标」，不含任何版面信息。本模块用坐标做几何聚类：

    1. 行聚类：y 中心相近或垂直投影重叠 → 同一行
    2. 表格检测：连续多行、列数一致且横向对齐 → 还原成 Markdown 表格
    3. 列表项识别：行首是 • / - / 1. 等符号
    4. 段落合并：行尾无句末标点则与下一行相连（中文不加空格）
    5. 公式行保护：疑似公式的行独立成段，不与上下文粘连
    6. 退化质检：VLM/OCR 输出退化（空表刷屏、复读机、版式塌陷）时判定丢弃，
       不让畸形 Markdown 写进知识库（见 `ocr_degenerate_reason`）

能力边界（重要）
----------------
- **表格可以还原**：靠 x 坐标聚类列边界。对齐良好的表格效果好；
  合并单元格、斜线表头、跨页表格会串列或错位。
- **公式 → LaTeX 做不到**：那是公式识别模型（pix2tex / LaTeX-OCR / Nougat）
  的活，纯 OCR 只能给出字符。本模块做的是「保护」：让公式行不被段落合并
  拆散，并可选地用 `$...$` 标记为行内公式（默认关闭，启发式有误判）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ocr.layout")

# 行首列表符号
_BULLET_RE = re.compile(
    r"^\s*(?:[•·※◦‣▪\-–—*\u25cf\u25cb\u25a0\u25a1]"
    r"|(?:\d+|[一二三四五六七八九十]+)\s*[.、)）]"
    r"|[（(]\s*(?:\d+|[一二三四五六七八九十]+)\s*[)）])"
    r"\s*\S")

# 句末标点：出现即视为段落结束
_SENT_END = "。！？!?；;…"

# 数学/公式特征字符
_MATH_CHARS = set("=+−×÷∑∫∬√±≤≥≠≈∈∉⊂⊃∪∩→⇒⇔∞∂∇"
                  "αβγδεζηθικλμνξπρστυφχψω"
                  "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΠΡΣΤΥΦΧΨΩ"
                  "^_{}[]()\\|/")
# 出现即高度疑似公式（普通中文/英文文本几乎不会用到）
_STRONG_MATH = set("∑∫∬∭√≤≥≠≈∈∉⊂⊃∪∩⇒⇔∞∂∇"
                   "αβγδεζηθικλμνξπρστυφχψω"
                   "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΠΡΣΤΥΦΧΨΩ")

# 单张图 OCR 文字的硬上限，防止异常图片刷爆聊天文件
MAX_OCR_CHARS = 3000


# ---------------------------------------------------------------------------
# 退化输出质检
# ---------------------------------------------------------------------------
# 背景：本地 VLM（Qwen2.5-VL-3B-Instruct 4bit）在「空表格 / 花名册」类截图上会退化，
# 典型两种形态：
#   a) 刷空单元格——`|  |  |  |` 重复几百次，整张表没有一个字
#   b) 复读机——把同一张表（内容是真实的）连续输出七八遍，总长上万字符
# 这些文本一旦写进 *_OCR.md 就会被切块进向量库，检索时全是噪声。
# 质检必须在**写盘之前**做（在 app/vision.py:analyze_image 里拦），不能事后清洗。
#
# 阈值来自实测：data/knowledge/transcripts 下 21 个 *_OCR.md（4 个退化 / 17 个正常），
# 阈值按这批样本定，样本少，换数据集时建议重新校准。
#   退化样本：最长行 2627~8525 字、行数均为 5、12 字滑窗去重率 0.09~0.21、
#             非空单元格占比 0.002~0.86（复读型的单元格是满的，所以还要靠去重率）
#   正常样本：最长行 ≤ 625 字、行数 3~29、去重率 ≥ 0.50、非空单元格 ≥ 0.48
# 阈值取在两者空档里，每条判定都留了 2 倍以上余量：宁可放过，不误杀。
DEGENERATE_NOTE = "（该图片疑似表格/版式，自动识别结果不可用，已跳过）"

DEGEN_MIN_CHARS = 400          # 短于此不判定：一条短通知不该被误杀
DEGEN_CELL_MIN_TOKENS = 100    # 按 | 切出的片段少于 100 个，不算「表格刷屏」
DEGEN_EMPTY_CELL_RATIO = 0.05  # 非空单元格占比 < 5% → 空表刷屏
DEGEN_SHINGLE_LEN = 12         # 复读检测滑窗：12 字约半个中文分句
DEGEN_SHINGLE_RATIO = 0.35     # 去重率 < 0.35 → 复读机（正常最低实测 0.50）
DEGEN_MAX_LINES = 6            # 行数 ≤ 6 且
DEGEN_MAX_LINE_CHARS = 1200    # 最长行 ≥ 1200 字 且
DEGEN_MAX_LINE_SHARE = 0.60    # 最长行占全文 ≥ 60% → 整篇压成一块（版式塌陷）


def _cell_stats(text: str) -> tuple[int, float]:
    """按 `|` 切分表格片段，返回 (片段数, 非空占比)。无竖线时返回 (0, 1.0)。"""
    toks = [t.strip() for t in text.split("|")]
    if len(toks) <= 1:
        return 0, 1.0
    # `---` / `:--:` 这类对齐分隔行不算内容
    nonempty = sum(1 for t in toks if t and set(t) - set("-: \t"))
    return len(toks), nonempty / len(toks)


def _shingle_ratio(text: str, n: int = DEGEN_SHINGLE_LEN) -> float:
    """n 字滑窗去重率（去空白后计）。复读机型退化会把它压到 0.2 以下。"""
    s = "".join(text.split())
    if len(s) < n:
        return 1.0
    grams = [s[i:i + n] for i in range(len(s) - n + 1)]
    return len(set(grams)) / len(grams)


def ocr_degenerate_reason(text: str) -> str:
    """判断 VLM/OCR 输出是否退化。

    返回 "" 表示正常；否则返回一句人话原因（供日志与笔记用）。
    三条判定彼此独立，任意一条命中即视为退化——它们抓的是不同的退化形态。
    """
    t = (text or "").strip()
    if len(t) < DEGEN_MIN_CHARS:
        return ""

    # 1) 空表刷屏：几百个单元格里几乎没有非空内容
    tokens, nonempty = _cell_stats(t)
    if tokens >= DEGEN_CELL_MIN_TOKENS and nonempty < DEGEN_EMPTY_CELL_RATIO:
        return f"空表刷屏（{tokens} 个表格片段，非空仅 {nonempty:.1%}）"

    # 2) 复读机：同一段内容被重复输出很多遍
    ratio = _shingle_ratio(t)
    if ratio < DEGEN_SHINGLE_RATIO:
        return f"内容复读（{DEGEN_SHINGLE_LEN} 字滑窗去重率 {ratio:.1%}）"

    # 3) 版式塌陷：几乎没有换行，整篇挤成一两块
    lines = t.splitlines()
    maxln = max((len(x) for x in lines), default=0)
    if (len(lines) <= DEGEN_MAX_LINES
            and maxln >= DEGEN_MAX_LINE_CHARS
            and maxln / len(t) >= DEGEN_MAX_LINE_SHARE):
        return f"版式塌陷（{len(lines)} 行，最长一行 {maxln} 字）"

    return ""



@dataclass
class _Item:
    """一个 OCR 文字块及其几何信息。"""

    text: str
    x0: float
    x1: float
    y0: float
    y1: float

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


def _to_items(boxes: list[tuple]) -> list[_Item]:
    """RapidOCR 原始结果 → _Item 列表（按 y 中心排序）。"""
    items: list[_Item] = []
    for box, text, _score in boxes:
        t = (text or "").strip()
        if not t:
            continue
        if box is not None and len(box) >= 4:
            try:
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
            except (TypeError, ValueError, IndexError):
                continue
            items.append(_Item(t, min(xs), max(xs), min(ys), max(ys)))
        else:
            # 无坐标（少数 OCR 版本）：退化成顺序文本，y 用序号占位
            items.append(_Item(t, 0.0, 1.0, float(len(items)),
                               float(len(items)) + 1))
    items.sort(key=lambda it: (it.yc, it.x0))
    return items


def _group_lines(items: list[_Item]) -> list[list[_Item]]:
    """把文字块聚成行：垂直投影重叠或 y 中心足够接近即同一行。"""
    if not items:
        return []
    heights = [it.height for it in items]
    avg_h = sum(heights) / len(heights)
    tol = max(4.0, avg_h * 0.6)

    lines: list[list[_Item]] = []
    cur: list[_Item] = [items[0]]
    max_y = items[0].y1
    min_y = items[0].y0
    for it in items[1:]:
        overlap = it.y0 < max_y and it.y1 > min_y
        if overlap or abs(it.yc - cur[-1].yc) <= tol:
            cur.append(it)
        else:
            lines.append(cur)
            cur = [it]
            max_y, min_y = it.y1, it.y0
            continue
        max_y = max(max_y, it.y1)
        min_y = min(min_y, it.y0)
    lines.append(cur)

    for line in lines:
        line.sort(key=lambda it: it.x0)
    return lines


def _table_ranges(lines: list[list[_Item]]) -> list[tuple[int, int]]:
    """找出表格区间：连续 ≥2 行、每行 ≥2 块、列数波动 ≤1。"""
    ranges: list[tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if len(lines[i]) < 2:
            i += 1
            continue
        j = i
        while j < n and len(lines[j]) >= 2:
            j += 1
        # 允许尾部再吸收一行单列（例如表格下方的注释被算进来会破坏结构，故不吸收）
        if j - i >= 2:
            counts = [len(lines[k]) for k in range(i, j)]
            if max(counts) - min(counts) <= 1 and max(counts) >= 2:
                ranges.append((i, j))
        i = j
    return ranges


def _cluster_columns(lines: list[list[_Item]]) -> list[float]:
    """在表格区间内用 x 起点聚类，得到各列的参考 x 坐标。"""
    xs = sorted(it.x0 for line in lines for it in line)
    if not xs:
        return []
    heights = [it.height for line in lines for it in line]
    avg_h = sum(heights) / len(heights) if heights else 20.0
    gap = max(12.0, avg_h * 1.2)  # 列间距阈值：小于它视为同一列

    cols: list[float] = []
    cur = [xs[0]]
    for x in xs[1:]:
        if x - cur[-1] <= gap:
            cur.append(x)
        else:
            cols.append(sum(cur) / len(cur))
            cur = [x]
    cols.append(sum(cur) / len(cur))
    return cols


def _cell(text: str) -> str:
    """表格单元格内容：竖线会破坏表格结构，需转义。"""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _render_table(lines: list[list[_Item]]) -> str:
    """把表格区间的行渲染成 Markdown 表格。"""
    cols = _cluster_columns(lines)
    if len(cols) < 2:
        return ""

    rows: list[list[str]] = []
    for line in lines:
        cells = [""] * len(cols)
        for it in line:
            k = min(range(len(cols)), key=lambda c: abs(it.x0 - cols[c]))
            cells[k] = f"{cells[k]} {it.text}".strip() if cells[k] else it.text
        rows.append([_cell(c) for c in cells])

    # 至少要有 2 行（表头 + 数据）才成表格；只有一行时调用方已排除
    head = rows[0]
    out = ["| " + " | ".join(head) + " |",
           "| " + " | ".join("---" for _ in head) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _looks_like_formula(text: str) -> bool:
    """启发式：是否像一整行公式。"""
    if not (2 <= len(text) <= 80):
        return False
    if any(c in text for c in _STRONG_MATH):
        return True
    n = sum(1 for c in text if c in _MATH_CHARS)
    # 数学符号占比够高（且不含中文，中文句子里常有 = 、括号）
    if n >= max(3, len(text) * 0.2) and not re.search(r"[\u4e00-\u9fff]", text):
        return True
    return False


def _lines_to_md(lines: list[list[_Item]], formula_wrap: bool) -> list[str]:
    """非表格行 → 段落 / 列表项 / 公式行。"""
    blocks: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append("".join(para))
            para.clear()

    for line in lines:
        text = " ".join(it.text for it in line).strip()
        if not text:
            continue
        if _looks_like_formula(text):
            flush()
            blocks.append(f"${text}$" if formula_wrap else text)
            continue
        if _BULLET_RE.match(text):
            flush()
            blocks.append(text)
            continue
        para.append(text)
        if text[-1] in _SENT_END:
            flush()
    flush()
    return blocks


def boxes_to_markdown(boxes: list[tuple], formula_wrap: bool = False) -> str:
    """OCR 原始结果 → Markdown（表格保留为表格，公式行不被拆散）。"""
    items = _to_items(boxes)
    if not items:
        return ""
    lines = _group_lines(items)
    if not lines:
        return ""

    ranges = _table_ranges(lines)
    tbl_map = {s: (s, e) for s, e in ranges}

    out: list[str] = []
    i = 0
    plain: list[list[_Item]] = []
    while i < len(lines):
        if i in tbl_map:
            if plain:
                out.extend(_lines_to_md(plain, formula_wrap))
                plain = []
            s, e = tbl_map[i]
            table = _render_table(lines[s:e])
            if table:
                out.append(table)
                i = e
                continue
        plain.append(lines[i])
        i += 1
    if plain:
        out.extend(_lines_to_md(plain, formula_wrap))

    md = "\n\n".join(b for b in out if b).strip()
    if len(md) > MAX_OCR_CHARS:
        md = md[:MAX_OCR_CHARS].rstrip() + "\n\n（图片文字过长，已截断）"
    return md


def ocr_image_markdown(path: Path, formula_wrap: bool = False) -> str:
    """对图片做 OCR 并还原成 Markdown。引擎不可用时返回空串。"""
    from .cleaning import ocr_boxes

    try:
        boxes = ocr_boxes(Path(path))
    except Exception as e:  # 单张图失败不能中断整批同步
        log.warning("OCR 失败 %s: %s", path, e)
        return ""
    return boxes_to_markdown(boxes, formula_wrap)
