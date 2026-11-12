"""把可研 PDF 转成 Markdown，供 Dify「项目查重」工作流的章节分块节点使用。

输出约定与 Dify 提取 docx 的结果对齐，章节分块节点不用区分来源：
- 标题取自 PDF 书签，独占一行，格式为 "2.1.1 标题"（不加 #，分块节点按行首编号识别标题）。
- 表格输出为 markdown 表格；跨页表格拼回一张，合并单元格按上一行补值。
- 正文去掉页码、目录，把排版造成的断行接回成段落。
"""

from __future__ import annotations

import io
import re
import statistics
from dataclasses import dataclass, field

import pdfplumber
from pypdf import PdfReader

CJK = r"　-〿一-鿿＀-￯"
_CJK_GAP = re.compile(rf"(?<=[{CJK}])\s+(?=[{CJK}])")
_TOC_LINE = re.compile(r"(?:\.{4,}|…{2,}|·{4,})\s*[\dIVXivx]+\s*$")
_PAGE_NUMBER = re.compile(r"^[-—\s]*(?:\d{1,4}|[IVXLivxl]{1,6})[-—\s]*$")
# 与章节分块节点识别标题的正则一致。有书签时，正文里匹配它的行（如编号列表"1.设计工作"）不是标题。
_NODE_HEADING = re.compile(r"^[ \t]*\d+(?:\.\d+){0,8}[ \t]*[^\n|]{1,120}$")
# 书签少于这个数时不可靠，标题交给分块节点自己识别。
_MIN_BOOKMARKS = 5

# 页面上下边缘的短行视为页眉页脚。
_EDGE_RATIO = 0.07
# 单独一行的页码可能离边缘稍远（有的版式在 92% 高度处），放宽到 12%。
_PAGE_NUMBER_RATIO = 0.12
_EDGE_MAX_CHARS = 30
# 段落判断：行尾离右边界超过这个距离视为段落结束，行首缩进超过这个距离视为新段落。
_LINE_END_GAP = 15
_SHORT_LINE_GAP = 60
_INDENT = 12
_PARAGRAPH_END = re.compile(r"[。；;：:！!？?]$")
_LIST_START = re.compile(r"^(?:\d{1,2}[.．、)）](?!\d)|[（(]\d{1,2}[）)])")
_LABEL_START = re.compile(r"^[一-鿿]{2,8}[：:]")


@dataclass
class Line:
    text: str
    x0: float
    x1: float
    top: float


@dataclass
class Heading:
    text: str
    top: float


