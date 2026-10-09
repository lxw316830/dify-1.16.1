# 项目查重批量测试
# 扫描文件夹中的可研文档，校验格式，把非 docx 转成 docx，再调用项目查重工作流。
# 查重报告按原文件名保存为 md，转换出的 docx 只在临时目录中使用。
from __future__ import annotations

import argparse
import json
import logging
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("项目查重")

DEFAULT_WORKFLOW_URL = "http://127.0.0.1/workflow/HNTRwRtfeqgkyPA1"
WORD_EXTENSIONS = {".doc", ".docx", ".docm", ".dot", ".dotx", ".wps", ".rtf", ".odt"}
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OUTPUT_KEYS = ("result", "answer", "output", "text", "report", "markdown", "error_msg")
WORKFLOW_URL_PATTERN = re.compile(r"^(https?://[^/]+)/workflow/([^/?#]+)/?$")


@dataclass
class PreparedDocument:
    source: Path
    relative: Path
    stem: str
    docx_path: Path
    report_path: Path


@dataclass
class WorkflowOutcome:
    status: str
    text: str
    elapsed_time: Optional[float]
    workflow_run_id: str
    error: str


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_dir = args.input.expanduser().resolve()
    if not input_dir.is_dir():
        print(f"输入文件夹不存在：{input_dir}", file=sys.stderr)
        return 2

    output_dir = args.output.expanduser().resolve() if args.output else input_dir.parent / f"{input_dir.name}-查重结果"
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_logging()

    sources = collect_documents(input_dir, output_dir)
    if args.limit > 0:
        sources = sources[: args.limit]
    LOGGER.info("待处理 %s 个文档，结果写入 %s", len(sources), output_dir)
    if not sources:
        LOGGER.error("没有找到可处理的 Word 文档（支持 %s）", "、".join(sorted(WORD_EXTENSIONS)))
        return 1

    client: Optional[WorkflowClient] = None
    if not args.convert_only:
        client = build_client(args)
        try:
            LOGGER.info("正在连接工作流")
            client.prepare()
        except Exception as exc:
            LOGGER.error("连接项目查重工作流失败：%s", exc)
            return 1

    converter = DocumentConverter()
    try:
        if any(not is_passthrough_docx(path) for path in sources):
            try:
                converter.ensure_available()
            except Exception as exc:
                LOGGER.error("%s", exc)
                return 1
        with tempfile.TemporaryDirectory(prefix="dedup-docx-") as temp_name:
            return process_documents(
                sources,
                input_dir,
                output_dir,
                Path(temp_name),
                converter,
                client,
                convert_only=args.convert_only,
            )
    finally:
        converter.close()


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量校验文档格式、转换为 docx，并测试项目查重工作流。")
    parser.add_argument("--input", required=True, type=Path, help="存放可研文档的文件夹")
    parser.add_argument("--output", type=Path, help="结果文件夹。默认是输入文件夹同级的「原文件夹名-查重结果」")
    parser.add_argument("--workflow-url", default=DEFAULT_WORKFLOW_URL, help="项目查重 Web 应用地址，形如 http://host/workflow/应用码")
    parser.add_argument("--api-base", default="", help="使用应用 API Key 时的服务地址，例如 http://127.0.0.1")
    parser.add_argument("--api-key", default="", help="应用 API Key。传入后走 /v1 接口，不再使用 Web 应用地址")
    parser.add_argument("--input-name", default="", help="工作流文件变量名。留空时自动读取工作流参数")
    parser.add_argument("--user", default="project-dedup-test", help="调用工作流使用的用户标识")
    parser.add_argument("--timeout", type=int, default=3600, help="单篇查重的最长等待秒数")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个文档，0 表示全部")
    parser.add_argument("--convert-only", action="store_true", help="只做格式校验和 docx 转换，不调用工作流")
    return parser.parse_args(argv)


def setup_logging() -> None:
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)


def log_progress(current: int, total: int, action: str, name: str) -> None:
    width = 20
    filled = max(1, int(width * current / total)) if current and total else 0
    bar = "#" * filled + "-" * (width - filled)
    LOGGER.info("[%s] %s/%s %s %s", bar, current, total, action, name)


