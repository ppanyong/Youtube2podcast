import time
import threading

from youtube2podcast.cancel import Cancellation, TaskCancelled, active_cancel, run_http, run_process


def test_stop_kills_a_running_process():
    cancel = Cancellation()
    token = active_cancel.set(cancel)
    started = time.monotonic()
    try:
        def later():
            time.sleep(0.3)
            cancel.set()

        threading.Thread(target=later).start()
        try:
            run_process(["sleep", "30"])
        except TaskCancelled:
            elapsed = time.monotonic() - started
            assert elapsed < 3
        else:
            raise AssertionError("进程没有被停止")
    finally:
        active_cancel.reset(token)


class _SlowClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def post(self) -> str:
        time.sleep(30)
        return "ok"


def test_stop_aborts_in_flight_http():
    cancel = Cancellation()
    token = active_cancel.set(cancel)
    client = _SlowClient()
    started = time.monotonic()
    try:
        def later():
            time.sleep(0.3)
            cancel.set()

        threading.Thread(target=later).start()
        try:
            run_http(client, client.post)
        except TaskCancelled:
            elapsed = time.monotonic() - started
            assert elapsed < 3
            assert client.closed
        else:
            raise AssertionError("HTTP 请求没有被停止")
    finally:
        active_cancel.reset(token)