@dataclass
class Table:
    rows: list[list[str | None]]
    top: float
    continues_page: bool = False
    # 最后一行是否像是被分页截断（最长那格的末行排满单元格且没有句末标点）。
    tail_open: bool = False
    # 各列的横向范围 (x0, x1)，用于跨页时按位置对齐列。
    columns: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Page:
    items: list = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _clean_heading(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    match = re.match(r"^(\d+(?:\s*[.．]\s*\d+)*)\s*[.．、]?\s*(.+)$", title)
    if match:
        number = re.sub(r"\s+", "", match.group(1)).replace("．", ".")
        return f"{number} {_CJK_GAP.sub('', match.group(2))}"
    return _CJK_GAP.sub("", title)


def _read_bookmarks(data: bytes) -> dict[int, list[tuple[float, str]]]:
    """返回 {页码: [(距页顶的位置, 标题), ...]}。没有书签时返回空字典。"""
    reader = PdfReader(io.BytesIO(data))
    items: list[tuple[int, float, str, int]] = []

    def walk(outline, level=0):
        for item in outline:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            try:
                page_index = reader.get_destination_page_number(item)
            except Exception:
                continue
            if page_index is None or page_index < 0:
                continue
            height = float(reader.pages[page_index].mediabox.height)
            top = getattr(item, "top", None)
            y = height - float(top) if top is not None else 0.0
            items.append((page_index, y, _clean_heading(str(item.title)), level))

    try:
        walk(reader.outline)
    except Exception:
        return {}

    result: dict[int, list[tuple[float, str]]] = {}
    for page_index, y, title in _consistent_headings(sorted(items, key=lambda item: (item[0], item[1]))):
        result.setdefault(page_index, []).append((y, title))
    return result


def _consistent_headings(items: list[tuple[int, float, str, int]]) -> list[tuple[int, float, str]]:
    """去掉编号和章节层级接不上的书签。

    有的作者给正文编号列表（如「2.投资研判…」「1、省侧…」）也打了书签，或者书签编号抄自别的文档
    （第 2 章里出现「5.2.3.1.1」）。把它们当标题会顶替真正的章标题，打乱后面所有章节路径。
    规则：
    - 单段编号只在书签树顶层（章标题所在层）才算章标题，且章号递增；正文列表项的书签都在深层。
    - 多段编号的第一段必须是当前所在章号。
    - 没有编号的书签保留。
    """
    chapter_levels = [level for _, _, title, level in items if re.match(r"^\d+ ", title)]
    top_level = min(chapter_levels) if chapter_levels else 0
    kept = []
    chapter: int | None = None
    for page_index, y, title, level in items:
        match = re.match(r"^(\d+(?:\.\d+)*) ", title)
        if not match:
            kept.append((page_index, y, title))
            continue
        parts = match.group(1).split(".")
        if len(parts) == 1:
            number = int(parts[0])
            if level != top_level or (chapter is not None and number <= chapter):
                continue
            chapter = number
        elif chapter is not None and int(parts[0]) != chapter:
            continue
        kept.append((page_index, y, title))
    return kept


def _clean_cell(value: str | None) -> str | None:
    if value is None:
        return None
    value = _CJK_GAP.sub("", value)
    value = re.sub(r"\s*\n\s*", " ", value).strip()
    return value.replace("|", "｜")


def _inside(obj, boxes) -> bool:
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes)


def _read_page(page, bookmarks: list[tuple[float, str]]) -> Page:
    tables = page.find_tables()
    boxes = [table.bbox for table in tables]
    text_page = page.filter(lambda obj: obj.get("object_type") != "char" or not _inside(obj, boxes))

    lines = []
    for raw in text_page.extract_text_lines(strip=True, return_chars=False):
        text = raw["text"].strip()
        if not text:
            continue
        at_edge = raw["top"] < page.height * _EDGE_RATIO or raw["bottom"] > page.height * (1 - _EDGE_RATIO)
        near_edge = raw["top"] < page.height * _PAGE_NUMBER_RATIO or raw["bottom"] > page.height * (1 - _PAGE_NUMBER_RATIO)
        if (at_edge and len(text) <= _EDGE_MAX_CHARS) or (near_edge and _PAGE_NUMBER.match(text)):
            continue
        if _TOC_LINE.search(text) or _norm(text) in ("目录", "目次"):
            continue
        # 单元格文字溢出到表格边框外，会留下孤立的标点（如「、」），它会把一张表隔成两段。
        if re.fullmatch(r"[^\w一-鿿]{1,3}", text):
            continue
        lines.append(Line(text, raw["x0"], raw["x1"], raw["top"]))

    items: list = list(lines)
    items += [_build_table(page, group) for group in _side_by_side_groups(tables)]

    # 书签标题替换正文里对应的标题行（标题可能折成两行）。
    for y, title in bookmarks:
        target = _norm(title)
        candidates = sorted(
            (line for line in lines if line.top >= y - 8 and line.top <= y + 60),
            key=lambda line: line.top,
        )
        consumed, text = [], ""
        for line in candidates:
            if line not in items:
                continue
            joined = text + _norm(line.text)
            if not target.startswith(joined):
                break
            consumed.append(line)
            text = joined
            if text == target:
                break
        if consumed and text == target:
            for line in consumed:
                items.remove(line)
            items.append(Heading(title, consumed[0].top - 0.1))
        else:
            items.append(Heading(title, y - 0.1))

    items.sort(key=lambda item: item.top)
    return Page(items)


