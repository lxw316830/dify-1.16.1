"""把 .doc/.wps/.rtf 等 Word 文档转换成 docx。

依次尝试 macOS textutil、Windows 上的 WPS/Word（COM）、LibreOffice。
"""
from __future__ import annotations

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
import zipfile
from pathlib import Path
from typing import List, Optional

LOGGER = logging.getLogger("项目查重")

WORD_EXTENSIONS = {".doc", ".docx", ".docm", ".dot", ".dotx", ".wps", ".rtf", ".odt"}


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
            try:
                convert_with_textutil(source, destination)
            except RuntimeError:
                # textutil rejects some real OLE .doc files. The documented order
                # continues with LibreOffice instead of failing the whole prepare.
                soffice = find_soffice()
                if not soffice:
                    raise
                convert_with_soffice(soffice, source, destination)
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


def ensure_docx(source: Path, work_dir: Path) -> Path:
    """合法 docx 直接返回原路径，其他格式转换到 work_dir/input.docx。"""
    if source.suffix.lower() not in WORD_EXTENSIONS:
        raise RuntimeError(f"不支持的文件格式：{source.suffix}（支持 {'、'.join(sorted(WORD_EXTENSIONS))}）")
    if source.suffix.lower() == ".docx" and is_valid_docx(source):
        return source
    destination = work_dir / "input.docx"
    converter = DocumentConverter()
    try:
        converter.ensure_available()
        destination.parent.mkdir(parents=True, exist_ok=True)
        converter.convert(source, destination)
    finally:
        converter.close()
    if not is_valid_docx(destination):
        raise RuntimeError(f"转换结果不是合法 docx：{source.name}")
    return destination
