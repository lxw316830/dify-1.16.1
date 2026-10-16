"""项目查重：把 docx 上传到查重服务，运行「项目查重」工作流，把报告保存为 Markdown。只依赖标准库。"""

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config" / "service.json"
# 工作流开始节点的文件变量名
INPUT_VARIABLE = "input"
# 结束节点输出中，按顺序取第一个非空字段作为报告正文
REPORT_FIELDS = ("result", "answer", "output", "text", "report", "markdown")
# 流式响应两次数据之间的最长等待时间（秒）；服务运行中会定期发 ping
READ_TIMEOUT = 600


class DedupError(Exception):
    pass


def clean_error(text) -> str:
    """服务端错误常带内部调用栈，只保留其中的 message。"""
    text = str(text or "")
    start = text.find("{")
    if start != -1:
        try:
            detail = json.loads(text[start:])
        except json.JSONDecodeError:
            detail = None
        if isinstance(detail, dict):
            args = detail.get("args") if isinstance(detail.get("args"), dict) else {}
            message = detail.get("message") or args.get("description")
            if isinstance(message, str) and message:
                text = message
    text = text.split("Traceback (most recent call last)")[0].strip()
    return text[:500] or "无错误信息"


def load_config() -> dict:
    config = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    api_base = (os.environ.get("DEDUP_API_BASE") or config.get("api_base") or "").rstrip("/")
    api_key = os.environ.get("DEDUP_API_KEY") or config.get("api_key") or ""
    if not api_base or not api_key:
        sys.exit(f"缺少查重服务地址或 API 密钥：请填写 {CONFIG}，或设置环境变量 DEDUP_API_BASE / DEDUP_API_KEY")
    if not api_base.endswith("/v1"):
        api_base += "/v1"
    return {"api_base": api_base, "api_key": api_key, "user": config.get("user") or "project-dedup"}


def open_request(config: dict, path: str, body: bytes, content_type: str, timeout: int):
    request = urllib.request.Request(
        f"{config['api_base']}{path}",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": content_type},
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise DedupError(f"{path} 返回 HTTP {e.code}：{clean_error(e.read().decode('utf-8', 'replace'))}") from e
    except urllib.error.URLError as e:
        raise DedupError(f"无法连接查重服务（{config['api_base']}）：{e.reason}") from e


def upload(path: Path, config: dict) -> str:
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    filename = path.name.replace("\\", "_").replace('"', "_").replace("\r", "").replace("\n", "")
    body = b"".join(
        [
            f'--{boundary}\r\nContent-Disposition: form-data; name="user"\r\n\r\n{config["user"]}\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    with open_request(config, "/files/upload", body, f"multipart/form-data; boundary={boundary}", 120) as response:
        return json.loads(response.read())["id"]


def run_workflow(file_id: str, config: dict) -> dict:
    """以流式模式运行工作流，返回 workflow_finished 事件的 data。"""
    payload = {
        "inputs": {
            INPUT_VARIABLE: {"type": "document", "transfer_method": "local_file", "upload_file_id": file_id}
        },
        "response_mode": "streaming",
        "user": config["user"],
    }
    body = json.dumps(payload).encode()
    with open_request(config, "/workflows/run", body, "application/json", READ_TIMEOUT) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            if event.get("event") == "workflow_finished":
                return event.get("data") or {}
            if event.get("event") == "error":
                raise DedupError(f"工作流报错：{clean_error(event.get('message') or event)}")
    raise DedupError("连接已结束但没有收到工作流完成事件，请到查重服务的运行日志确认这次任务的状态")


def pick_report(outputs: dict) -> str:
    for field in REPORT_FIELDS:
        value = outputs.get(field)
        if value:
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if len(outputs) == 1:
        value = next(iter(outputs.values()))
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return json.dumps(outputs, ensure_ascii=False, indent=2)


def dedup_one(path: Path, report_path: Path, config: dict) -> None:
    file_id = upload(path, config)
    data = run_workflow(file_id, config)
    if data.get("status") != "succeeded":
        raise DedupError(f"工作流状态 {data.get('status')}：{clean_error(data.get('error'))}")
    outputs = data.get("outputs") or {}
    # 工作流在校验失败时走错误分支，状态仍是 succeeded，只输出 error_msg
    if outputs.get("error_msg"):
        raise DedupError(f"工作流未完成查重：{outputs['error_msg']}")
    report = pick_report(outputs)
    if not report.strip():
        raise DedupError("工作流没有返回报告内容")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")


def collect(source: Path, output: Path) -> list[tuple[Path, Path]]:
    if source.is_file():
        return [(source, output / f"{source.stem}.md")]
    return [
        (path, output / path.relative_to(source).with_suffix(".md"))
        for path in sorted(source.rglob("*"))
        if path.is_file() and path.suffix.lower() == ".docx" and not path.name.startswith(("~$", "._"))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="docx 文档，或包含 docx 的文件夹（递归查找）")
    parser.add_argument("--output", type=Path, default=Path("dedup-reports"), help="报告目录，默认 ./dedup-reports")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有报告")
    args = parser.parse_args()

    source: Path = args.source
    if not source.exists():
        sys.exit(f"路径不存在：{source}")
    if source.is_file() and source.suffix.lower() != ".docx":
        sys.exit(f"只支持 docx：{source}。doc、wps 等格式请先用 Word/WPS 另存为 docx")
    jobs = collect(source, args.output)
    if not jobs:
        sys.exit(f"没有找到 docx 文档：{source}")

    config = load_config()
    results = []
    for index, (path, report_path) in enumerate(jobs, 1):
        print(f"[{index}/{len(jobs)}] {path}", file=sys.stderr, flush=True)
        if path.stat().st_size == 0:
            results.append({"file": str(path), "status": "failed", "error": "文件为空"})
        elif report_path.exists() and not args.overwrite:
            results.append({"file": str(path), "status": "skipped", "report": str(report_path), "error": "报告已存在，加 --overwrite 覆盖"})
        else:
            try:
                dedup_one(path, report_path, config)
                results.append({"file": str(path), "status": "succeeded", "report": str(report_path)})
            except (DedupError, OSError, ValueError, KeyError) as e:
                results.append({"file": str(path), "status": "failed", "error": str(e)})
        print(f"    {results[-1]['status']}  {results[-1].get('error', results[-1].get('report'))}", file=sys.stderr, flush=True)

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(r["status"] == "succeeded" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