def _side_by_side_groups(tables) -> list[list]:
    """把同一高度上左右并排的表格归为一组。

    某一列整页都是合并单元格、没有竖线时，pdfplumber 会把一张表拆成左右两张。
    """
    groups: list[list] = []
    for table in sorted(tables, key=lambda t: (t.bbox[1], t.bbox[0])):
        x0, top, x1, bottom = table.bbox
        for group in groups:
            g_top = min(t.bbox[1] for t in group)
            g_bottom = max(t.bbox[3] for t in group)
            overlap = min(bottom, g_bottom) - max(top, g_top)
            shorter = min(bottom - top, g_bottom - g_top)
            beside = all(x0 >= t.bbox[2] - 2 or x1 <= t.bbox[0] + 2 for t in group)
            if shorter > 0 and overlap > 0.8 * shorter and beside:
                group.append(table)
                break
        else:
            groups.append([table])
    return groups


def _build_table(page, group) -> Table:
    if len(group) == 1:
        table = group[0]
        return Table(
            [[_clean_cell(c) for c in row] for row in table.extract()],
            table.bbox[1],
            tail_open=_tail_open(page, table),
            columns=[(column.bbox[0], column.bbox[2]) for column in table.columns],
        )

    # 并排的几张表按行的纵坐标对齐成一张；某张表在这一行没有格子，说明是合并单元格，留空待补值。
    group = sorted(group, key=lambda t: t.bbox[0])
    tops: list[float] = []
    for table in group:
        for row in table.rows:
            if not any(abs(row.bbox[1] - top) < 3 for top in tops):
                tops.append(row.bbox[1])
    tops.sort()
    width = sum(len(table.columns) for table in group)
    grid: list[list[str | None]] = [[None] * width for _ in tops]
    offset = 0
    for table in group:
        for row, cells in zip(table.rows, table.extract()):
            index = min(range(len(tops)), key=lambda i: abs(tops[i] - row.bbox[1]))
            for col, cell in enumerate(cells):
                grid[index][offset + col] = _clean_cell(cell)
        offset += len(table.columns)
    return Table(
        grid,
        min(t.bbox[1] for t in group),
        tail_open=any(_tail_open(page, t) for t in group),
        columns=[(c.bbox[0], c.bbox[2]) for t in group for c in t.columns],
    )


def _merge_tables(pages: list[Page]) -> None:
    """标出可能的续表：续页的第一项是表格、上一页最后一项也是表格。能否拼上由列对齐决定（见 _column_map）。"""
    previous_last = None
    for page in pages:
        if page.items and previous_last is not None:
            first = page.items[0]
            if isinstance(first, Table) and isinstance(previous_last, Table) and first.rows:
                first.continues_page = True
        previous_last = page.items[-1] if page.items else previous_last


def _column_map(base: list[tuple[float, float]], columns: list[tuple[float, float]], width: int, count: int):
    """续表的每一列对应到原表的第几列。对不上时返回 None（不是同一张表）。

    续页上合并单元格所在的列常常没有竖线，pdfplumber 会少识别一列；按列中心的横坐标对齐，
    缺的列留空，之后按合并单元格向下补值。
    """
    mapping = None
    if base and columns and len(columns) == count:
        mapping = []
        for x0, x1 in columns:
            center = (x0 + x1) / 2
            hits = [i for i, (b0, b1) in enumerate(base) if b0 - 5 <= center <= b1 + 5]
            if len(hits) != 1:
                mapping = None
                break
            mapping.append(hits[0])
        # 允许多列对应同一列（续页某列中间多出一条竖线被拆成两列），但顺序不能乱。
        if mapping is not None and mapping != sorted(mapping):
            mapping = None
    if mapping is None and count == width:
        # 列数相同就按原顺序对应（与按位置对齐之前的行为一致）。
        return list(range(width))
    return mapping


