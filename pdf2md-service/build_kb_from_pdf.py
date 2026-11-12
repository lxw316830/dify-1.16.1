"""把可研 PDF 做成 Dify 知识库的入库文件。

PDF 只负责转成文本（converter.py），章节识别、入库范围、标签、切分长度和输出格式
全部复用仓库根目录的 build_project_kb_chunks.py，与 docx 入库完全一致。

用法：
    python build_kb_from_pdf.py --input <PDF 或目录> --output <输出目录>
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # build_project_kb_chunks.py 在仓库根目录

from build_project_kb_chunks import (  # noqa: E402
    DEFAULT_MAX_CHARS,
    DEFAULT_SEPARATOR,
    Chunk,
    build_chunks_from_text,
    restore_auto_numbered_headings,
    write_outputs,
)
from converter import convert  # noqa: E402


def build_for_pdf(pdf_path: str, max_chars: int) -> tuple[str, list[Chunk]]:
    logging.disable(logging.CRITICAL)
    path = Path(pdf_path)
    text = restore_auto_numbered_headings(convert(path.read_bytes()))
    return pdf_path, build_chunks_from_text(text, path, max_chars)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Dify KB TXT chunks from feasibility-study PDFs.")
    parser.add_argument("--input", type=Path, required=True, help="PDF 文件或包含 PDF 的目录")
    parser.add_argument("--output", type=Path, required=True, help="输出目录")
    parser.add_argument("--separator", default=DEFAULT_SEPARATOR)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    if args.input.is_file():
        pdfs = [args.input]
    else:
        pdfs = sorted(p for p in args.input.glob("*.pdf") if not p.name.startswith(("._", "~$")))
    if not pdfs:
        raise SystemExit(f"没有找到 PDF：{args.input}")

    chunks_by_doc: dict[Path, list[Chunk]] = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(build_for_pdf, str(p), args.max_chars) for p in pdfs]
        for done, future in enumerate(futures, start=1):
            pdf_path, chunks = future.result()
            chunks_by_doc[Path(pdf_path)] = chunks
            requirement = sum(c.content_type == "requirement" for c in chunks)
            content = sum(c.content_type == "project_content" for c in chunks)
            print(f"[{done}/{len(pdfs)}] 需求 {requirement} 建设内容 {content}  {Path(pdf_path).name}", flush=True)

    write_outputs(chunks_by_doc, args.output, args.separator)
    total = sum(len(c) for c in chunks_by_doc.values())
    print(f"共 {total} 块，{len(pdfs)} 份文档，输出到 {args.output}")
    print(f"Dify 自定义分隔符：{args.separator}")


if __name__ == "__main__":
    main()