def process_documents(
    sources: Sequence[Path],
    input_dir: Path,
    output_dir: Path,
    temp_dir: Path,
    converter: "DocumentConverter",
    client: Optional["WorkflowClient"],
    convert_only: bool,
) -> int:
    total = len(sources)
    success_count = 0
    failure_count = 0
    used_stems: set[Tuple[str, str]] = set()
    for index, source in enumerate(sources, start=1):
        relative = source.relative_to(input_dir)
        stem = unique_stem(relative, used_stems)
        report_path = output_dir / relative.parent / f"{stem}.md"
        document = PreparedDocument(
            source=source,
            relative=relative,
            stem=stem,
            docx_path=source,
            report_path=report_path,
        )
        try:
            if is_passthrough_docx(source):
                docx_path = source
            else:
                log_progress(index, total, "转换", source.name)
                docx_path = prepare_docx(source, temp_dir / relative.parent / f"{stem}.docx", converter)
            document.docx_path = docx_path
        except Exception as exc:
            failure_count += 1
            LOGGER.error("失败 %s/%s %s：%s", index, total, source.name, exc)
            if "转换超时" in str(exc):
                LOGGER.error("这篇转换超时，已关闭 WPS/Word 并继续下一篇。若窗口里有确认框，请先关掉。")
                converter.reset_after_timeout()
            continue

        if convert_only:
            success_count += 1
            continue

        log_progress(index, total, "查重", source.name)
        assert client is not None
        try:
            outcome = client.run(docx_path)
        except Exception as exc:
            failure_count += 1
            save_report(report_path, failure_report(document, str(exc)))
            LOGGER.error("失败 %s/%s %s：%s", index, total, source.name, exc)
            continue

        if outcome.status != "succeeded" or not outcome.text.strip():
            failure_count += 1
            message = outcome.error or "工作流没有返回查重报告"
            save_report(report_path, failure_report(document, message, outcome))
            LOGGER.error("失败 %s/%s %s：%s", index, total, source.name, message)
            continue

        save_report(report_path, outcome.text)
        success_count += 1
        LOGGER.info(
            "[%s] %s/%s 完成 %s %s",
            "#" * 20,
            index,
            total,
            extract_conclusion(outcome.text) or "未识别结论",
            source.name,
        )

    LOGGER.info("全部结束。成功 %s，失败 %s。结果目录：%s", success_count, failure_count, output_dir)
    return 0 if failure_count == 0 else 1


def collect_documents(input_dir: Path, output_dir: Path) -> List[Path]:
    documents: List[Path] = []
    output_resolved = output_dir.resolve()
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.name.startswith(".") or path.name.startswith("~$"):
            continue
        if is_inside(path, output_resolved):
            continue
        if path.suffix.lower() not in WORD_EXTENSIONS:
            continue
        documents.append(path)
    return documents


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent)
    except ValueError:
        return False
    return True


def unique_stem(relative: Path, used: set[Tuple[str, str]]) -> str:
    parent = str(relative.parent)
    stem = relative.stem
    if (parent, stem) not in used:
        used.add((parent, stem))
        return stem
    candidate = f"{stem}{relative.suffix}"
    index = 2
    while (parent, candidate) in used:
        candidate = f"{stem}{relative.suffix}-{index}"
        index += 1
    used.add((parent, candidate))
    LOGGER.warning("文件名重复，结果将保存为 %s", candidate)
    return candidate


def is_passthrough_docx(path: Path) -> bool:
    return path.suffix.lower() == ".docx" and is_valid_docx(path)


def prepare_docx(source: Path, destination: Path, converter: "DocumentConverter") -> Path:
    if is_passthrough_docx(source):
        return source

    destination.parent.mkdir(parents=True, exist_ok=True)
    converter.convert(source, destination)
    if not is_valid_docx(destination):
        raise RuntimeError(f"转换结果不是合法 docx：{source.name}")
    if docx_text_length(destination) < 100:
        LOGGER.warning("转换结果正文过短：%s", source.name)
    return destination


def is_valid_docx(path: Path) -> bool:
    if not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    return "word/document.xml" in names and "[Content_Types].xml" in names