def _align_rows(rows: list[list[str | None]], mapping: list[int], width: int) -> list[list[str | None]]:
    aligned = []
    for row in rows:
        new_row: list[str | None] = [None] * width
        for col, cell in enumerate(row):
            if col < len(mapping):
                target = mapping[col]
                if new_row[target] and cell:
                    new_row[target] = _join(new_row[target], cell)
                elif cell is not None or new_row[target] is None:
                    new_row[target] = cell if new_row[target] is None else new_row[target]
        aligned.append(new_row)
    return aligned


def _fill_merged_cells(rows: list[list[str | None]], previous_row: list[str] | None) -> list[list[str]]:
    filled: list[list[str]] = []
    above = previous_row
    for index, row in enumerate(rows):
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        new_row = []
        for col, cell in enumerate(row):
            if cell is None:
                cell = above[col] if above and col < len(above) else ""
            elif cell == "" and index == 0 and previous_row and next_row and next_row[col] is None:
                # 续页第一行：合并单元格从上一页延续过来，本页这一格是空的。
                cell = previous_row[col]
            new_row.append(cell)
        filled.append(new_row)
        # 新表的第一行是表头，下面的合并单元格不能用表头文字补值。
        above = None if index == 0 and previous_row is None else new_row
    return filled


_SENTENCE_END = re.compile(r"[。；;！!？?）)」”]$")


def _tail_open(page, table) -> bool:
    """表格最后一行是否像被分页截断：最长那格的最后一行文字排满了单元格，且没有以句末标点结束。"""
    if not table.rows:
        return False
    best_text, best_box = "", None
    for box in table.rows[-1].cells:
        if box is None:
            continue
        text = page.crop(box, strict=False).extract_text() or ""
        if len(_norm(text)) > len(_norm(best_text)):
            best_text, best_box = text, box
    if best_box is None or _SENTENCE_END.search(_norm(best_text)):
        return False
    chars = [c for c in page.crop(best_box, strict=False).chars if c["text"].strip()]
    if not chars:
        return False
    last_top = max(c["top"] for c in chars)
    last_line = [c for c in chars if abs(c["top"] - last_top) < 2]
    char_width = statistics.median(c["x1"] - c["x0"] for c in chars)
    # 单元格左右各有内边距，末行右端离单元格右边界不到两个字宽就算排满。
    return max(c["x1"] for c in last_line) >= best_box[2] - char_width * 2


def _is_split_row(previous: Table, rows: list[list[str | None]]) -> bool:
    """续页第一行是不是上一页最后一行被分页截断的后半截。

    续页第一行的首列有内容，说明它是新的一行。首列为空时，满足任一条件即视为截断：
    - 上一页的表格最后一行看起来没写完（见 _tail_open）；
    - 续页第一行在非合并列上是空的（下一行这一列有自己的内容，说明不是合并单元格，
      这一格空着是因为内容都留在了上一页）。
    """
    first = rows[0]
    if first[0] not in ("", None):
        return False
    if previous.tail_open:
        return True
    following = rows[1] if len(rows) > 1 else None
    if following is None:
        return False
    return any(cell == "" and col < len(following) and following[col] for col, cell in enumerate(first))


def _table_to_markdown(rows: list[list[str]]) -> str:
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def _join(left: str, right: str) -> str:
    if not left:
        return right
    if re.search(r"[A-Za-z0-9,.;:)]$", left) and re.match(r"^[A-Za-z0-9(]", right):
        return f"{left} {right}"
    return left + right


