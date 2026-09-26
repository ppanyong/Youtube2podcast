from __future__ import annotations

import logging
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from youtube2podcast.cancel import Cancellation
from youtube2podcast.config import SettingsError
from youtube2podcast.tasks import TaskStore
from youtube2podcast.voices import voice_choices

STATIC_DIR = Path(__file__).parent / "static"
STATIC_INDEX = STATIC_DIR / "index.html"
STATIC_SETTINGS = STATIC_DIR / "settings.html"
logger = logging.getLogger(__name__)


class JobRunner:
    """一次只跑一条任务，避免同时打满字幕和语音接口。"""

    def __init__(self, store: TaskStore, run_task) -> None:
        self.store = store
        self.run_task = run_task
        self.queue: queue.Queue[int] = queue.Queue()
        self._started = False
        self._lock = threading.Lock()
        self._cancel = Cancellation()
        self._current: int | None = None
        self._skip: set[int] = set()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for task in self.store.list_requeueable():
                if task["status"] == "running":
                    self.store.update(
                        task["id"],
                        status="queued",
                        stage="queued",
                        detail="服务重启，重新排队",
                        error=None,
                    )
                self.queue.put(task["id"])
            threading.Thread(target=self._loop, name="youtube2podcast", daemon=True).start()

    def submit(self, task_id: int) -> None:
        self.queue.put(task_id)

    def stop(self, task_id: int) -> None:
        task = self.store.get(task_id)
        if task["status"] not in {"running", "queued"}:
            raise RuntimeError("只有进行中或排队中的任务可以停止")
        with self._lock:
            current = self._current == task_id
            if current:
                self._cancel.set()
            else:
                self._skip.add(task_id)
        if current:
            if task["status"] == "running":
                self.store.update(task_id, detail="正在停止")
            return
        self.store.update(task_id, status="stopped", stage="stopped", detail="已停止", error=None)

    def _loop(self) -> None:
        while True:
            task_id = self.queue.get()
            try:
                with self._lock:
                    if task_id in self._skip:
                        self._skip.discard(task_id)
                        action = "skip"
                    else:
                        self._cancel.clear()
                        self._current = task_id
                        action = "run"
                if action == "skip":
                    current = self.store.get(task_id)
                    if current["status"] in {"queued", "running"}:
                        self.store.update(
                            task_id,
                            status="stopped",
                            stage="stopped",
                            detail="已停止",
                            error=None,
                        )
                    continue
                try:
                    self.run_task(task_id, self._cancel)
                except TypeError:
                    self.run_task(task_id)
            except Exception:
                logger.exception("任务 %s 未能写回结果", task_id)
            finally:
                try:
                    if self._cancel.is_set():
                        current = self.store.get(task_id)
                        if current["status"] in {"queued", "running"}:
                            self.store.update(
                                task_id,
                                status="stopped",
                                stage="stopped",
                                detail="已停止",
                                error=None,
                            )
                except Exception:
                    logger.exception("任务 %s 停止后未能写回结果", task_id)
                with self._lock:
                    if self._current == task_id:
                        self._current = None
                self.queue.task_done()


class CreateBody(BaseModel):
    urls: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    album: str = ""


class SettingsBody(BaseModel):
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    tts_base_url: str = ""
    tts_api_key: str = ""
    tts_model: str = ""
    tts_voice: str = ""
    tts_speed: float = 1.0
    output_dir: str = ""
    album: str = ""


