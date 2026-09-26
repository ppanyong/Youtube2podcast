import threading
import time

from fastapi.testclient import TestClient

from youtube2podcast.server import JobRunner, create_app
from youtube2podcast.tasks import TaskStore
from youtube2podcast.urls import extract_video_id


def test_extract_video_id():
    assert extract_video_id("https://www.youtube.com/watch?v=abc_123") == "abc_123"
    assert extract_video_id("https://youtu.be/abc_123") == "abc_123"
    assert extract_video_id("https://www.youtube.com/shorts/abc_123") == "abc_123"
    assert extract_video_id("https://example.com/video") is None


def test_api_stores_each_task_and_shows_progress(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")

    def run_task(task_id):
        store.update(task_id, status="running", stage="fetching", detail="获取字幕")
        store.update(task_id, status="running", stage="tts", detail="转音频 1/2")
        store.update(task_id, status="done", stage="done", detail="完成", output_path=str(tmp_path / "done"))

    app = create_app(store, JobRunner(store, run_task), {"output_dir": "/listen", "album": "机器学习"})
    with TestClient(app) as client:
        defaults = client.get("/api/config")
        assert defaults.json()["album"] == "机器学习"
        denied = client.post("/api/tasks", json={"urls": "not-a-url", "output_dir": "/listen", "album": "机器学习"})
        assert denied.status_code == 400
        created = client.post(
            "/api/tasks",
            json={
                "urls": "https://www.youtube.com/watch?v=abc123\nhttps://youtu.be/def456",
                "output_dir": "/listen",
                "album": "机器学习",
            },
        )
        assert created.status_code == 200
        assert len(created.json()["tasks"]) == 2
        saved = None
        for _ in range(40):
            body = client.get("/api/tasks").json()["tasks"]
            if all(item["status"] == "done" for item in body):
                saved = body
                break
            time.sleep(0.05)
        assert saved is not None
        assert {item["stage"] for item in saved} == {"done"}
        page = client.get("/")
        assert page.status_code == 200
        assert "长视频" in page.text
        assert "转音频" in page.text


def test_retry_requeues_a_failed_task(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    started = []

    def run_task(task_id):
        started.append(task_id)
        store.update(task_id, status="running", stage="tts", detail="从转音频继续")

    app = create_app(store, JobRunner(store, run_task))
    with TestClient(app) as client:
        task = store.create("https://youtu.be/abc123", "专辑", "/listen")
        store.update(task["id"], status="failed", stage="failed", detail="失败", error="语音接口返回 404")
        done = store.create("https://youtu.be/done123", "专辑", "/listen")
        store.update(done["id"], status="done", stage="done", detail="完成", output_path="/tmp/out")
        blocked = client.post(f"/api/tasks/{done['id']}/retry")
        assert blocked.status_code == 400
        accepted = client.post(f"/api/tasks/{task['id']}/retry")
        assert accepted.status_code == 200
        assert accepted.json()["detail"] == "从失败处继续"
        for _ in range(40):
            if task["id"] in started:
                break
            time.sleep(0.05)
        assert task["id"] in started
        assert "继续" in client.get("/").text


def test_delete_task_and_browse_directories(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    app = create_app(store, JobRunner(store, lambda task_id: None))
    nested = tmp_path / "Library"
    nested.mkdir()
    with TestClient(app) as client:
        task = store.create("https://youtu.be/abc123", "专辑", str(tmp_path))
        store.update(task["id"], status="failed", stage="failed", detail="失败", error="x")
        listed = client.get("/api/dirs", params={"path": str(tmp_path)})
        assert listed.status_code == 200
        assert "Library" in listed.json()["dirs"]
        removed = client.delete(f"/api/tasks/{task['id']}")
        assert removed.status_code == 200
        assert client.get("/api/tasks").json()["tasks"] == []
        page = client.get("/settings")
        assert page.status_code == 200
        assert "人声" in page.text


def test_task_audio_can_be_previewed(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    folder = tmp_path / "episode"
    folder.mkdir()
    (folder / "注意力机制入门.mp3").write_bytes(b"ID3" + b"\x00" * 2000)
    (folder / "notes.txt").write_text("skip", encoding="utf-8")
    app = create_app(store, JobRunner(store, lambda task_id: None))
    with TestClient(app) as client:
        ready = store.create("https://youtu.be/abc123", "注意力", str(tmp_path))
        store.update(ready["id"], status="done", stage="done", detail="完成", output_path=str(folder), title="注意力机制入门")
        busy = store.create("https://youtu.be/def456", "注意力", str(tmp_path))
        store.update(busy["id"], status="running", stage="tts", detail="转音频", output_path=str(folder))
        missing = store.create("https://youtu.be/ghi789", "注意力", str(tmp_path))
        store.update(missing["id"], status="done", stage="done", detail="完成", output_path=str(tmp_path / "empty"))

        listed = {item["id"]: item for item in client.get("/api/tasks").json()["tasks"]}
        assert listed[ready["id"]]["has_audio"] is True
        assert listed[busy["id"]]["has_audio"] is False
        assert listed[missing["id"]]["has_audio"] is False

        audio = client.get(f"/api/tasks/{ready['id']}/audio")
        assert audio.status_code == 200
        assert audio.headers["content-type"].startswith("audio/mpeg")
        assert audio.content.startswith(b"ID3")
        ranged = client.get(f"/api/tasks/{ready['id']}/audio", headers={"Range": "bytes=0-9"})
        assert ranged.status_code == 206
        assert ranged.content == b"ID3" + b"\x00" * 7
        assert client.get(f"/api/tasks/{missing['id']}/audio").status_code == 404
        assert client.get("/api/tasks/999/audio").status_code == 404
        assert "预听" in client.get("/").text


def test_stop_marks_the_running_task(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    entered = threading.Event()

    def run_task(task_id, cancel=None):
        store.update(task_id, status="running", stage="tts", detail="转音频")
        entered.set()
        while not (cancel and cancel.is_set()):
            time.sleep(0.02)

    app = create_app(store, JobRunner(store, run_task))
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={"urls": "https://www.youtube.com/watch?v=abc123", "output_dir": str(tmp_path)},
        )
        task_id = created.json()["tasks"][0]["id"]
        assert entered.wait(2)
        stopped = client.post(f"/api/tasks/{task_id}/stop")
        assert stopped.status_code == 200
        saved = None
        for _ in range(40):
            rows = client.get("/api/tasks").json()["tasks"]
            saved = next(item for item in rows if item["id"] == task_id)
            if saved["status"] == "stopped":
                break
            time.sleep(0.05)
        assert saved["status"] == "stopped"
        assert "停止" in client.get("/").text
