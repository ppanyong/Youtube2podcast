from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable
from contextvars import ContextVar
from typing import TypeVar


class TaskCancelled(Exception):
    """用户主动停掉正在执行的任务。"""


active_cancel: ContextVar[Cancellation | None] = ContextVar("active_cancel", default=None)
T = TypeVar("T")


class Cancellation:
    """停止标记。置位时同时杀掉正在跑的子进程和进行中的 HTTP 请求。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen] = []
        self._clients: list = []

    def set(self) -> None:
        self._event.set()
        self.abort()

    def clear(self) -> None:
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    def track_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.append(proc)
        if self.is_set():
            _kill(proc)

    def untrack_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs = [item for item in self._procs if item is not proc]

    def track_client(self, client) -> None:
        with self._lock:
            self._clients.append(client)
        if self.is_set():
            _close(client)

    def untrack_client(self, client) -> None:
        with self._lock:
            self._clients = [item for item in self._clients if item is not client]

    def abort(self) -> None:
        with self._lock:
            procs = list(self._procs)
            clients = list(self._clients)
        for proc in procs:
            _kill(proc)
        for client in clients:
            _close(client)


def run_process(args: list[str]) -> subprocess.CompletedProcess:
    """运行外部命令；若用户点了停止，马上结束进程。"""
    cancel = active_cancel.get()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    if cancel is not None and hasattr(cancel, "track_process"):
        cancel.track_process(proc)
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    _kill(proc)
                    proc.communicate()
                    raise TaskCancelled()
        if cancel is not None and cancel.is_set():
            raise TaskCancelled()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode or 1, args, output=out, stderr=err)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)
    finally:
        if cancel is not None and hasattr(cancel, "untrack_process"):
            cancel.untrack_process(proc)


def run_http(client, call: Callable[[], T]) -> T:
    """在旁路线程跑同步 HTTP；点停止时关掉客户端并立刻当作已取消。"""
    cancel = active_cancel.get()
    if cancel is not None and cancel.is_set():
        raise TaskCancelled()
    box: dict = {}

    def work() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - 要把取消和网络错误都交回主线程
            box["error"] = exc

    thread = threading.Thread(target=work, name="youtube2podcast-http", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(0.2)
        if cancel is not None and cancel.is_set():
            _close(client)
            raise TaskCancelled()
    if "error" in box:
        exc = box["error"]
        if cancel is not None and cancel.is_set():
            raise TaskCancelled() from exc
        raise exc
    return box["value"]


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        proc.kill()


def _close(client) -> None:
    try:
        client.close()
    except Exception:
        return
