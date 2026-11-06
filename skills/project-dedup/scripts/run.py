"""项目查重：把 docx 上传到查重服务，运行「项目查重」工作流，把报告保存为 Markdown。只依赖标准库。

每次调用最多运行 --budget 秒，进度记在报告目录的 .dedup-state.json 里。
没有全部完成时退出码为 2，用完全相同的参数再运行一次即可接着处理。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config" / "service.json"
# 工作流开始节点的文件变量名
INPUT_VARIABLE = "input"
# 结束节点输出中，按顺序取第一个非空字段作为报告正文
REPORT_FIELDS = ("result", "answer", "output", "text", "report", "markdown")

STATE_NAME = ".dedup-state.json"
LOCK_NAME = ".dedup.lock"
DEFAULT_BUDGET = 240  # 单次调用的时间上限（秒），给平台的 5 分钟留余量
POLL_INTERVAL = 60  # 连接断开后，查询运行状态的间隔（秒）
START_TIMEOUT = 90  # 发起运行后等待 workflow_run_id 的时间（秒），超过后改为查运行日志
STREAM_READ_TIMEOUT = 600  # 流式连接两次数据之间的最长等待（秒）
NOT_FOUND_GRACE = 300  # 刚提交时查询返回 404 的容忍时间（秒）
LOST_GRACE = 600  # 发起运行后日志里一直查不到记录，超过这个时间就重新提交（秒）
MAX_RUN_SECONDS = 7200  # 单篇最长运行时间（秒），超过记为失败
UPLOAD_MIN_WINDOW = 150  # 还不知道上传速度时，剩余时间少于这个值就不开始上传（秒）
MAX_START_ATTEMPTS = 3  # 发起运行遇到限流、服务端错误时的最多尝试次数
RETRY_DELAY = 60  # 发起运行失败后，再次尝试前的等待（秒）
RESET_REUSE = 1800  # 带 --overwrite / --retry-failed 的调用间隔不到这个时间，算同一批，不再重置（秒）

UNFINISHED = ("pending", "uploaded", "starting", "running")
DONE = ("succeeded", "failed", "skipped")


class DedupError(Exception):
    pass


class HttpError(DedupError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class FatalError(DedupError):
    """查重服务不可用（连不上、密钥错误），这次调用不再继续。"""


class UploadTimeout(DedupError):
    pass


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


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


def mb(size: float) -> str:
    return f"{size / 1024 / 1024:.1f}MB"


# ---------- 配置与 HTTP ----------


def load_config() -> dict:
    config = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    api_base = (os.environ.get("DEDUP_API_BASE") or config.get("api_base") or "").rstrip("/")
    api_key = os.environ.get("DEDUP_API_KEY") or config.get("api_key") or ""
    if not api_base or not api_key:
        sys.exit(f"缺少查重服务地址或 API 密钥：请填写 {CONFIG}，或设置环境变量 DEDUP_API_BASE / DEDUP_API_KEY")
    if not api_base.endswith("/v1"):
        api_base += "/v1"
    return {"api_base": api_base, "api_key": api_key, "user": config.get("user") or "project-dedup"}


def api_request(config: dict, method: str, path: str, *, body=None, content_type=None, query=None, timeout=60):
    url = f"{config['api_base']}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    if content_type:
        headers["Content-Type"] = content_type
    if isinstance(body, UploadBody):
        headers["Content-Length"] = str(body.length)
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, f"{path} 返回 HTTP {e.code}：{clean_error(e.read().decode('utf-8', 'replace'))}") from e
    except urllib.error.URLError as e:
        raise DedupError(f"无法连接查重服务（{config['api_base']}）：{e.reason}") from e


def get_json(config: dict, path: str, query=None) -> dict:
    with api_request(config, "GET", path, query=query, timeout=60) as response:
        return json.loads(response.read())


class UploadBody:
    """multipart 请求体。分块读出，读的同时打印进度，超过截止时间就中止上传。"""

    CHUNK = 64 * 1024

    def __init__(self, path: Path, user: str, deadline):
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = path.name.replace("\\", "_").replace('"', "_").replace("\r", "").replace("\n", "")
        self.head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="user"\r\n\r\n{user}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        self.tail = f"\r\n--{boundary}--\r\n".encode()
        self.content_type = f"multipart/form-data; boundary={boundary}"
        self.size = path.stat().st_size
        self.length = len(self.head) + self.size + len(self.tail)
        self.deadline = deadline
        self.started = time.time()
        self.sent = 0
        self._last_log = self.started
        self._file = open(path, "rb")
        self._stage = 0

    def progress(self) -> str:
        elapsed = max(time.time() - self.started, 0.001)
        percent = self.sent * 100 // max(self.length, 1)
        return f"{percent}%（{mb(self.sent)}/{mb(self.length)}，{mb(self.sent / elapsed)}/s）"

    def read(self, _size: int = -1) -> bytes:
        now = time.time()
        if self.deadline is not None and now > self.deadline:
            raise UploadTimeout(self.progress())
        if now - self._last_log >= 10:
            log(f"    上传中 {self.progress()}")
            self._last_log = now
        if self._stage == 0:
            self._stage, chunk = 1, self.head
        elif self._stage == 1:
            chunk = self._file.read(self.CHUNK)
            if not chunk:
                self._stage, chunk = 2, self.tail
        elif self._stage == 2:
            self._stage, chunk = 3, b""
        else:
            chunk = b""
        self.sent += len(chunk)
        return chunk

    def close(self) -> None:
        self._file.close()


def upload(path: Path, user: str, config: dict, deadline) -> tuple[str, float]:
    body = UploadBody(path, user, deadline)
    # 发完数据后等服务端响应也算在上传时间里，socket 超时不超过剩余时间
    timeout = 120 if deadline is None else max(5, min(120, deadline - time.time()))
    try:
        with api_request(
            config, "POST", "/files/upload", body=body, content_type=body.content_type, timeout=timeout
        ) as response:
            file_id = json.loads(response.read())["id"]
    except DedupError as e:
        cause = e.__cause__
        reason = getattr(cause, "reason", cause)
        if deadline is not None and isinstance(reason, (socket.timeout, TimeoutError)):
            raise UploadTimeout(f"{body.progress()}，等服务端响应时超时") from e
        raise
    except (socket.timeout, TimeoutError) as e:
        if deadline is None:
            raise DedupError(f"上传超时：{e}") from e
        raise UploadTimeout(f"{body.progress()}，等服务端响应时超时") from e
    finally:
        body.close()
    return file_id, time.time() - body.started


def stream_reader(key: str, user: str, file_id: str, config: dict, events: queue.Queue) -> None:
    """后台线程：发起工作流并读取流式事件，把关键事件放进队列。断开连接不会中止服务端的工作流。"""
    payload = {
        "inputs": {INPUT_VARIABLE: {"type": "document", "transfer_method": "local_file", "upload_file_id": file_id}},
        "response_mode": "streaming",
        "user": user,
    }
    try:
        response = api_request(
            config, "POST", "/workflows/run", body=json.dumps(payload).encode(),
            content_type="application/json", timeout=STREAM_READ_TIMEOUT,
        )
    except HttpError as e:
        events.put((key, "http_error", (e.code, str(e))))
        return
    except DedupError as e:
        events.put((key, "conn_error", str(e)))
        return
    started = False
    try:
        with response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                kind = event.get("event")
                if not started:
                    run_id = event.get("workflow_run_id")
                    if not run_id and kind == "workflow_started":
                        run_id = (event.get("data") or {}).get("id")
                    if run_id:
                        started = True
                        events.put((key, "started", run_id))
                if kind == "workflow_finished":
                    events.put((key, "finished", event.get("data") or {}))
                    return
                if kind == "error":
                    events.put((key, "error", clean_error(event.get("message") or event)))
                    return
    except Exception:  # 断流、超时等，改为轮询
        pass
    events.put((key, "closed", None))


# ---------- 状态文件 ----------


def load_state(output: Path) -> dict:
    path = output / STATE_NAME
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("jobs"), dict):
                return state
        except (OSError, json.JSONDecodeError):
            log(f"状态文件损坏，重新开始：{path}")
    return {"version": 1, "jobs": {}}


def save_state(output: Path, state: dict) -> None:
    path = output / STATE_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def acquire_lock(output: Path) -> Path:
    lock = output / LOCK_NAME
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                info = json.loads(lock.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                info = {}
            if lock_alive(info):
                sys.exit(
                    f"上一次运行还没结束：报告目录 {output} 正被 pid {info.get('pid')} 使用。"
                    "请等它结束（最多约 4 分钟）、读取它的输出，再用同一命令运行；不要删除锁文件"
                )
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "time": time.time()}, f)
        return lock
    sys.exit(f"无法锁定报告目录：{lock}")


def lock_alive(info: dict) -> bool:
    pid, created = info.get("pid"), info.get("time")
    if not isinstance(pid, int) or not isinstance(created, (int, float)):
        return False
    if time.time() - created > 6 * 3600:
        return False
    if os.name == "nt":  # Windows 上 os.kill 会结束进程，只按时间判断
        return time.time() - created < 600
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------- 任务处理 ----------


def collect(source: Path, output: Path) -> list[tuple[Path, Path]]:
    if source.is_file():
        return [(source, output / f"{source.stem}.md")]
    return [
        (path, output / path.relative_to(source).with_suffix(".md"))
        for path in sorted(source.rglob("*"))
        if path.is_file() and path.suffix.lower() == ".docx" and not path.name.startswith(("~$", "._"))
    ]


def pick_report(outputs: dict) -> str:
    for field in REPORT_FIELDS:
        value = outputs.get(field)
        if value:
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if len(outputs) == 1:
        value = next(iter(outputs.values()))
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return json.dumps(outputs, ensure_ascii=False, indent=2)


class Runner:
    def __init__(self, args, config: dict, output: Path, state: dict):
        self.args = args
        self.config = config
        self.output = output
        self.state = state
        self.jobs: dict = state["jobs"]
        self.keys: list[str] = []
        self.events: queue.Queue = queue.Queue()
        self.streaming: dict[str, float] = {}  # 本次调用里还连着流的任务 -> 发起时间
        self.last_poll: dict[str, float] = {}
        self.budget = args.budget
        self.started = time.time()
        self.deadline = self.started + args.budget if args.budget > 0 else None

    # ----- 准备 -----

    def prepare(self, pairs: list[tuple[Path, Path]]) -> None:
        now = time.time()
        flags = {"overwrite": self.args.overwrite, "retry_failed": self.args.retry_failed}
        reset = self.state.get("reset")
        if not any(flags.values()):
            self.state.pop("reset", None)
            reset = None
        elif not (reset and reset.get("flags") == flags and now - reset.get("seen", 0) < RESET_REUSE):
            # 只在第一次带参数调用时记下时间点，之后重复调用不会把这批已经重跑完的任务再重置一遍
            reset = self.state["reset"] = {"at": now, "flags": flags}
        if reset:
            reset["seen"] = now

        for path, report_path in pairs:
            key = report_path.relative_to(self.output).as_posix()
            stat = path.stat()
            fingerprint = [stat.st_size, stat.st_mtime_ns]
            source = str(path.resolve())
            job = self.jobs.get(key)
            changed = False
            if job and (job.get("source") != source or job.get("fingerprint") != fingerprint):
                # 文档改过，旧报告已经过时，重新查重并覆盖
                log(f"文档已改动，重新查重：{path}")
                job, changed = None, True
            if job and job.get("status") in ("succeeded", "skipped") and not report_path.exists():
                job = None
            if job and reset and job.get("status") in DONE and job.get("finished_at", 0) < reset["at"]:
                if flags["overwrite"] or (flags["retry_failed"] and job["status"] == "failed"):
                    job = None
            if job is None:
                job = {
                    "source": source,
                    "fingerprint": fingerprint,
                    "status": "pending",
                    "user": f"{self.config['user']}-{uuid.uuid4().hex[:12]}",
                    "created_at": now,
                    "attempts": 0,
                }
                if stat.st_size == 0:
                    self.finish(job, "failed", error="文件为空")
                elif report_path.exists() and not self.args.overwrite and not changed:
                    self.finish(job, "skipped", error="报告已存在，加 --overwrite 覆盖")
            self.jobs[key] = job
            self.keys.append(key)
        self.save()

    # ----- 工具 -----

    def save(self) -> None:
        save_state(self.output, self.state)

    def report_path(self, key: str) -> Path:
        return self.output / key

    def name(self, key: str) -> str:
        return Path(self.jobs[key]["source"]).name

    def finish(self, job: dict, status: str, error: str | None = None) -> None:
        job["status"] = status
        job["finished_at"] = time.time()
        if error:
            job["error"] = error
        else:
            job.pop("error", None)

    def remaining(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - time.time()

    def unfinished(self) -> list[str]:
        return [k for k in self.keys if self.jobs[k]["status"] in UNFINISHED]

    def active(self) -> list[str]:
        return [k for k in self.keys if self.jobs[k]["status"] in ("starting", "running")]

    # ----- 主循环 -----

    def run(self) -> None:
        while True:
            self.drain_events()
            now = time.time()
            for key in self.active():
                if self.should_poll(key, now):
                    self.poll(key)
                    self.last_poll[key] = time.time()

            if len(self.active()) < self.args.parallel:
                key = self.next_to_start()
                if key:
                    self.submit(key)
                    continue

            if not self.unfinished():
                return
            if self.remaining() <= 0:
                return
            if not self.active():
                # 没有在跑的任务：剩下的要么这次调用时间不够上传，要么在等重试
                retry = [self.jobs[k].get("retry_at") for k in self.unfinished() if self.jobs[k].get("retry_at")]
                if not retry or (self.deadline is not None and min(retry) > self.deadline - 30):
                    return
            self.wait()

    def should_poll(self, key: str, now: float) -> bool:
        if key in self.streaming:
            # 连着流的任务不用查；没拿到 run_id 的，等一段时间后改查运行日志
            if self.jobs[key]["status"] == "running" or now - self.streaming[key] < START_TIMEOUT:
                return False
        return now - self.last_poll.get(key, 0) >= POLL_INTERVAL

    def wait(self) -> None:
        now = time.time()
        due = [self.last_poll.get(k, 0) + POLL_INTERVAL for k in self.active() if k not in self.streaming]
        due += [t + START_TIMEOUT for k, t in self.streaming.items() if self.jobs[k]["status"] == "starting"]
        due += [now + POLL_INTERVAL]
        for key in self.keys:
            retry_at = self.jobs[key].get("retry_at")
            if self.jobs[key]["status"] == "uploaded" and retry_at:
                due.append(retry_at)
        wake = min(due)
        if self.deadline is not None:
            wake = min(wake, self.deadline)
        try:
            item = self.events.get(timeout=max(0.5, wake - now))
        except queue.Empty:
            return
        self.handle_event(*item)

    def drain_events(self) -> None:
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                return
            self.handle_event(*item)

    # ----- 提交 -----

    def next_to_start(self) -> str | None:
        """优先发起已上传的文档；已上传未发起的也占并发名额，避免先把后面的文档都传上去。"""
        now = time.time()
        uploaded = [k for k in self.keys if self.jobs[k]["status"] == "uploaded"]
        if uploaded:
            candidates = uploaded
        elif len(self.active()) < self.args.parallel:
            candidates = [k for k in self.keys if self.jobs[k]["status"] == "pending"]
        else:
            candidates = []
        for key in candidates:
            job = self.jobs[key]
            if job.get("retry_at", 0) > now:
                continue
            if self.has_time_for(job):
                return key
        return None

    def start_window(self) -> float:
        """发起运行至少要留的时间（秒）。"""
        return min(30, self.budget * 0.5)

    def has_time_for(self, job: dict) -> bool:
        if self.deadline is None:
            return True
        remaining = self.remaining()
        if job["status"] == "uploaded":
            return remaining >= self.start_window()
        # 调用刚开始时总是允许上传，这样传不完时能给出明确的超时提示，而不是一直推迟
        full_window = max(self.budget - 15, self.budget * 0.5)
        speed = self.state.get("upload_speed")
        need = job["fingerprint"][0] / speed * 1.3 + 20 if speed else UPLOAD_MIN_WINDOW
        return remaining >= min(need, full_window)

    def submit(self, key: str) -> None:
        job = self.jobs[key]
        path = Path(job["source"])
        if job["status"] == "pending":
            full_window = self.remaining() >= max(self.budget - 15, self.budget * 0.5)
            log(f"上传：{path}（{mb(job['fingerprint'][0])}）")
            try:
                file_id, elapsed = upload(path, job["user"], self.config, self.deadline)
            except UploadTimeout as e:
                if full_window:
                    # 用满一次调用的时间都传不完，重试也一样，直接记为失败
                    self.finish(job, "failed", error=(
                        f"上传超时：{path.name}（{mb(job['fingerprint'][0])}）在 {self.budget} 秒内只传了 {e}，"
                        f"超过单次调用的时间限制。请压缩文档里的图片后再试，或在支持长时间命令的环境里加 --budget 0 运行"
                    ))
                    log(f"    失败  {job['error']}")
                else:
                    log(f"    本次剩余时间不够，已传 {e}，下次调用重新上传")
                self.save()
                return
            except HttpError as e:
                if e.code in (401, 403):
                    raise FatalError(f"查重服务拒绝访问（API 密钥错误或无权限）：{e}") from e
                self.finish(job, "failed", error=f"上传失败：{e}")
                log(f"    失败  {job['error']}")
                self.save()
                return
            except DedupError as e:
                raise FatalError(str(e)) from e
            except OSError as e:
                self.finish(job, "failed", error=f"读取文档失败：{e}")
                self.save()
                return
            job["file_id"] = file_id
            job["status"] = "uploaded"
            job["upload_seconds"] = round(elapsed, 1)
            if elapsed > 1:
                self.state["upload_speed"] = job["fingerprint"][0] / elapsed
            log(f"    上传完成，用时 {elapsed:.0f} 秒")
            self.save()
            if self.remaining() < self.start_window():
                return

        job["status"] = "starting"
        job["submitted_at"] = time.time()
        job["attempts"] = job.get("attempts", 0) + 1
        job.pop("retry_at", None)
        job.pop("run_id", None)
        self.save()  # 先记下 starting 再发请求，进程中途被杀也能凭 user 在运行日志里找回
        log(f"发起查重工作流：{path.name}")
        self.streaming[key] = time.time()
        threading.Thread(
            target=stream_reader, args=(key, job["user"], job["file_id"], self.config, self.events), daemon=True
        ).start()

    # ----- 事件 -----

    def handle_event(self, key: str, kind: str, payload) -> None:
        job = self.jobs.get(key)
        if job is None or job["status"] not in ("starting", "running"):
            return
        if kind == "started":
            job["run_id"] = payload
            job["status"] = "running"
            log(f"    工作流已开始运行：{self.name(key)}（run_id {payload}）")
        elif kind == "finished":
            self.streaming.pop(key, None)
            self.complete(key, payload)
        elif kind == "error":
            self.streaming.pop(key, None)
            self.finish(job, "failed", error=f"工作流报错：{payload}")
            log(f"    失败  {self.name(key)}：{job['error']}")
        elif kind == "http_error":
            self.streaming.pop(key, None)
            code, message = payload
            if code in (429, 500, 502, 503, 504) and job["attempts"] < MAX_START_ATTEMPTS:
                job["status"] = "uploaded"
                job["retry_at"] = time.time() + RETRY_DELAY
                job["error"] = message
                log(f"    发起失败，{RETRY_DELAY} 秒后重试：{message}")
            else:
                self.finish(job, "failed", error=f"发起工作流失败：{message}")
                log(f"    失败  {self.name(key)}：{job['error']}")
        elif kind in ("conn_error", "closed"):
            # 连接问题：工作流可能已在服务端运行，改为轮询运行状态或运行日志
            self.streaming.pop(key, None)
            self.last_poll[key] = 0
            if kind == "conn_error":
                log(f"    连接中断，改为定时查询：{payload}")
        self.save()

    # ----- 轮询 -----

    def poll(self, key: str) -> None:
        job = self.jobs[key]
        age = time.time() - job.get("submitted_at", time.time())
        if job["status"] == "starting":
            self.recover_run_id(key, age)
            return
        try:
            data = get_json(self.config, f"/workflows/run/{job['run_id']}")
        except HttpError as e:
            if e.code == 404 and age < NOT_FOUND_GRACE:
                return
            if e.code == 404:
                self.finish(job, "failed", error="查重服务里查不到这次运行记录，请到查重服务查看运行日志")
            elif e.code in (401, 403):
                raise FatalError(f"查重服务拒绝访问（API 密钥错误或无权限）：{e}") from e
            else:
                log(f"    查询运行状态失败，稍后再试：{e}")
                self.check_timeout(job, age)
            self.save()
            return
        except (DedupError, ValueError) as e:
            log(f"    查询运行状态失败，稍后再试：{e}")
            self.check_timeout(job, age)
            self.save()
            return
        if data.get("status") in ("running", "paused", None):
            log(f"    运行中：{self.name(key)}，已运行 {age / 60:.0f} 分钟")
            self.check_timeout(job, age)
            self.save()
            return
        self.complete(key, data)
        self.save()

    def recover_run_id(self, key: str, age: float) -> None:
        job = self.jobs[key]
        try:
            logs = get_json(self.config, "/workflows/logs", {"created_by_end_user_session_id": job["user"], "limit": 5})
        except (DedupError, ValueError) as e:
            log(f"    查询运行日志失败，稍后再试：{e}")
            return
        for item in logs.get("data") or []:
            run_id = (item.get("workflow_run") or {}).get("id")
            if run_id:
                job["run_id"] = run_id
                job["status"] = "running"
                self.last_poll[key] = 0
                log(f"    从运行日志找回任务：{self.name(key)}（run_id {run_id}）")
                self.save()
                return
        if age > LOST_GRACE:
            if job.get("attempts", 0) >= MAX_START_ATTEMPTS:
                self.finish(job, "failed", error="多次发起工作流都没有查到运行记录，请到查重服务查看运行日志")
            else:
                job["status"] = "uploaded"
                log(f"    {LOST_GRACE // 60} 分钟内没有查到运行记录，重新发起：{self.name(key)}")
            self.save()

    def check_timeout(self, job: dict, age: float) -> None:
        if age > MAX_RUN_SECONDS:
            self.finish(job, "failed", error=(
                f"工作流运行超过 {MAX_RUN_SECONDS // 3600} 小时仍未结束，查重未完成，请到查重服务查看运行日志"
            ))

    def complete(self, key: str, data: dict) -> None:
        job = self.jobs[key]
        status = data.get("status")
        outputs = data.get("outputs") or {}
        if status != "succeeded":
            error = f"工作流状态 {status}：{clean_error(data.get('error'))}"
            if status == "stopped":
                error += "（工作流被停止，可能是在查重服务里手动停止的）"
            self.finish(job, "failed", error=error)
        elif outputs.get("error_msg"):
            # 工作流在校验失败时走错误分支，状态仍是 succeeded，只输出 error_msg
            self.finish(job, "failed", error=f"工作流未完成查重：{outputs['error_msg']}")
        else:
            report = pick_report(outputs)
            if not report.strip():
                self.finish(job, "failed", error="工作流没有返回报告内容")
            else:
                path = self.report_path(key)
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(report, encoding="utf-8")
                    self.finish(job, "succeeded")
                except OSError as e:
                    self.finish(job, "failed", error=f"保存报告失败：{e}")
        log(f"    {job['status']}  {self.name(key)}  {job.get('error') or self.report_path(key)}")

    # ----- 结果 -----

    def results(self) -> list[dict]:
        rows = []
        now = time.time()
        for key in self.keys:
            job = self.jobs[key]
            status = job["status"]
            row = {"file": job["source"]}
            if status in ("succeeded", "skipped"):
                row.update(status=status, report=str(self.report_path(key)))
            elif status == "failed":
                row.update(status="failed")
            elif status in ("starting", "running"):
                minutes = (now - job.get("submitted_at", now)) / 60
                row.update(status="running", detail=f"工作流运行中，已运行 {minutes:.0f} 分钟")
            else:
                row.update(status="pending", detail="等待上传")
            if job.get("error") and status in ("failed", "skipped"):
                row["error"] = job["error"]
            rows.append(row)
        return rows


def summarize(rows: list[dict], fatal: str | None) -> tuple[dict, str, int]:
    count = {s: sum(r["status"] == s for r in rows) for s in ("succeeded", "failed", "skipped", "running", "pending")}
    unfinished = count["running"] + count["pending"]
    if fatal:
        message = f"查重服务不可用，已停止：{fatal}"
        code = 1
    elif unfinished:
        message = (
            f"未完成：还有 {unfinished} 篇在查重（运行中 {count['running']}，等待上传 {count['pending']}）。"
            "查重还没结束，不要汇报结论；请用完全相同的命令再运行一次，直到不再出现这条提示。"
        )
        code = 2
    else:
        message = f"全部完成：成功 {count['succeeded']}，失败 {count['failed']}，跳过 {count['skipped']}。"
        code = 0 if count["succeeded"] == len(rows) else 1
    return count, message, code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="docx 文档，或包含 docx 的文件夹（递归查找）")
    parser.add_argument("--output", type=Path, default=Path("dedup-reports"), help="报告目录，默认 ./dedup-reports")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help=f"单次调用最多运行的秒数，默认 {DEFAULT_BUDGET}；0 表示一直运行到全部完成")
    parser.add_argument("--parallel", type=int, default=1, help="同时运行的工作流数量，默认 1")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有报告，重新查重")
    parser.add_argument("--retry-failed", action="store_true", help="重新查重之前失败的文档")
    args = parser.parse_args()
    if args.budget < 0 or args.parallel < 1:
        sys.exit("--budget 不能小于 0，--parallel 不能小于 1")

    source: Path = args.source
    if not source.exists():
        sys.exit(f"路径不存在：{source}")
    if source.is_file() and source.suffix.lower() != ".docx":
        sys.exit(f"只支持 docx：{source}。doc、wps 等格式请先用 Word/WPS 另存为 docx")
    pairs = collect(source, args.output)
    if not pairs:
        sys.exit(f"没有找到 docx 文档：{source}")

    config = load_config()
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(output)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    fatal = None
    try:
        state = load_state(output)
        runner = Runner(args, config, output, state)
        runner.prepare(pairs)
        try:
            runner.run()
        except FatalError as e:
            fatal = str(e)
        runner.save()
        rows = runner.results()
    finally:
        lock.unlink(missing_ok=True)

    count, message, code = summarize(rows, fatal)
    print(json.dumps({"results": rows, "summary": count, "message": message}, ensure_ascii=False, indent=2))
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
