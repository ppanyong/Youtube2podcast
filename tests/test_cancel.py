import time

from youtube2podcast.cancel import Cancellation, TaskCancelled, active_cancel, run_process


def test_stop_kills_a_running_process():
    cancel = Cancellation()
    token = active_cancel.set(cancel)
    started = time.monotonic()
    try:
        def later():
            time.sleep(0.3)
            cancel.set()

        import threading

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