def convert(data: bytes) -> str:
    bookmarks = _read_bookmarks(data)
    pages: list[Page] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for index, page in enumerate(pdf.pages):
            pages.append(_read_page(page, bookmarks.get(index, [])))

    all_lines = [item for page in pages for item in page.items if isinstance(item, Line)]
    left = statistics.median(line.x0 for line in all_lines) if all_lines else 0
    right = statistics.quantiles([line.x1 for line in all_lines], n=20)[-1] if len(all_lines) > 20 else 0

    _merge_tables(pages)

    trust_bookmarks = sum(len(items) for items in bookmarks.values()) >= _MIN_BOOKMARKS
    heading_texts = {title for items in bookmarks.values() for _, title in items}
    blocks: list[str] = []
    paragraph = ""
    table_rows: list[list[str]] | None = None

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            # 书签标题插入后，正文里没对上的那行原标题会紧跟着重复一遍，丢掉。
            if blocks and blocks[-1] in heading_texts and _norm(paragraph) == _norm(blocks[-1]):
                paragraph = ""
                return
            if trust_bookmarks and _NODE_HEADING.match(paragraph):
                paragraph = "- " + paragraph
            blocks.append(paragraph)
            paragraph = ""

    def flush_table():
        nonlocal table_rows
        if table_rows:
            blocks.append(_table_to_markdown(table_rows))
        table_rows = None

    previous_line: Line | None = None
    previous_table: Table | None = None
    table_columns: list[tuple[float, float]] = []
    for page in pages:
        for item in page.items:
            if isinstance(item, Table):
                flush_paragraph()
                previous_line = None
                rows = [row for row in item.rows if any(cell for cell in row)]
                if not rows:
                    continue
                mapping = None
                if item.continues_page and table_rows:
                    mapping = _column_map(table_columns, item.columns, len(table_rows[0]), len(rows[0]))
                if mapping is not None:
                    rows = _align_rows(rows, mapping, len(table_rows[0]))
                    # 续页重复的表头：对齐后缺的列是空，只比较有内容的格。
                    if all(c is None or _norm(c) == _norm(h) for c, h in zip(rows[0], table_rows[0])) and any(rows[0]):
                        rows = rows[1:]
                    if rows and _is_split_row(previous_table, rows):
                        # 一行被分页截断：把续页这一截接到上一页最后一行。
                        last = table_rows[-1]
                        for col, cell in enumerate(rows[0]):
                            if cell and col < len(last):
                                last[col] = _join(last[col], cell)
                        rows = rows[1:]
                    table_rows += _fill_merged_cells(rows, table_rows[-1]) if rows else []
                else:
                    flush_table()
                    table_rows = _fill_merged_cells(rows, None)
                    table_columns = item.columns
                previous_table = item
                continue

            flush_table()
            if isinstance(item, Heading):
                flush_paragraph()
                blocks.append(item.text)
                previous_line = None
                continue

            gap = right - previous_line.x1 if previous_line else 0
            starts_new = (
                previous_line is None
                or item.x0 > left + _INDENT
                # 上一行明显没写满：段落结束。
                or gap > _SHORT_LINE_GAP
                # 只差一两个字没到右边界，可能是两端对齐时行尾标点被压缩，要看是否以句末标点结束。
                or (gap > _LINE_END_GAP and _PARAGRAPH_END.search(previous_line.text))
                # 编号开头（3.、（1）、2、）的行是新的一项。
                or _LIST_START.match(item.text)
                # 「项目名称：」「项目申报单位：」这类标签行（封面、表单）各自成段。
                or _LABEL_START.match(item.text)
            )
            if paragraph.startswith("项目名称") and "：" not in item.text and ":" not in item.text:
                starts_new = False
            if starts_new:
                flush_paragraph()
            paragraph = _join(paragraph, item.text)
            previous_line = item

    flush_paragraph()
    flush_table()
    return "\n\n".join(blocks) + "\n"


if __name__ == "__main__":
    import sys

    with open(sys.argv[1], "rb") as source:
        markdown = convert(source.read())
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8") as target:
            target.write(markdown)
    else:
        sys.stdout.write(markdown)
