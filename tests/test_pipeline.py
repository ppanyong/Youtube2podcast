from pathlib import Path

from youtube2podcast.download import Media
from youtube2podcast.pipeline import Pipeline
from youtube2podcast.subtitles import Cue
from youtube2podcast.summarize import KeyPoint, Summary, Term
from youtube2podcast.tasks import TaskStore


class Downloader:
    def __init__(self, media: Media):
        self.media = media
        self.calls = []

    def probe(self, url, workdir):
        self.calls.append("probe")
        return self.media


class Translator:
    def translate(self, sentences, on_batch=None, on_partial=None, **kwargs):
        if on_batch:
            on_batch(1, 1)
        return [f"译：{text}" for text in sentences]


class Summarizer:
    def summarize(self, original_title, lines):
        return Summary(
            title="注意力机制入门",
            intro="先听导语。",
            points=[KeyPoint(1.0, "为什么需要注意力")],
            terms=[Term("attention", "注意力")],
        )


class Speaker:
    def speak(self, sentences, dest, on_chunk=None, **kwargs):
        if on_chunk:
            on_chunk(1, 1)
        dest.write_bytes(b"mp3")


def _pipeline(store, media, **kwargs):
    downloader = kwargs.pop("downloader", None) or Downloader(media)
    return Pipeline(
        store,
        downloader,
        Translator(),
        Summarizer(),
        Speaker(),
        tag=lambda *args, **kw: None,
        **kwargs,
    ), downloader


def _media(**kwargs):
    data = dict(
        video_id="abc123",
        title="Attention",
        channel="Chan",
        duration=90,
        url="https://www.youtube.com/watch?v=abc123",
        upload_date="20260925",
        cues=[Cue("Hello there.", 0, 2)],
        chapters=[],
        thumbnail_path=None,
    )
    data.update(kwargs)
    return Media(**data)


def test_pipeline_stops_before_downloading(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("https://www.youtube.com/watch?v=abc123", "机器学习", str(tmp_path))
    pipeline, downloader = _pipeline(store, _media())
    cancel = __import__("threading").Event()
    cancel.set()
    pipeline.run(task["id"], cancel=cancel)
    assert store.get(task["id"])["status"] == "stopped"
    assert downloader.calls == []


def test_pipeline_skips_when_there_is_no_english_subtitle(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("https://youtu.be/abc123", "机器学习", str(tmp_path / "out"))
    pipeline, downloader = _pipeline(store, _media(cues=None))
    pipeline.run(task["id"])
    saved = store.get(task["id"])
    assert saved["status"] == "skipped"
    assert saved["detail"] == "没有可用的英文字幕"
    assert downloader.calls == ["probe"]


def test_pipeline_skips_video_that_already_succeeded(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    done_dir = tmp_path / "already"
    done_dir.mkdir()
    previous = store.create("https://youtu.be/abc123", "机器学习", str(tmp_path))
    store.update(previous["id"], status="done", stage="done", video_id="abc123", output_path=str(done_dir), detail="完成")
    task = store.create("https://www.youtube.com/watch?v=abc123", "机器学习", str(tmp_path / "out"))
    pipeline, downloader = _pipeline(store, _media())
    pipeline.run(task["id"])
    saved = store.get(task["id"])
    assert saved["status"] == "skipped"
    assert saved["detail"] == "该视频已经成功产出"
    assert downloader.calls == []


def test_pipeline_writes_audio_and_note(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("https://www.youtube.com/watch?v=abc123", "机器学习", str(tmp_path / "learn"))
    pipeline, downloader = _pipeline(store, _media())
    pipeline.run(task["id"])
    saved = store.get(task["id"])
    assert saved["status"] == "done"
    folder = Path(saved["output_path"])
    assert folder.name.startswith("2026-09-25")
    assert (folder / "summary.md").read_text(encoding="utf-8").startswith("# 注意力机制入门")
    assert (folder / "注意力机制入门.mp3").exists()
    assert not (folder / "shots").exists()
    assert downloader.calls == ["probe"]
    assert any(event["stage"] == "tts" for event in saved["events"])


def test_pipeline_records_failure_and_keeps_the_queue_usable(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")

    class Boom:
        def translate(self, sentences, on_batch=None, on_partial=None, **kwargs):
            raise RuntimeError("翻译服务不可用")

    task = store.create("https://youtu.be/zzz999", "机器学习", str(tmp_path / "out"))
    pipeline = Pipeline(store, Downloader(_media(video_id="zzz999")), Boom(), Summarizer(), Speaker(), tag=lambda *a, **k: None)
    pipeline.run(task["id"])
    saved = store.get(task["id"])
    assert saved["status"] == "failed"
    assert "翻译服务不可用" in saved["error"]


def test_failed_audio_step_continues_without_translating_again(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    media = _media()
    downloader = Downloader(media)
    translations = {"count": 0}

    class CountingTranslator:
        def translate(self, sentences, on_batch=None, on_partial=None, **kwargs):
            translations["count"] += 1
            return [f"译：{text}" for text in sentences]

    class FlakySpeaker:
        def __init__(self):
            self.calls = 0

        def speak(self, sentences, dest, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("语音接口返回 404：Not Found")
            dest.write_bytes(b"m" * 2048)

    speaker = FlakySpeaker()
    pipeline = Pipeline(
        store,
        downloader,
        CountingTranslator(),
        Summarizer(),
        speaker,
        tag=lambda *args, **kwargs: None,
    )
    task = store.create("https://www.youtube.com/watch?v=abc123", "机器学习", str(tmp_path / "learn"))
    pipeline.run(task["id"])
    failed = store.get(task["id"])
    assert failed["status"] == "failed"
    assert failed["checkpoint"]["resume_stage"] == "tts"
    pipeline.run(task["id"])
    saved = store.get(task["id"])
    assert saved["status"] == "done"
    assert translations["count"] == 1
    assert downloader.calls.count("probe") == 1
    assert speaker.calls == 2


def test_blank_album_uses_the_video_title(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("https://www.youtube.com/watch?v=abc123", "", str(tmp_path / "learn"))
    pipeline, _downloader = _pipeline(store, _media())
    pipeline.run(task["id"])
    saved = store.get(task["id"])
    assert saved["album"] == "Attention"
    assert "Attention" in saved["output_path"]
