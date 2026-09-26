import pytest

from youtube2podcast.tasks import TaskStore


def test_task_records_start_end_and_can_be_deleted(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("https://www.youtube.com/watch?v=abc", "机器学习", "/tmp/learn")
    assert task["status"] == "queued"
    assert task["started_at"] is None

    running = store.update(task["id"], status="running", stage="fetching", detail="获取字幕")
    started = running["started_at"]
    assert started
    again = store.update(task["id"], status="running", stage="tts", detail="转音频")
    assert again["started_at"] == started

    done = store.update(
        task["id"],
        status="done",
        stage="done",
        detail="完成",
        video_id="abc",
        output_path="/tmp/learn/out",
        error=None,
    )
    assert done["finished_at"]
    assert store.find_done("abc")["id"] == task["id"]
    store.delete(task["id"])
    assert store.list_tasks() == []

    busy = store.create("https://youtu.be/abc", "专辑", "/tmp")
    store.update(busy["id"], status="running", stage="tts", detail="转音频")
    with pytest.raises(RuntimeError):
        store.delete(busy["id"])