def docx_text_length(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    text = re.sub(br"<[^>]+>", b"", xml)
    return len(text.decode("utf-8", "ignore").strip())


def convert_with_textutil(source: Path, destination: Path) -> None:
    textutil = shutil.which("textutil")
    if not textutil:
        raise RuntimeError("未找到 textutil，无法把文档转换为 docx")
    if destination.exists():
        destination.unlink()
    completed = subprocess.run(
        [textutil, "-convert", "docx", "-output", str(destination), str(source)],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if completed.returncode != 0 or not destination.exists():
        detail = (completed.stderr or completed.stdout or "textutil 没有生成文件").strip()
        raise RuntimeError(detail)


def find_soffice() -> Optional[str]:
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return found
    candidates = (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def convert_with_soffice(soffice: str, source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    with tempfile.TemporaryDirectory(prefix="docx-convert-") as temp_dir:
        profile_dir = Path(temp_dir) / "profile"
        output_dir = Path(temp_dir) / "out"
        output_dir.mkdir()
        command = [
            soffice,
            f"-env:UserInstallation={profile_dir.as_uri()}",
            "--headless",
            "--norestore",
            "--convert-to",
            "docx",
            "--outdir",
            str(output_dir),
            str(source),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)
        produced = list(output_dir.glob("*.docx"))
        if completed.returncode != 0 or len(produced) != 1:
            detail = (completed.stderr or completed.stdout or "LibreOffice 没有生成 docx").strip()
            raise RuntimeError(detail)
        shutil.move(str(produced[0]), destination)


class DocumentConverter:
    def __init__(self) -> None:
        self.kind = ""
        self.backend_name = ""
        self._soffice = ""
        self._office: Optional["WindowsOfficeSession"] = None

    def ensure_available(self) -> str:
        if self.kind:
            return self.kind
        if shutil.which("textutil"):
            self.kind = "textutil"
            self.backend_name = "textutil"
            LOGGER.info("使用 textutil 转换文档")
            return self.kind
        if sys.platform.startswith("win"):
            try:
                self._start_windows_office()
                return self.kind
            except Exception as exc:
                LOGGER.warning("未能使用 Word/WPS 转换：%s", exc)
                self.close()
        soffice = find_soffice()
        if soffice:
            self.kind = "libreoffice"
            self.backend_name = "LibreOffice"
            self._soffice = soffice
            LOGGER.info("使用 LibreOffice 转换文档")
            return self.kind
        raise RuntimeError(
            "无法把 .doc/.wps 转成 docx。请在这台 Windows 上安装 WPS、Microsoft Word 或 LibreOffice 后重新运行。"
        )

    def convert(self, source: Path, destination: Path) -> None:
        self.ensure_available()
        if self.kind == "textutil":
            convert_with_textutil(source, destination)
            return
        if self.kind == "libreoffice":
            convert_with_soffice(self._soffice, source, destination)
            return
        if self._office is None:
            raise RuntimeError("Word/WPS 转换进程未启动")
        self._office.convert(source, destination)

    def close(self) -> None:
        if self._office is not None:
            self._office.close()
            self._office = None
            if self.kind == "office":
                self.kind = ""

    def reset_after_timeout(self) -> None:
        if self._office is not None:
            self._office.close(force=True)
            self._office = None
        self.kind = ""

    def _start_windows_office(self) -> None:
        stop_office_processes()
        errors: List[str] = []
        for executable in windows_powershell_executables():
            session = WindowsOfficeSession(executable)
            try:
                backend_name = session.start()
            except Exception as exc:
                session.close()
                errors.append(f"{executable}：{exc}")
                continue
            self._office = session
            self.kind = "office"
            self.backend_name = backend_name
            LOGGER.info("使用 %s 转换文档", backend_name)
            return
        detail = "；".join(errors) if errors else "未找到 PowerShell"
        raise RuntimeError(detail)


def stop_office_processes() -> None:
    if not sys.platform.startswith("win"):
        return
    for image_name in ("wps.exe", "winword.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", image_name],
            capture_output=True,
            check=False,
        )


def windows_powershell_executables() -> List[str]:
    executables = ["powershell.exe"]
    wow64 = Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe")
    if wow64.is_file():
        executables.append(str(wow64))
    return executables


class WindowsOfficeSession:
    def __init__(self, powershell: str) -> None:
        self.powershell = powershell
        self._process: Optional[subprocess.Popen[str]] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr: List[str] = []

    def start(self) -> str:
        script_path = Path(tempfile.gettempdir()) / "dify_docx_convert.ps1"
        script_path.write_text(WINDOWS_OFFICE_SCRIPT, encoding="utf-8")
        self._process = subprocess.Popen(
            [
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        line = self._read(90)
        if not line.startswith("READY"):
            raise RuntimeError(self._failure_detail(line))
        return line.split(None, 1)[1] if " " in line else "Office"

    def convert(self, source: Path, destination: Path) -> None:
        if destination.exists():
            destination.unlink()
        self._send({"cmd": "convert", "source": str(source), "destination": str(destination)})
        line = self._read(300)
        if line != "OK":
            raise RuntimeError(self._failure_detail(line))

    def close(self, force: bool = False) -> None:
        process = self._process
        self._process = None
        if process is None:
            if force:
                stop_office_processes()
            return
        if process.poll() is not None:
            if force:
                stop_office_processes()
            return
        try:
            if process.stdin and not force:
                process.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                process.stdin.flush()
            process.wait(timeout=5 if force else 20)
        except Exception:
            process.kill()
            process.wait(timeout=5)
        if force:
            stop_office_processes()

    def _send(self, payload: dict) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError(self._failure_detail("Word/WPS 转换进程已退出"))
        process.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
        process.stdin.flush()

    def _read(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(self._failure_detail("转换超时"))
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError(self._failure_detail("转换超时")) from exc
            if line is None:
                raise RuntimeError(self._failure_detail("转换进程已退出"))
            text = line.lstrip("\ufeff").strip()
            if text:
                return text

    def _failure_detail(self, message: str) -> str:
        extra = " ".join(self._stderr[-6:]).strip()
        if extra and extra not in message:
            return f"{message} {extra}".strip()
        return message.strip()

    def _pump_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._lines.put(None)
            return
        for line in process.stdout:
            self._lines.put(line.rstrip("\r\n"))
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr.append(line.rstrip("\r\n"))


WINDOWS_OFFICE_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$utf8 = New-Object System.Text.UTF8Encoding $false
$reader = New-Object System.IO.StreamReader ([Console]::OpenStandardInput(), $utf8, $false, 4096, $true)
$writer = New-Object System.IO.StreamWriter ([Console]::OpenStandardOutput(), $utf8)
$writer.AutoFlush = $true

function Write-Msg([string]$text) {
    $writer.WriteLine($text)
}

function Close-Doc($doc) {
    if ($null -ne $doc) {
        try { $doc.Close($false) | Out-Null } catch {}
    }
}

function Save-Docx($doc, [string]$destination) {
    $format = 12
    try {
        $doc.SaveAs2($destination, $format) | Out-Null
        return
    } catch {}
    try {
        $doc.SaveAs($destination, $format) | Out-Null
        return
    } catch {}
    $dest = $destination
    $doc.SaveAs([ref]$dest, [ref]$format) | Out-Null
}

$progIds = @("KWPS.Application", "Word.Application", "wps.Application")
$app = $null
$progId = ""
foreach ($id in $progIds) {
    try {
        $app = New-Object -ComObject $id
        $progId = $id
        break
    } catch {}
}
if ($null -eq $app) {
    Write-Msg "ERR no Word or WPS"
    exit 1
}
try { $app.Visible = $false } catch {}
try { $app.DisplayAlerts = 0 } catch {}
try { $app.AutomationSecurity = 3 } catch {}
try { $app.ScreenUpdating = $false } catch {}
try { $app.Options.ConfirmConversions = $false } catch {}
try { $app.Options.UpdateLinksAtOpen = $false } catch {}
try { $app.Options.DoNotPromptForConvert = $true } catch {}
Write-Msg ("READY " + $progId)

try {
    while ($true) {
        $line = $reader.ReadLine()
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $msg = $line | ConvertFrom-Json
        if ($msg.cmd -eq "quit") { break }
        if ($msg.cmd -ne "convert") {
            Write-Msg "ERR unknown command"
            continue
        }
        $doc = $null
        try {
            if (Test-Path -LiteralPath $msg.destination) {
                Remove-Item -LiteralPath $msg.destination -Force
            }
            $parent = Split-Path -Parent $msg.destination
            if ($parent -and -not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Path $parent | Out-Null
            }
            try {
                $doc = $app.Documents.OpenNoRepairDialog($msg.source, $false, $true, $false)
            } catch {}
            if ($null -eq $doc) {
                try {
                    $doc = $app.Documents.Open($msg.source, $false, $true, $false)
                } catch {
                    $doc = $app.Documents.Open($msg.source)
                }
            }
            Save-Docx $doc $msg.destination
            Close-Doc $doc
            $doc = $null
            if (Test-Path -LiteralPath $msg.destination) {
                Write-Msg "OK"
            } else {
                Write-Msg "ERR output missing"
            }
        } catch {
            Close-Doc $doc
            $doc = $null
            $err = ($_.Exception.Message -replace "[\r\n\t]", " ")
            Write-Msg ("ERR " + $err)
        }
    }
} finally {
    try {
        foreach ($openDoc in @($app.Documents)) {
            try { $openDoc.Close($false) | Out-Null } catch {}
        }
    } catch {}
    try { $app.Quit() } catch {}
    try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null } catch {}
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
"""


class WorkflowClient:
    def __init__(
        self,
        workflow_url: str,
        api_base: str,
        api_key: str,
        input_name: str,
        user: str,
        timeout: int,
    ) -> None:
        self.workflow_url = workflow_url.strip()
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key.strip()
        self.input_name = input_name.strip()
        self.user = user
        self.timeout = timeout
        self.base_url = ""
        self.app_code = ""
        self.passport = ""

    def prepare(self) -> None:
        if self.api_key:
            if not self.api_base:
                raise RuntimeError("使用 API Key 时需要同时传入 --api-base")
            self.base_url = self.api_base
            parameters = self._request_json("GET", f"{self.base_url}/v1/parameters", self._api_headers())
        else:
            self.base_url, self.app_code = parse_workflow_url(self.workflow_url)
            passport_payload = self._request_json(
                "GET",
                f"{self.base_url}/api/passport?user_id={urllib.parse.quote(self.user)}",
                {"X-App-Code": self.app_code},
            )
            token = str(passport_payload.get("access_token") or "")
            if not token:
                raise RuntimeError("工作流连接成功，但响应里没有 access_token")
            self.passport = token
            parameters = self._request_json("GET", f"{self.base_url}/api/parameters", self._web_headers())

        detected = find_file_input_name(parameters)
        if detected and not self.input_name:
            self.input_name = detected
        if not self.input_name:
            self.input_name = "input"

    def run(self, docx_path: Path) -> WorkflowOutcome:
        file_id = self._upload(docx_path)
        inputs = {
            self.input_name: {
                "transfer_method": "local_file",
                "upload_file_id": file_id,
                "type": "document",
            }
        }
        if self.api_key:
            url = f"{self.base_url}/v1/workflows/run"
            headers = self._api_headers({"Content-Type": "application/json"})
            body = json.dumps(
                {"inputs": inputs, "response_mode": "streaming", "user": self.user},
                ensure_ascii=False,
            ).encode("utf-8")
        else:
            url = f"{self.base_url}/api/workflows/run"
            headers = self._web_headers({"Content-Type": "application/json"})
            body = json.dumps({"inputs": inputs}, ensure_ascii=False).encode("utf-8")
        return self._read_workflow_stream(url, headers, body)

    def _upload(self, docx_path: Path) -> str:
        content = docx_path.read_bytes()
        if self.api_key:
            url = f"{self.base_url}/v1/files/upload"
            fields = {"user": self.user}
            header_seed = self._api_headers()
        else:
            url = f"{self.base_url}/api/files/upload"
            fields = {}
            header_seed = self._web_headers()
        boundary, payload = encode_multipart(fields, docx_path.name, content)
        headers = dict(header_seed)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        payload_json = self._request_json("POST", url, headers, payload, timeout=self.timeout)
        file_id = str(payload_json.get("id") or "")
        if not file_id:
            raise RuntimeError(f"上传响应里没有文件 ID：{json.dumps(payload_json, ensure_ascii=False)[:300]}")
        return file_id

    def _read_workflow_stream(self, url: str, headers: Dict[str, str], body: bytes) -> WorkflowOutcome:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接工作流：{exc.reason}") from exc

        with response:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                payload = json.loads(response.read().decode("utf-8"))
                return outcome_from_payload(payload)

            text_chunks: List[str] = []
            finished: Optional[dict] = None
            for event_name, data_text in iter_sse(response):
                if not data_text or data_text == "[DONE]":
                    continue
                try:
                    event = json.loads(data_text)
                except json.JSONDecodeError:
                    LOGGER.warning("忽略无法解析的工作流事件：%s", data_text[:200])
                    continue
                if event_name and not event.get("event"):
                    event["event"] = event_name
                log_workflow_event(event)
                event_type = str(event.get("event") or "")
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                if event_type == "text_chunk":
                    text_chunks.append(str(data.get("text") or ""))
                elif event_type == "text_replace":
                    text_chunks = [str(data.get("text") or "")]
                elif event_type == "workflow_finished":
                    finished = event
            if finished is None:
                raise RuntimeError("工作流连接结束，但没有收到完成事件")
            outcome = outcome_from_payload(finished)
            if not outcome.text.strip() and text_chunks:
                outcome.text = "".join(text_chunks)
            return outcome

    def _api_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if extra:
            headers.update(extra)
        return headers

    def _web_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"X-App-Code": self.app_code, "X-App-Passport": self.passport}
        if extra:
            headers.update(extra)
        return headers

    def _request_json(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes] = None,
        timeout: int = 60,
    ) -> dict:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {exc.code} {url}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 {url}：{exc.reason}") from exc
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"响应不是 JSON：{raw[:300]}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"响应格式无法识别：{raw[:300]}")
        return payload


def build_client(args: argparse.Namespace) -> WorkflowClient:
    return WorkflowClient(
        workflow_url=args.workflow_url,
        api_base=args.api_base,
        api_key=args.api_key,
        input_name=args.input_name,
        user=args.user,
        timeout=args.timeout,
    )


def parse_workflow_url(workflow_url: str) -> Tuple[str, str]:
    match = WORKFLOW_URL_PATTERN.match(workflow_url.strip())
    if not match:
        raise RuntimeError("运行地址需要是 http://主机/workflow/应用码")
    return match.group(1), match.group(2)


def find_file_input_name(parameters: dict) -> str:
    form = parameters.get("user_input_form")
    if not isinstance(form, list):
        return ""
    for item in form:
        if not isinstance(item, dict):
            continue
        for key, field in item.items():
            if not isinstance(field, dict):
                continue
            variable = str(field.get("variable") or "")
            if variable and (field.get("type") == "file" or key == "file"):
                return variable
    return ""


def encode_multipart(fields: Dict[str, str], filename: str, content: bytes) -> Tuple[str, bytes]:
    boundary = f"----DifyDedup{uuid.uuid4().hex}"
    safe_name = filename.replace("\\", "_").replace('"', "")
    chunks: List[bytes] = []
    for key, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {DOCX_CONTENT_TYPE}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(chunks)


def iter_sse(response: Iterable[bytes]) -> Iterable[Tuple[str, str]]:
    event_name = ""
    data_lines: List[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data_lines or event_name:
                yield event_name, "\n".join(data_lines)
            event_name = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines or event_name:
        yield event_name, "\n".join(data_lines)


def log_workflow_event(event: dict) -> None:
    if str(event.get("event") or "") != "error":
        return
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    LOGGER.error("工作流错误：%s", data.get("message") or event.get("message") or event)


def outcome_from_payload(payload: dict) -> WorkflowOutcome:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else {}
    status = str(data.get("status") or payload.get("status") or "")
    error = str(data.get("error") or payload.get("error") or "")
    elapsed = data.get("elapsed_time")
    elapsed_time = float(elapsed) if isinstance(elapsed, (int, float)) else None
    return WorkflowOutcome(
        status=status or ("succeeded" if outputs else "failed"),
        text=extract_output_text(outputs),
        elapsed_time=elapsed_time,
        workflow_run_id=str(payload.get("workflow_run_id") or data.get("id") or ""),
        error=error,
    )


def extract_output_text(outputs: dict) -> str:
    for key in OUTPUT_KEYS:
        value = outputs.get(key)
        if value not in (None, ""):
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if len(outputs) == 1:
        value = next(iter(outputs.values()))
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if outputs:
        return json.dumps(outputs, ensure_ascii=False, indent=2)
    return ""


def extract_conclusion(report: str) -> str:
    match = re.search(r"\[查重结论\][\s\S]{0,80}?(不重复|重复)", report)
    if match:
        return match.group(1)
    return ""


def save_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def failure_report(document: PreparedDocument, message: str, outcome: Optional[WorkflowOutcome] = None) -> str:
    lines = [
        "# 查重未完成",
        "",
        f"- 源文件：{document.relative}",
        f"- 说明：{message}",
    ]
    if outcome and outcome.workflow_run_id:
        lines.append(f"- 运行 ID：{outcome.workflow_run_id}")
    if outcome and outcome.text.strip():
        lines.extend(["", outcome.text.strip()])
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
