"""docx 正文提取，输出格式与 Dify 文档提取器（python-docx 1.2）一致。

只用标准库。段落取 w:r 和 w:hyperlink 下的文本，表格转成 Markdown，
合并单元格按 python-docx 的规则展开。正文中的 w:sdt（常见于目录）不输出。
"""
from __future__ import annotations

import posixpath
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOCUMENT_TYPE = "/officeDocument"


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


class TableStructureError(ValueError):
    pass


def _read_zip_entries(path: Path) -> Dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename.replace("\\", "/"): archive.read(info) for info in archive.infolist()}


def _main_document_name(entries: Dict[str, bytes]) -> str:
    rels = entries.get("_rels/.rels")
    if rels:
        root = ET.fromstring(rels)
        for rel in root.findall(f"{{{REL_NS}}}Relationship"):
            if str(rel.get("Type", "")).endswith(OFFICE_DOCUMENT_TYPE):
                target = str(rel.get("Target", "")).lstrip("/")
                if target in entries:
                    return target
                normalized = posixpath.normpath(target)
                if normalized in entries:
                    return normalized
    if "word/document.xml" in entries:
        return "word/document.xml"
    raise ValueError("docx 中找不到正文 word/document.xml")


def _run_text(run: ET.Element) -> str:
    parts: List[str] = []
    for child in run:
        if child.tag == w("t"):
            parts.append(child.text or "")
        elif child.tag in (w("tab"), w("ptab")):
            parts.append("\t")
        elif child.tag == w("br"):
            parts.append("\n" if child.get(w("type"), "textWrapping") == "textWrapping" else "")
        elif child.tag == w("cr"):
            parts.append("\n")
        elif child.tag == w("noBreakHyphen"):
            parts.append("-")
    return "".join(parts)


def paragraph_text(paragraph: ET.Element) -> str:
    parts: List[str] = []
    for child in paragraph:
        if child.tag == w("r"):
            parts.append(_run_text(child))
        elif child.tag == w("hyperlink"):
            parts.extend(_run_text(run) for run in child if run.tag == w("r"))
    return "".join(parts)


def _int_val(element: Optional[ET.Element], default: int) -> int:
    if element is None:
        return default
    try:
        return int(element.get(w("val"), default))
    except (TypeError, ValueError):
        return default


def _grid_before(tr: ET.Element) -> int:
    tr_pr = tr.find(w("trPr"))
    return _int_val(tr_pr.find(w("gridBefore")) if tr_pr is not None else None, 0)


def _grid_span(tc: ET.Element) -> int:
    tc_pr = tc.find(w("tcPr"))
    return _int_val(tc_pr.find(w("gridSpan")) if tc_pr is not None else None, 1)


def _v_merge(tc: ET.Element) -> Optional[str]:
    tc_pr = tc.find(w("tcPr"))
    if tc_pr is None:
        return None
    v_merge = tc_pr.find(w("vMerge"))
    if v_merge is None:
        return None
    return v_merge.get(w("val"), "continue")


def _cell_text(tc: ET.Element) -> str:
    return "\n".join(paragraph_text(p) for p in tc if p.tag == w("p"))


class _Table:
    def __init__(self, tbl: ET.Element) -> None:
        self.rows = [tr for tr in tbl if tr.tag == w("tr")]

    @staticmethod
    def _tcs(tr: ET.Element) -> List[ET.Element]:
        return [tc for tc in tr if tc.tag == w("tc")]

    def _tc_at_grid_offset(self, row_index: int, grid_offset: int) -> ET.Element:
        tr = self.rows[row_index]
        remaining = grid_offset - _grid_before(tr)
        for tc in self._tcs(tr):
            if remaining < 0:
                break
            if remaining == 0:
                return tc
            remaining -= _grid_span(tc)
        raise TableStructureError(f"no `tc` element at grid_offset={grid_offset}")

    def _root_tc(self, row_index: int, tc: ET.Element) -> ET.Element:
        while _v_merge(tc) == "continue":
            if row_index == 0:
                raise TableStructureError("no tr above topmost tr in w:tbl")
            tr = self.rows[row_index]
            offset = _grid_before(tr)
            for sibling in self._tcs(tr):
                if sibling is tc:
                    break
                offset += _grid_span(sibling)
            row_index -= 1
            tc = self._tc_at_grid_offset(row_index, offset)
        return tc

    def row_cells(self, row_index: int) -> List[str]:
        cells: List[str] = []
        for tc in self._tcs(self.rows[row_index]):
            root = self._root_tc(row_index, tc)
            cells.extend([_cell_text(root)] * _grid_span(root))
        return cells


def table_markdown(tbl: ET.Element) -> Optional[str]:
    table = _Table(tbl)
    try:
        rows = [table.row_cells(index) for index in range(len(table.rows))]
    except TableStructureError:
        return None
    if not any(any(cell.strip() for cell in row) for row in rows):
        return None
    header = [cell.replace("\n", "<br>") for cell in rows[0]]
    markdown = f"| {' | '.join(header)} |\n"
    markdown += f"| {' | '.join(['---'] * len(rows[0]))} |\n"
    for row in rows[1:]:
        markdown += "| " + " | ".join(cell.replace("\n", "<br>") for cell in row) + " |\n"
    return markdown


def extract_docx_text(path: Path) -> str:
    entries = _read_zip_entries(path)
    root = ET.fromstring(entries[_main_document_name(entries)])
    body = root.find(w("body"))
    if body is None:
        return ""
    texts: List[str] = []
    for block in body:
        if block.tag == w("p"):
            texts.append(paragraph_text(block))
        elif block.tag == w("tbl"):
            markdown = table_markdown(block)
            if markdown is not None:
                texts.append(markdown)
    return "\n".join(texts)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法：python3 docx_text.py <文件.docx>", file=sys.stderr)
        sys.exit(2)
    sys.stdout.write(extract_docx_text(Path(sys.argv[1])))
