from __future__ import annotations

import threading
from pathlib import Path

from youtube2podcast.config import Settings, SettingsStore
from youtube2podcast.download import YtDlpDownloader
from youtube2podcast.llm import build_complete
from youtube2podcast.pipeline import Pipeline
from youtube2podcast.summarize import LLMSummarizer
from youtube2podcast.tasks import TaskStore
from youtube2podcast.translate import LLMTranslator
from youtube2podcast.tts import HttpSpeaker


def build_pipeline(settings: Settings, store: TaskStore) -> Pipeline:
    complete = build_complete(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    speaker = HttpSpeaker(
        base_url=settings.tts_base_url,
        api_key=settings.tts_api_key,
        model=settings.tts_model,
        voice=settings.tts_voice,
        speed=settings.tts_speed,
    )
    return Pipeline(
        store,
        YtDlpDownloader(),
        LLMTranslator(complete),
        LLMSummarizer(complete),
        speaker,
    )


def build_runtime(settings: Settings) -> tuple[TaskStore, Pipeline]:
    store = TaskStore(settings.db_path)
    return store, build_pipeline(settings, store)


class AppRuntime:
    """保存配置后重建翻译和语音，下一条任务用新配置。"""

    def __init__(self, settings: Settings, env_path: Path) -> None:
        self.store = TaskStore(settings.db_path)
        self.settings_store = SettingsStore(settings, env_path)
        self._lock = threading.Lock()
        self._pipeline = build_pipeline(settings, self.store)
        self.settings_store.on_change = self._swap

    def _swap(self, settings: Settings) -> None:
        pipeline = build_pipeline(settings, self.store)
        with self._lock:
            self._pipeline = pipeline

    def run_task(self, task_id: int, cancel=None) -> None:
        with self._lock:
            pipeline = self._pipeline
        pipeline.run(task_id, cancel=cancel)
