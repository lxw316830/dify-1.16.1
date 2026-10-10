"""Build delimiter-based knowledge-base chunks for project deduplication.

The output is intended for rebuilding the historical Dify knowledge base with
the same section granularity used by the project-dedup workflow:

- requirement chunks from project-demand chapters, project goals, and expected
  benefits/results;
- project-content chunks from project-content/construction-content chapters;
- no generated parent summaries;
- no parent section swallowing child sections.

Each exported chunk starts with a stable delimiter line so Dify can split the
uploaded TXT files by a custom separator.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_SEPARATOR = "###@@DIFY_KB_CHUNK_BOUNDARY@@###"
DEFAULT_MAX_CHARS = 3000
SUPPORTED_INPUT_EXTENSIONS = {".docx", ".txt"}


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_name: str
    project_name: str
    content_type: str
    section_path: str
    part_index: int
    part_total: int
    content: str


@dataclass(frozen=True)
class DocxStyleInfo:
    name: str
    num_id: str
    ilvl: int | None
    outline_level: int | None


@dataclass(frozen=True)
class DocxNumberingLevel:
    num_format: str
    level_text: str
    start: int


class DocxNumbering:
    def __init__(
        self,
        num_to_abstract: dict[str, str],
        levels: dict[tuple[str, int], DocxNumberingLevel],
    ) -> None:
        self.num_to_abstract = num_to_abstract
        self.levels = levels
        self.counters: dict[str, dict[int, int]] = {}

    @classmethod
    def from_archive(cls, archive: zipfile.ZipFile) -> DocxNumbering:
        try:
            numbering_xml = archive.read("word/numbering.xml")
        except KeyError:
            return cls({}, {})

        root = ET.fromstring(numbering_xml)
        num_to_abstract: dict[str, str] = {}
        levels: dict[tuple[str, int], DocxNumberingLevel] = {}

        for num in root.findall(word_tag("num")):
            num_id = num.get(word_tag("numId"), "")
            abstract = num.find(word_tag("abstractNumId"))
            abstract_id = abstract.get(word_tag("val"), "") if abstract is not None else ""
            if num_id and abstract_id:
                num_to_abstract[num_id] = abstract_id

        for abstract in root.findall(word_tag("abstractNum")):
            abstract_id = abstract.get(word_tag("abstractNumId"), "")
            if not abstract_id:
                continue
            for level in abstract.findall(word_tag("lvl")):
                ilvl = int_or_none(level.get(word_tag("ilvl"), ""))
                if ilvl is None:
                    continue
                num_format = child_val(level, "numFmt")
                level_text = child_val(level, "lvlText")
                start = int_or_none(child_val(level, "start")) or 1
                levels[(abstract_id, ilvl)] = DocxNumberingLevel(num_format, level_text, start)

        return cls(num_to_abstract, levels)

    def next_label(self, num_id: str, ilvl: int) -> str:
        abstract_id = self.num_to_abstract.get(num_id, "")
        level = self.levels.get((abstract_id, ilvl))
        if level is None or not is_decimal_outline_level(level):
            return ""

        counters = self.counters.setdefault(num_id, {})
        if any(parent_level not in counters for parent_level in range(ilvl)):
            return ""

        counters[ilvl] = counters.get(ilvl, level.start - 1) + 1
        for stale_level in [key for key in counters if key > ilvl]:
            del counters[stale_level]

        label = level.level_text
        for level_index in range(9):
            label = label.replace(f"%{level_index + 1}", str(counters.get(level_index, 0)))
        if re.search(r"%\d", label):
            return ""
        return normalize_heading_number(label)


def build_chunks_for_source(path: Path, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    text = read_source_text(path)
    project_name = extract_project_name(text) or infer_project_name(path)
    chunks: list[Chunk] = []

    sections = parse_sections(text)
    for section in sections:
        content_type = classify_section(section["number"], section["title"], section["section_path"])
        if not content_type:
            continue

        body = clean_body(section["body"])
        if not body:
            continue

        parts = split_long_text(body, max_chars=max_chars)
        for part_index, part in enumerate(parts, start=1):
            if not part:
                continue
            chunk_id = f"{path.stem}_{len(chunks) + 1:04d}"
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    document_name=path.name,
                    project_name=project_name,
                    content_type=content_type,
                    section_path=section["section_path"],
                    part_index=part_index,
                    part_total=len(parts),
                    content=part,
                )
            )

    return chunks


def read_source_text(path: Path) -> str:
    extension = path.suffix.lower()
    if extension == ".docx":
        return restore_auto_numbered_headings(extract_docx_text(path))
    if extension == ".txt":
        return restore_auto_numbered_headings(path.read_text(encoding="utf-8-sig"))
    raise ValueError(f"Unsupported input file type: {path}")


def extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read("word/document.xml")
        styles = read_docx_style_info(archive)
        numbering = DocxNumbering.from_archive(archive)

    root = ET.fromstring(document_xml)
    body = root.find(word_tag("body"))
    if body is None:
        return ""

    lines: list[str] = []
    for child in body:
        if child.tag == word_tag("p"):
            text = clean_inline_text(xml_text(child))
            if text:
                lines.append(format_docx_paragraph_text(child, text, styles, numbering))
        elif child.tag == word_tag("tbl"):
            lines.extend(table_lines(child))

    return "\n".join(lines)


def read_docx_style_info(archive: zipfile.ZipFile) -> dict[str, DocxStyleInfo]:
    try:
        styles_xml = archive.read("word/styles.xml")
    except KeyError:
        return {}

    root = ET.fromstring(styles_xml)
    styles: dict[str, DocxStyleInfo] = {}
    for style in root.findall(word_tag("style")):
        style_id = style.get(word_tag("styleId"), "")
        if not style_id:
            continue

        ppr = style.find(word_tag("pPr"))
        num_id, ilvl = num_props_from_ppr(ppr)
        outline = ppr.find(word_tag("outlineLvl")) if ppr is not None else None
        styles[style_id] = DocxStyleInfo(
            name=child_val(style, "name"),
            num_id=num_id,
            ilvl=ilvl,
            outline_level=int_or_none(outline.get(word_tag("val"), "")) if outline is not None else None,
        )
    return styles


def format_docx_paragraph_text(
    paragraph: ET.Element,
    text: str,
    styles: dict[str, DocxStyleInfo],
    numbering: DocxNumbering,
) -> str:
    parsed = parse_numbered_heading_line(text)
    if parsed and is_likely_heading(parsed[0], parsed[1]):
        return f"{parsed[0]} {parsed[1]}"

    style_id = paragraph_style_id(paragraph)
    num_id, ilvl = paragraph_num_props(paragraph, style_id, styles)

    if num_id and ilvl is not None and (is_heading_style(style_id, styles) or looks_like_auto_numbered_heading_text(text)):
        label = numbering.next_label(num_id, ilvl)
        if label:
            return f"{label} {clean_title(text)}"

    return text


def paragraph_style_id(paragraph: ET.Element) -> str:
    style = paragraph.find(f"{word_tag('pPr')}/{word_tag('pStyle')}")
    return style.get(word_tag("val"), "") if style is not None else ""


def paragraph_num_props(
    paragraph: ET.Element,
    style_id: str,
    styles: dict[str, DocxStyleInfo],
) -> tuple[str, int | None]:
    ppr = paragraph.find(word_tag("pPr"))
    num_id, ilvl = num_props_from_ppr(ppr)
    style = styles.get(style_id)
    if style is None:
        return num_id, ilvl

    if not num_id:
        num_id = style.num_id
    if ilvl is None:
        ilvl = style.ilvl
    return num_id, ilvl


def num_props_from_ppr(ppr: ET.Element | None) -> tuple[str, int | None]:
    if ppr is None:
        return "", None

    num_pr = ppr.find(word_tag("numPr"))
    if num_pr is None:
        return "", None

    num_id = child_val(num_pr, "numId")
    ilvl = int_or_none(child_val(num_pr, "ilvl"))
    return num_id, ilvl


def is_heading_style(style_id: str, styles: dict[str, DocxStyleInfo]) -> bool:
    style = styles.get(style_id)
    if style is None:
        return False

    name = style.name.lower()
    return name.startswith("heading ") or name.startswith("标题") or (
        style.outline_level is not None and style.outline_level <= 8
    )


def looks_like_auto_numbered_heading_text(text: str) -> bool:
    title = clean_title(text)
    return bool(
        title
        and len(title) <= 120
        and re.search(r"[\u4e00-\u9fa5A-Za-z]", title)
        and not re.search(r"[。；;]", title)
    )


def is_decimal_outline_level(level: DocxNumberingLevel) -> bool:
    level_text = level.level_text.strip()
    return level.num_format == "decimal" and bool(re.fullmatch(r"(?:%\d[.．]?)+\.?", level_text))


def child_val(element: ET.Element, child_name: str) -> str:
    child = element.find(word_tag(child_name))
    return child.get(word_tag("val"), "") if child is not None else ""


def int_or_none(value: str | None) -> int | None:
    if value is None or not str(value).isdigit():
        return None
    return int(value)


def table_lines(table: ET.Element) -> list[str]:
    lines: list[str] = []
    for row in table.findall(word_tag("tr")):
        cells: list[str] = []
        for cell in row.findall(word_tag("tc")):
            paragraphs = [
                clean_inline_text(xml_text(paragraph))
                for paragraph in cell.findall(word_tag("p"))
            ]
            cells.append(clean_inline_text(" ".join(text for text in paragraphs if text)))
        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
    return lines


def xml_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == word_tag("t"):
            parts.append(node.text or "")
        elif node.tag in {word_tag("tab"), word_tag("br"), word_tag("cr")}:
            parts.append(" ")
    return "".join(parts)


def word_tag(name: str) -> str:
    return f"{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}{name}"


def parse_numbered_heading_line(line: str) -> tuple[str, str] | None:
    stripped = str(line or "").strip()
    match = re.match(
        r"^(\d{1,2}(?:\s*[.．]\s*\d{1,3}){0,8})\s*(?:[.．]\s*)?(.+?)\s*$",
        stripped,
    )
    if not match:
        return None

    number = normalize_heading_number(match.group(1))
    title = clean_title(match.group(2))
    if not number or not title or title.isdigit():
        return None

    return number, title


def restore_auto_numbered_headings(text: str) -> str:
    toc_headings = build_toc_heading_map(text)
    if not toc_headings:
        return text

    restored: list[str] = []
    used_counts: dict[str, int] = {}

    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            restored.append(line)
            continue

        parsed = parse_numbered_heading_line(stripped)
        if parsed and is_likely_heading(parsed[0], parsed[1]):
            restored.append(f"{parsed[0]} {parsed[1]}")
            continue

        key = title_key(stripped)
        candidates = toc_headings.get(key, [])
        if not candidates or len(stripped) > 120:
            restored.append(line)
            continue

        used_index = min(used_counts.get(key, 0), len(candidates) - 1)
        number, title = candidates[used_index]
        used_counts[key] = used_counts.get(key, 0) + 1
        restored.append(f"{number} {title}")

    return "\n".join(restored)


def build_toc_heading_map(text: str) -> dict[str, list[tuple[str, str]]]:
    headings: dict[str, list[tuple[str, str]]] = {}
    for line in str(text or "").splitlines():
        parsed = parse_numbered_heading_line(line)
        if not parsed or not looks_like_toc_entry(line):
            continue

        number, title = parsed
        if not is_likely_heading(number, title):
            continue

        key = title_key(title)
        values = headings.setdefault(key, [])
        if (number, title) not in values:
            values.append((number, title))
    return headings


def looks_like_toc_entry(line: str) -> bool:
    stripped = str(line or "").strip()
    return bool(re.search(r"(?:[\t ]+\d{1,4}|(?<=[\u4e00-\u9fa5）)])\d{1,4})\s*$", stripped))


def parse_sections(text: str) -> list[dict[str, str]]:
    heading_pattern = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+){0,8})[ \t]*([^\n|]{1,120})$")
    matches = [
        match
        for match in heading_pattern.finditer(str(text or ""))
        if is_likely_heading(match.group(1), clean_title(match.group(2)))
    ]

    heading_names: dict[str, str] = {}
    sections: list[dict[str, str]] = []

    for index, match in enumerate(matches):
        number = normalize_heading_number(match.group(1))
        title = clean_title(match.group(2))
        heading_names[number] = f"{number} {title}"

        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = str(text or "")[match.end():end].strip()

        path_parts: list[str] = []
        number_parts = number.split(".")
        for depth in range(1, len(number_parts) + 1):
            parent = ".".join(number_parts[:depth])
            if parent in heading_names:
                path_parts.append(heading_names[parent])

        sections.append(
            {
                "number": number,
                "title": title,
                "section_path": " > ".join(path_parts),
                "body": body,
            }
        )

    return sections


def classify_section(number: str, title: str, section_path: str) -> str:
    titles = section_titles(section_path)
    root_title = titles[0] if titles else title_text(title)
    current_title = titles[-1] if titles else title_text(title)

    skip_markers = (
        "集成需求",
        "非功能需求",
        "集成工作",
        "系统实施",
        "实施范围",
        "技术方案",
        "技术路线",
        "非功能要求",
        "数据需求",
        "数据需求及获取方式",
        "建设现状",
        "工作步骤",
        "关键技术",
        "工程范围",
    )
    exact_skip_titles = (
        "项目管理",
        "项目岗位要求",
        "项目进度",
        "项目会议",
        "项目培训需求",
        "项目分包要求",
        "培训内容",
    )

    if any(marker in item for item in titles for marker in skip_markers):
        return ""
    if any(item in exact_skip_titles for item in titles):
        return ""

    if any(marker in current_title for marker in ("项目目标", "预期成效", "预期成果")):
        return "requirement"

    if (
        any(marker in root_title for marker in ("项目需求分析", "需求分析"))
        or any(marker in current_title for marker in ("业务建设需求", "业务需求", "本期建设需求"))
    ):
        return "requirement"

    if any(marker in item for item in titles for marker in ("项目内容", "建设内容")):
        if len(number.split(".")) <= 8:
            return "project_content"

    if any(marker in root_title for marker in ("项目方案", "建设方案")):
        if any(marker in current_title for marker in ("工作内容", "建设任务")):
            if len(number.split(".")) <= 8:
                return "project_content"

    return ""


def split_long_text(text: str, max_chars: int) -> list[str]:
    body = clean_body(text)
    if not body:
        return []
    if len(body) <= max_chars:
        return [body]

    blocks = re.split(r"\n\s*\n+", body)
    results: list[str] = []
    current = ""

    def flush_current() -> None:
        nonlocal current
        if current.strip():
            results.append(current.strip())
        current = ""

    for block in blocks:
        block = "\n".join(line.strip() for line in block.splitlines() if line.strip())
        if not block:
            continue

        if len(block) > max_chars:
            if current:
                block = f"{current}\n{block}"
                current = ""
            results.extend(split_long_block(block, max_chars=max_chars))
            continue

        candidate = f"{current}\n{block}".strip() if current else block
        if len(candidate) <= max_chars:
            current = candidate
        else:
            flush_current()
            current = block

    flush_current()
    return results


def split_long_block(block: str, max_chars: int) -> list[str]:
    line_parts = split_long_lines(block, max_chars=max_chars)
    if line_parts:
        return line_parts

    sentences = re.split(r"(?<=[。；;])", block)
    results: list[str] = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(sentence) > max_chars:
            if current:
                results.append(current.strip())
                current = ""
            for start in range(0, len(sentence), max_chars):
                results.append(sentence[start:start + max_chars].strip())
            continue

        candidate = f"{current}{sentence}" if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                results.append(current.strip())
            current = sentence

    if current:
        results.append(current.strip())
    return results


def split_long_lines(block: str, max_chars: int) -> list[str]:
    lines = [line.strip() for line in str(block or "").splitlines() if line.strip()]
    if len(lines) <= 1:
        return []

    results: list[str] = []
    current = ""

    for line in lines:
        if len(line) > max_chars:
            if current:
                results.append(current.strip())
                current = ""
            results.extend(split_long_block(line, max_chars=max_chars))
            continue

        candidate = f"{current}\n{line}".strip() if current else line
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                results.append(current.strip())
            current = line

    if current:
        results.append(current.strip())
    return results


def clean_body(text: str) -> str:
    lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()]
    cleaned: list[str] = []
    for line in lines:
        if not line:
            cleaned.append("")
            continue
        if is_noise_line(line):
            continue
        cleaned.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def is_noise_line(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return False
    if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", stripped):
        return True
    return False


def format_chunk(chunk: Chunk, separator: str) -> str:
    return "\n".join(
        [
            separator,
            f"章节路径：{chunk.section_path}",
            "",
            chunk.content.strip(),
        ]
    ).strip()


def write_outputs(chunks_by_doc: dict[Path, list[Chunk]], output_dir: Path, separator: str) -> None:
    documents_dir = output_dir / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_chunks: list[Chunk] = []
    manifest_rows: list[dict[str, object]] = []

    for path, chunks in chunks_by_doc.items():
        all_chunks.extend(chunks)
        target = documents_dir / f"{path.stem}.txt"
        target.write_text("\n\n".join(format_chunk(chunk, separator) for chunk in chunks) + ("\n" if chunks else ""), encoding="utf-8")
        manifest_rows.append(
            {
                "source_file": path.name,
                "output_file": str(target.relative_to(output_dir)),
                "chunk_count": len(chunks),
                "requirement_count": sum(1 for chunk in chunks if chunk.content_type == "requirement"),
                "project_content_count": sum(1 for chunk in chunks if chunk.content_type == "project_content"),
            }
        )

    (output_dir / "all_kb_segments.txt").write_text(
        "\n\n".join(format_chunk(chunk, separator) for chunk in all_chunks) + ("\n" if all_chunks else ""),
        encoding="utf-8",
    )
    (output_dir / "chunks.jsonl").write_text(
        "\n".join(json.dumps(asdict(chunk), ensure_ascii=False) for chunk in all_chunks) + ("\n" if all_chunks else ""),
        encoding="utf-8",
    )
    write_manifest(output_dir / "manifest.csv", manifest_rows)
    (output_dir / "README.txt").write_text(
        "\n".join(
            [
                "Dify knowledge-base rebuild output",
                "",
                f"Custom chunk separator: {separator}",
                "",
                "Recommended upload files:",
                "- Upload files under documents/ if you want one Dify document per source DOCX.",
                "- Upload all_kb_segments.txt only if you intentionally want one combined Dify document.",
                "- Uploaded TXT embeds only section path plus original chunk text.",
                "- Full structured chunk metadata is also kept in chunks.jsonl and manifest.csv for checking.",
                "",
                "Dify segmentation setting:",
                f"- Use the custom separator exactly as: {separator}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["source_file", "output_file", "chunk_count", "requirement_count", "project_content_count"],
        )
        writer.writeheader()
        writer.writerows(rows)


def iter_source_files(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
            raise ValueError(f"Input file must be .docx or .txt: {input_path}")
        return [input_path]

    pattern = "**/*" if recursive else "*"
    return sorted(
        path
        for path in input_path.glob(pattern)
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS
        and not path.name.startswith("~$")
    )


def section_titles(section_path: str) -> list[str]:
    return [title_text(part) for part in str(section_path or "").split(" > ") if title_text(part)]


def title_text(label: str) -> str:
    return clean_title(re.sub(r"^\d+(?:\.\d+)*[\t .．]*", "", str(label or "").strip()))


def title_key(title: str) -> str:
    return re.sub(r"\s+", "", clean_title(title))


def normalize_heading_number(number: str) -> str:
    return re.sub(r"\s+", "", str(number or "").replace("．", ".")).strip(".")


def clean_title(title: str) -> str:
    cleaned = str(title or "").replace("\\t", "\t").replace("\u3000", " ").strip()
    cleaned = re.sub(r"^[.．、\s]+", "", cleaned).strip()
    cleaned = re.sub(r"[\t ]+\d{1,4}\s*$", "", cleaned).strip()
    cleaned = re.sub(r"(?<=[\u4e00-\u9fa5）)])\d{1,4}$", "", cleaned).strip()
    cleaned = re.sub(r"^[.．、\s]+", "", cleaned).strip()
    return cleaned


def clean_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def is_likely_heading(number: str, title: str) -> bool:
    title = clean_title(title)
    if not title or title.isdigit():
        return False
    if title[0] in ".．、，,；;：:）)】]":
        return False
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", title):
        return False

    depth = number.count(".") + 1
    if depth == 1 and re.search(r"[。；;]", title):
        return False

    if depth == 1:
        top_level_markers = (
            "总论",
            "现状",
            "需求",
            "方案",
            "设计",
            "设备",
            "材料",
            "清册",
            "估算",
            "概述",
            "投资",
            "结论",
            "附件",
            "项目",
            "建设",
        )
        if not any(marker in title for marker in top_level_markers):
            return False

    return True


def extract_project_name(text: str) -> str:
    match = re.search(r"(?m)^\s*项目名称\s*[:：]\s*(.+?)\s*$", str(text or "")[:10000])
    return match.group(1).strip() if match else ""


def infer_project_name(path: Path) -> str:
    name = path.stem
    name = re.sub(r"^(终稿|最终版|输入项目|可研报告)[-_、\s]*", "", name)
    name = re.sub(r"[-_、\s]*(可研报告|实施项目|研究报告|项目申请报告).*$", "", name).strip()
    return name or path.stem


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Dify KB TXT chunks from feasibility-study DOCX/TXT files.")
    parser.add_argument("--input", type=Path, default=r"G:\liuxiaowei\项目查重可研\可研终稿-2022-2024", help="A DOCX/TXT file or a directory containing DOCX/TXT files.")
    parser.add_argument("--output", type=Path, default=r"G:\liuxiaowei\项目查重可研\知识库重建1\可研终稿-2022-2024", help="Output directory.")
    parser.add_argument("--separator", default=DEFAULT_SEPARATOR, help="Custom chunk separator for Dify.")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Maximum characters per exported chunk.")
    parser.add_argument("--recursive", action="store_true", help="Recursively scan input directories for DOCX/TXT files.")
    args = parser.parse_args()

    files = iter_source_files(args.input, recursive=args.recursive)
    if not files:
        raise SystemExit(f"No .docx or .txt files found under {args.input}")

    chunks_by_doc: dict[Path, list[Chunk]] = {}
    for index, path in enumerate(files, start=1):
        chunks = build_chunks_for_source(path, max_chars=args.max_chars)
        chunks_by_doc[path] = chunks
        print(f"[{index}/{len(files)}] chunks={len(chunks)} {path.name}", flush=True)

    write_outputs(chunks_by_doc, args.output, args.separator)
    total_chunks = sum(len(chunks) for chunks in chunks_by_doc.values())
    print(f"Wrote {total_chunks} chunk(s) for {len(files)} document(s) to {args.output}")
    print(f"Use Dify custom separator: {args.separator}")


if __name__ == "__main__":
    main()