def create_app(store: TaskStore, runner: JobRunner, defaults: dict | None = None, settings_store=None) -> FastAPI:
    fallback = {
        "output_dir": (defaults or {}).get("output_dir", ""),
        "album": (defaults or {}).get("album", ""),
    }

    def form_defaults() -> dict:
        if settings_store is None:
            return fallback
        current = settings_store.settings
        return {"output_dir": current.output_dir, "album": current.album}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner.start()
        yield

    app = FastAPI(title="Youtube2podcast", lifespan=lifespan)
    app.state.store = store
    app.state.runner = runner

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return STATIC_INDEX.read_text(encoding="utf-8")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page() -> str:
        return STATIC_SETTINGS.read_text(encoding="utf-8")

    @app.get("/api/dirs")
    def dirs(path: str = "") -> dict:
        return _list_dirs(path)

    @app.get("/api/voices")
    def voices() -> dict:
        if settings_store is None:
            return {"available": False, "voices": []}
        current = settings_store.settings
        custom: list[dict] = []
        available = "cosyvoice2" in current.tts_model.lower()
        if current.tts_api_key and current.tts_base_url:
            try:
                import httpx

                root = current.tts_base_url.rstrip("/")
                if root.endswith("/audio/speech"):
                    root = root[: -len("/audio/speech")]
                response = httpx.get(
                    f"{root}/audio/voice/list",
                    headers={"Authorization": f"Bearer {current.tts_api_key}"},
                    timeout=8,
                )
                if response.status_code == 200:
                    payload = response.json()
                    custom = list(payload.get("results") or [])
                    available = True
            except Exception:
                logger.info("音色列表没有取到，沿用已保存的音色")
        choices = voice_choices(current.tts_model, custom)
        return {"available": bool(choices) or available, "voices": choices}

    @app.get("/api/config")
    def config() -> dict:
        return form_defaults()

    @app.get("/api/settings")
    def get_settings() -> dict:
        if settings_store is None:
            raise HTTPException(status_code=404, detail="当前没有可改的配置")
        return settings_store.public()

    @app.put("/api/settings")
    def put_settings(body: SettingsBody) -> dict:
        if settings_store is None:
            raise HTTPException(status_code=404, detail="当前没有可改的配置")
        try:
            return settings_store.update(body.model_dump(exclude_unset=True))
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks")
    def list_tasks() -> dict:
        return {"tasks": [_public_task(task) for task in store.list_tasks()]}

    @app.get("/api/tasks/{task_id}/audio")
    def task_audio(task_id: int) -> FileResponse:
        try:
            task = store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        path = _task_audio(task)
        if path is None:
            raise HTTPException(status_code=404, detail="还没有可听的音频")
        return FileResponse(
            path,
            media_type="audio/mpeg",
            filename=path.name,
            content_disposition_type="inline",
        )

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> dict:
        try:
            return _public_task(store.get(task_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: int) -> dict:
        try:
            task = store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        if task["status"] not in {"failed", "stopped"}:
            raise HTTPException(status_code=400, detail="只有失败或已停止的任务可以继续")
        updated = store.update(
            task_id,
            status="queued",
            stage="queued",
            detail="从失败处继续",
            error=None,
        )
        runner.submit(task_id)
        return _public_task(updated)

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: int) -> dict:
        try:
            runner.stop(task_id)
            return _public_task(store.get(task_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int) -> dict:
        try:
            store.delete(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks")
    def create_tasks(body: CreateBody) -> dict:
        urls = [line.strip() for line in body.urls.splitlines() if line.strip()]
        invalid = [url for url in urls if not url.startswith(("http://", "https://"))]
        if not urls or invalid:
            raise HTTPException(status_code=400, detail="请每行填写一条以 http 开头的视频链接")
        created = []
        for url in urls:
            task = store.create(url, body.album.strip(), body.output_dir.strip())
            runner.submit(task["id"])
            created.append(task)
        return {"tasks": [_public_task(task) for task in created]}

    return app


def _public_task(task: dict) -> dict:
    visible = dict(task)
    visible.pop("checkpoint", None)
    visible["has_audio"] = task.get("status") not in {"queued", "running"} and _task_audio(task) is not None
    return visible


def _task_audio(task: dict) -> Path | None:
    folder = task.get("output_path") or ""
    if not folder:
        return None
    try:
        root = Path(folder).expanduser().resolve()
    except OSError:
        return None
    if not root.is_dir():
        return None
    files = []
    try:
        for path in root.glob("*.mp3"):
            if not path.is_file() or path.stat().st_size < 1024:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
            files.append(resolved)
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda item: item.stat().st_mtime)


def _list_dirs(path: str) -> dict:
    target = Path(path).expanduser() if path else Path.home()
    try:
        target = target.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail="打不开这个目录") from exc
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="这不是一个目录")
    names: list[str] = []
    try:
        for child in target.iterdir():
            if child.name.startswith(".") or not child.is_dir():
                continue
            names.append(child.name)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="没有权限查看这个目录") from exc
    parent = None if target.parent == target else str(target.parent)
    return {"path": str(target), "parent": parent, "dirs": sorted(names, key=str.lower)}
