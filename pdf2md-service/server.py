"""PDF 转 Markdown 服务，供 Dify「项目查重」工作流的 HTTP 请求节点调用。

POST /convert   multipart/form-data，字段 file 为 PDF。
                返回 Markdown 文件（Content-Disposition: attachment），HTTP 节点据此把响应当作文件，
                交给文档提取器读成文本。文件形式不受 HTTP 节点 1MB 文本上限的限制。
GET  /health    健康检查。

只依赖标准库和 converter.py 的依赖（pdfplumber、pypdf）。
"""

from __future__ import annotations

import argparse
import logging
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePath
from urllib.parse import quote

from converter import convert

MAX_UPLOAD_BYTES = 200 * 1024 * 1024

logger = logging.getLogger("pdf2md")


def read_upload(handler: BaseHTTPRequestHandler) -> tuple[str, bytes]:
    content_type = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length") or 0)
    if not content_type.startswith("multipart/form-data"):
        raise ValueError("需要 multipart/form-data，文件放在 file 字段")
    if length <= 0 or length > MAX_UPLOAD_BYTES:
        raise ValueError(f"上传大小不合法：{length} 字节")
    body = handler.rfile.read(length)
    message = BytesParser(policy=HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") == "file":
            return part.get_filename() or "document.pdf", part.get_payload(decode=True) or b""
    raise ValueError("没有找到 file 字段")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self.reply(200, b"ok", "text/plain; charset=utf-8")
        else:
            self.reply(404, "未知路径".encode(), "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path.rstrip("/") != "/convert":
            self.reply(404, "未知路径".encode(), "text/plain; charset=utf-8")
            return
        started = time.time()
        try:
            filename, data = read_upload(self)
            if not data.startswith(b"%PDF"):
                raise ValueError("上传的不是 PDF 文件")
            markdown = convert(data).encode("utf-8")
        except ValueError as error:
            self.reply(400, str(error).encode(), "text/plain; charset=utf-8")
            return
        except Exception as error:  # 转换失败要让工作流看到原因，不能静默返回空文本
            logger.exception("转换失败")
            self.reply(500, f"PDF 转换失败：{error}".encode(), "text/plain; charset=utf-8")
            return

        name = PurePath(filename).stem + ".md"
        disposition = f"attachment; filename=\"document.md\"; filename*=UTF-8''{quote(name)}"
        self.reply(200, markdown, "text/markdown; charset=utf-8", {"Content-Disposition": disposition})
        logger.info("%s → %d 字节，用时 %.1fs", filename, len(markdown), time.time() - started)

    def reply(self, status: int, body: bytes, content_type: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.debug(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info("PDF 转换服务监听 %s:%d", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
