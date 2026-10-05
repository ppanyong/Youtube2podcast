from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

from youtube2podcast.download import Media
from youtube2podcast.names import safe_filename
from youtube2podcast.subtitles import collapse_echo, merge_cues
from youtube2podcast.translate import polish_spoken, polish_translated
from youtube2podcast.speakers import (
    Spoken,
    Turn,
    apply_voices,
    label_speakers_by_text,
    map_speaker_voices,
    turns_from_sentences,
)
from youtube2podcast.summarize import Chapter, KeyPoint, Summary, Term, render_markdown
from youtube2podcast.tags import write_tags
from youtube2podcast.tasks import TaskStore
from youtube2podcast.urls import extract_video_id
from youtube2podcast.cancel import TaskCancelled, active_cancel

TagFn = Callable[..., None]
_MP3_READY_BYTES = 1024


class Pipeline:
    def __init__(
        self,
        store: TaskStore,
        downloader,
        translator,
        summarizer,
        speaker,
        *,
        tag: TagFn = write_tags,
        complete: Callable[[str, str], str] | None = None,
        tts_provider: str = "siliconflow",
    ) -> None:
        self.store = store
        self.downloader = downloader
        self.translator = translator
        self.summarizer = summarizer
        self.speaker = speaker
        self.tag = tag
        self.complete = complete
        self.tts_provider = tts_provider or "siliconflow"
        self._cancel = None

    def _check(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise TaskCancelled()

    def run(self, task_id: int, cancel=None) -> None:
        self._cancel = cancel
        token = active_cancel.set(cancel)
        try:
            self._run(task_id)
        except TaskCancelled:
            self.store.update(
                task_id,
                status="stopped",
                stage="stopped",
                detail="已停止",
                error=None,
            )
        except Exception as exc:
            self.store.update(
                task_id,
                status="failed",
                stage="failed",
                detail="失败",
                error=str(exc),
            )
        finally:
            active_cancel.reset(token)

    def _run(self, task_id: int) -> None:
        self._check()
        task = self.store.get(task_id)
        checkpoint = task.get("checkpoint") or {}
        if checkpoint.get("resume_stage") == "tts" and checkpoint.get("summary") and checkpoint.get("translated"):
            self.store.update(task_id, status="running", stage="tts", detail="从转音频继续", error=None)
            self._publish_saved(task, checkpoint)
            return
        if checkpoint.get("resume_stage") == "summarizing" and checkpoint.get("lines"):
            self._resume_summary(task, checkpoint)
            return
        if checkpoint.get("resume_stage") == "translating" and checkpoint.get("source_sentences"):
            self._resume_translation(task, checkpoint)
            return

        known = extract_video_id(task["url"])
        if known and self._already_done(known, task_id):
            self._skip(task_id, "该视频已经成功产出", video_id=known)
            return

        self._check()
        self.store.update(task_id, status="running", stage="fetching", detail="获取字幕", error=None)
        with tempfile.TemporaryDirectory(prefix="youtube2podcast-") as tmp:
            work = Path(tmp)
            self._check()
            media = self.downloader.probe(task["url"], work)
            self.store.update(
                task_id,
                video_id=media.video_id,
                title=media.title,
                detail="获取字幕",
            )
            if self._already_done(media.video_id, task_id):
                self._skip(task_id, "该视频已经成功产出", video_id=media.video_id, title=media.title)
                return
            sentences = merge_cues(media.cues or [])
            if not sentences:
                self._skip(task_id, "没有可用的英文字幕", video_id=media.video_id, title=media.title)
                return
            sentences = self._label_speakers(task_id, media, sentences)
            task = self._named_album(task, media.title)
            self.store.update(task_id, stage="translating", detail="翻译")
            translated = self._translate(task_id, media, sentences)
            self._finish_translation(task, media, sentences, translated, work)

    def _resume_translation(self, task: dict, checkpoint: dict) -> None:
        task = self._named_album(task, checkpoint.get("original_title") or "")
        task_id = task["id"]
        sources = checkpoint.get("source_sentences") or []
        done = list(checkpoint.get("translated_partial") or [])
        if len(done) > len(sources):
            done = done[: len(sources)]
        self.store.update(task_id, status="running", stage="translating", detail="从翻译继续", error=None)
        media = _media_from_checkpoint(checkpoint)
        sentences = [
            _StoredSentence(float(item.get("start") or 0), str(item.get("text") or ""), item.get("speaker"))
            for item in sources
        ]
        remaining = [item.text for item in sentences[len(done) :]]
        previous = ""
        for text in reversed(done):
            if text and text != "[重复]":
                previous = text
                break

        def on_partial(piece: list[str]) -> None:
            merged = done + piece
            self._store_translation(task_id, media, sentences, merged, detail=f"翻译 {len(merged)}/{len(sentences)}")

        more = self.translator.translate(
            remaining,
            on_batch=self._batch_progress(task_id, len(done), len(sentences)),
            on_partial=on_partial,
            previous=previous,
            speakers=[getattr(item, "speaker", None) for item in sentences[len(done) :]],
            previous_speaker=getattr(sentences[len(done) - 1], "speaker", None) if done else None,
            with_speakers=True,
        )
        more_texts, more_speakers = _unpack_translation(more)
        translated = done + more_texts
        if len(translated) != len(sentences):
            raise RuntimeError("翻译结果数量与原文不一致")
        self._apply_translated_speakers(media, sentences, ([None] * len(done)) + more_speakers)
        with tempfile.TemporaryDirectory(prefix="youtube2podcast-") as tmp:
            self._finish_translation(task, media, sentences, translated, Path(tmp))

    def _translate(self, task_id: int, media: Media, sentences) -> list[str]:
        def on_partial(piece: list[str]) -> None:
            self._store_translation(task_id, media, sentences, piece, detail=f"翻译 {len(piece)}/{len(sentences)}")

        translated, speakers = _unpack_translation(
            self.translator.translate(
                [item.text for item in sentences],
                on_batch=self._batch_progress(task_id, 0, len(sentences)),
                on_partial=on_partial,
                speakers=[getattr(item, "speaker", None) for item in sentences],
                with_speakers=True,
            )
        )
        if len(translated) != len(sentences):
            raise RuntimeError("翻译结果数量与原文不一致")
        self._apply_translated_speakers(media, sentences, speakers)
        return translated

    def _apply_translated_speakers(self, media: Media, sentences, speakers: list[str | None]) -> None:
        """把翻译阶段标出的说话人写回字幕，供后面绑音色。"""
        if not speakers:
            return
        changed = False
        for item, letter in zip(sentences, speakers):
            if not letter:
                continue
            if getattr(item, "speaker", None) != letter:
                changed = True
            item.speaker = letter
        if not any(getattr(item, "speaker", None) for item in sentences):
            return
        media.turns = turns_from_sentences(sentences)
        if changed or not getattr(media, "speaker_genders", None):
            names = []
            for item in sentences:
                speaker = getattr(item, "speaker", None)
                if speaker and speaker not in names:
                    names.append(speaker)
            if len(names) >= 2 and not getattr(media, "speaker_genders", None):
                media.speaker_genders = {name: ("female" if index % 2 else "male") for index, name in enumerate(names)}

    def _batch_progress(self, task_id: int, done: int, total_sentences: int):
        batch_size = max(1, getattr(self.translator, "batch_size", 12))
        finished = done // batch_size
        total = max(1, (total_sentences + batch_size - 1) // batch_size)

        def on_batch(index: int, _batch_total: int) -> None:
            self._check()
            current = min(total, finished + index)
            self.store.update(task_id, stage="translating", detail=f"翻译 {current}/{total}")

        return on_batch

    def _store_translation(self, task_id: int, media: Media, sentences, partial: list[str], *, detail: str) -> None:
        checkpoint = _checkpoint(media, [], [], None, None, getattr(media, "thumbnail_path", None))
        checkpoint["resume_stage"] = "translating"
        checkpoint["source_sentences"] = [
            {"start": item.start, "text": item.text, "speaker": getattr(item, "speaker", None)} for item in sentences
        ]
        checkpoint["translated_partial"] = partial
        self.store.update(task_id, checkpoint=checkpoint, stage="translating", detail=detail)

    def _finish_translation(self, task: dict, media: Media, sentences, translated: list[str], work: Path) -> None:
        task_id = task["id"]
        spoken_rows = [
            Spoken(start=item.start, text=text, speaker=getattr(item, "speaker", None))
            for item, text in zip(sentences, translated)
        ]
        polished = polish_spoken(spoken_rows)
        if not polished:
            raise RuntimeError("去重后没有可朗读的译文")
        voices = map_speaker_voices(
            polished,
            media.turns if getattr(media, "turns", None) else [],
            model=getattr(self.speaker, "model", "") or "",
            default_voice=getattr(self.speaker, "voice", "") or "",
            provider=self.tts_provider,
            genders=getattr(media, "speaker_genders", None) or {},
        )
        polished = apply_voices(polished, voices, getattr(self.speaker, "voice", "") or "")
        lines = [(item.start, item.text) for item in polished]
        spoken = [item.text for item in polished]
        checkpoint = _checkpoint(media, lines, spoken, summary=None, folder=None, thumbnail=getattr(media, "thumbnail_path", None))
        checkpoint["resume_stage"] = "summarizing"
        checkpoint["spoken"] = [
            {"start": item.start, "text": item.text, "speaker": item.speaker, "voice": item.voice} for item in polished
        ]
        self.store.update(task_id, checkpoint=checkpoint, stage="summarizing", detail="写小结")
        summary = self.summarizer.summarize(media.title, lines)
        self._publish(task, media, summary, lines, spoken, work, spoken_rows=polished)

    def _label_speakers(self, task_id: int, media: Media, sentences):
        if self.complete is None:
            self.store.update(task_id, stage="speakers", detail="未配置大模型，跳过文本分人")
            return sentences
        self._check()
        self.store.update(task_id, stage="speakers", detail="从文稿区分说话人")
        try:
            labeled, genders, turns = label_speakers_by_text(sentences, self.complete)
        except TaskCancelled:
            raise
        except Exception as exc:
            self.store.update(task_id, stage="speakers", detail=f"文本分人失败，沿用单音色：{exc}")
            return sentences
        media.turns = turns
        media.speaker_genders = genders
        count = len({item.speaker for item in labeled if item.speaker})
        if count < 2:
            self.store.update(task_id, stage="speakers", detail="文稿像一个人在讲，沿用原来的音色")
            return sentences
        self.store.update(task_id, stage="speakers", detail=f"从文稿区分出 {count} 个说话人")
        return labeled

    def _resume_summary(self, task: dict, checkpoint: dict) -> None:
        task = self._named_album(task, checkpoint.get("original_title") or "")
        task_id = task["id"]
        self.store.update(task_id, status="running", stage="summarizing", detail="从写小结继续", error=None)
        lines = polish_translated([(float(item["start"]), item["text"]) for item in checkpoint["lines"]])
        if not lines:
            lines = [
                (0.0, collapse_echo(text))
                for text in checkpoint.get("translated") or []
                if collapse_echo(text)
            ]
        translated = [text for _start, text in lines]
        summary = self.summarizer.summarize(checkpoint["original_title"], lines)
        media = _media_from_checkpoint(checkpoint)
        spoken_rows = _spoken_from(checkpoint)
        with tempfile.TemporaryDirectory(prefix="youtube2podcast-") as tmp:
            self._publish(task, media, summary, lines, translated, Path(tmp), spoken_rows=spoken_rows)

    def _publish_saved(self, task: dict, checkpoint: dict) -> None:
        task = self._named_album(task, checkpoint.get("original_title") or "")
        media = _media_from_checkpoint(checkpoint)
        summary = _summary_from(checkpoint["summary"])
        lines = polish_translated([(float(item["start"]), item["text"]) for item in checkpoint["lines"]])
        if not lines:
            lines = [
                (0.0, collapse_echo(text))
                for text in checkpoint.get("translated") or []
                if collapse_echo(text)
            ]
        translated = [text for _start, text in lines]
        folder = Path(checkpoint["folder"]) if checkpoint.get("folder") else None
        spoken_rows = _spoken_from(checkpoint)
        with tempfile.TemporaryDirectory(prefix="youtube2podcast-") as tmp:
            self._publish(
                task,
                media,
                summary,
                lines,
                translated,
                Path(tmp),
                folder=folder,
                spoken_rows=spoken_rows,
            )

    def _publish(
        self,
        task: dict,
        media: Media,
        summary: Summary,
        lines,
        translated: list[str],
        work: Path,
        *,
        folder: Path | None = None,
        spoken_rows: list[Spoken] | None = None,
    ) -> None:
        task_id = task["id"]
        title = safe_filename(summary.title)
        if folder is None:
            folder = self._folder(task, media, title)
            folder.mkdir(parents=True, exist_ok=False)
        else:
            folder.mkdir(parents=True, exist_ok=True)
        markdown = render_markdown(
            summary,
            original_title=media.title,
            url=media.url,
            duration=media.duration,
        )
        (folder / "summary.md").write_text(markdown, encoding="utf-8")
        thumbnail = _copy_cover(media.thumbnail_path, folder)
        checkpoint = _checkpoint(media, lines, translated, summary, folder, thumbnail)
        checkpoint["resume_stage"] = "tts"
        if spoken_rows:
            checkpoint["spoken"] = [
                {"start": item.start, "text": item.text, "speaker": item.speaker, "voice": item.voice}
                for item in spoken_rows
            ]
        self.store.update(
            task_id,
            title=summary.title,
            stage="tts",
            detail="转音频",
            output_path=str(folder),
            checkpoint=checkpoint,
        )

        mp3 = folder / f"{title}.mp3"

        def on_chunk(index: int, total: int) -> None:
            self._check()
            self.store.update(task_id, stage="tts", detail=f"转音频 {index}/{total}")

        if mp3.exists() and mp3.stat().st_size >= _MP3_READY_BYTES:
            self.store.update(task_id, stage="tts", detail="沿用已有音频")
        else:
            self._check()
            self.speaker.speak(
                translated,
                mp3,
                on_chunk=on_chunk,
                should_stop=self._check,
                voices=[item.voice or "" for item in spoken_rows] if spoken_rows else None,
            )

        self._check()
        self.store.update(task_id, stage="tagging", detail="写入专辑")
        self.tag(
            mp3,
            title=summary.title,
            album=task["album"],
            artist=media.channel,
            cover_path=thumbnail,
        )
        meta = {
            "video_id": media.video_id,
            "url": media.url,
            "original_title": media.title,
            "title": summary.title,
            "channel": media.channel,
            "album": task["album"],
            "duration": media.duration,
            "status": "done",
            "sentences": [
                {
                    "start": start,
                    "text": text,
                    **(
                        {"speaker": spoken_rows[index].speaker, "voice": spoken_rows[index].voice}
                        if spoken_rows and index < len(spoken_rows)
                        else {}
                    ),
                }
                for index, (start, text) in enumerate(lines)
            ],
        }
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.update(
            task_id,
            status="done",
            stage="done",
            detail="完成",
            output_path=str(folder),
            error=None,
            title=summary.title,
        )

    def _named_album(self, task: dict, video_title: str) -> dict:
        if (task.get("album") or "").strip():
            return task
        album = (video_title or "").strip() or "未命名视频"
        self.store.update(task["id"], album=album)
        return {**task, "album": album}

    def _folder(self, task: dict, media: Media, title: str) -> Path:
        day = _upload_day(media.upload_date)
        root = Path(task["output_dir"]).expanduser() / safe_filename(task["album"])
        folder = root / f"{day} {title}"
        if folder.exists():
            folder = root / f"{day} {title} {media.video_id}"
        base = folder
        suffix = 2
        while folder.exists():
            folder = Path(f"{base}-{suffix}")
            suffix += 1
        return folder

    def _already_done(self, video_id: str, task_id: int) -> bool:
        if not video_id:
            return False
        previous = self.store.find_done(video_id)
        if not previous or previous["id"] == task_id:
            return False
        path = previous.get("output_path") or ""
        return bool(path and Path(path).exists())

    def _skip(self, task_id: int, reason: str, *, video_id: str | None = None, title: str | None = None) -> None:
        fields = {
            "status": "skipped",
            "stage": "skipped",
            "detail": reason,
            "error": None,
        }
        if video_id:
            fields["video_id"] = video_id
        if title:
            fields["title"] = title
        self.store.update(task_id, **fields)


class _StoredSentence:
    def __init__(self, start: float, text: str, speaker=None) -> None:
        self.start = start
        self.text = text
        self.speaker = speaker


def _unpack_translation(result) -> tuple[list[str], list[str | None]]:
    if isinstance(result, tuple) and len(result) == 2:
        texts, speakers = result
        return list(texts), list(speakers)
    return list(result), []


def _upload_day(raw: str | None) -> str:
    if raw and len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return date.today().isoformat()


def _copy_cover(source: Path | None, folder: Path) -> Path | None:
    if source is None or not source.exists():
        return None
    dest = folder / f"cover{source.suffix.lower() or '.jpg'}"
    if source.resolve() != dest.resolve():
        shutil.copyfile(source, dest)
    return dest


def _checkpoint(
    media: Media,
    lines: list[tuple[float, str]],
    translated: list[str],
    summary: Summary | None,
    folder: Path | None,
    thumbnail: Path | None,
) -> dict:
    return {
        "video_id": media.video_id,
        "original_title": media.title,
        "channel": media.channel,
        "duration": media.duration,
        "url": media.url,
        "upload_date": media.upload_date,
        "chapters": [{"start": chapter.start, "title": chapter.title} for chapter in media.chapters],
        "lines": [{"start": start, "text": text} for start, text in lines],
        "translated": translated,
        "summary": _summary_payload(summary) if summary else None,
        "folder": str(folder) if folder else None,
        "thumbnail": str(thumbnail) if thumbnail else None,
        "turns": [
            {"start": turn.start, "end": turn.end, "speaker": turn.speaker}
            for turn in getattr(media, "turns", None) or []
        ],
        "speaker_genders": dict(getattr(media, "speaker_genders", None) or {}),
    }


def _spoken_from(checkpoint: dict) -> list[Spoken] | None:
    rows = checkpoint.get("spoken")
    if not rows:
        return None
    return [
        Spoken(
            start=float(item.get("start") or 0),
            text=str(item.get("text") or ""),
            speaker=item.get("speaker"),
            voice=item.get("voice"),
        )
        for item in rows
        if item.get("text")
    ]


def _summary_payload(summary: Summary) -> dict:
    return {
        "title": summary.title,
        "intro": summary.intro,
        "points": [{"timestamp": point.timestamp, "text": point.text} for point in summary.points],
        "terms": [{"source": term.source, "zh": term.zh} for term in summary.terms],
    }


def _summary_from(data: dict) -> Summary:
    return Summary(
        title=data["title"],
        intro=data.get("intro") or "",
        points=[KeyPoint(float(point["timestamp"]), point["text"]) for point in data.get("points") or []],
        terms=[Term(item["source"], item["zh"]) for item in data.get("terms") or []],
    )


def _media_from_checkpoint(checkpoint: dict) -> Media:
    thumbnail = checkpoint.get("thumbnail")
    media = Media(
        video_id=checkpoint.get("video_id") or "",
        title=checkpoint.get("original_title") or "",
        channel=checkpoint.get("channel") or "",
        duration=float(checkpoint.get("duration") or 0),
        url=checkpoint.get("url") or "",
        upload_date=checkpoint.get("upload_date"),
        cues=None,
        chapters=[
            Chapter(start=float(item.get("start") or 0), title=str(item.get("title") or "章节"))
            for item in checkpoint.get("chapters") or []
        ],
        thumbnail_path=Path(thumbnail) if thumbnail else None,
    )
    media.turns = [
        Turn(
            start=float(item.get("start") or 0),
            end=float(item.get("end") or 0),
            speaker=str(item.get("speaker") or "A"),
        )
        for item in checkpoint.get("turns") or []
    ]
    genders = checkpoint.get("speaker_genders") or {}
    media.speaker_genders = {str(key): str(value) for key, value in genders.items()}
    return media
