from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from youtube2podcast.cancel import TaskCancelled, active_cancel, run_process
from youtube2podcast.subtitles import Cue, english_track_candidates, parse_vtt
from youtube2podcast.summarize import Chapter

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SUBTITLE_TRIES = 2


class DownloadError(RuntimeError):
    pass


@dataclass
class Media:
    video_id: str
    title: str
    channel: str
    duration: float
    url: str
    upload_date: str | None
    cues: list[Cue] | None
    chapters: list[Chapter]
    thumbnail_path: Path | None


class YtDlpDownloader:
    """只取元数据、字幕和封面，不下载整段视频。"""

    def probe(self, url: str, workdir: Path) -> Media:
        info = self._extract(
            url,
            workdir,
            {
                "skip_download": True,
                "writesubtitles": False,
                "writeautomaticsub": False,
                "writethumbnail": False,
            },
            download=False,
        )
        failures: list[str] = []
        for lang, kind in english_track_candidates(
            info.get("subtitles") or {},
            info.get("automatic_captions") or {},
        ):
            error = self._save_subtitle(url, workdir, lang, kind)
            if error is None:
                failures.clear()
                break
            failures.append(error)
        if failures:
            raise DownloadError("下载失败：" + failures[-1])
        if _thumbnail(workdir) is None:
            try:
                self._extract(
                    url,
                    workdir,
                    {
                        "skip_download": True,
                        "writesubtitles": False,
                        "writeautomaticsub": False,
                        "writethumbnail": True,
                    },
                )
            except DownloadError:
                pass
        return self._to_media(info, workdir, url)

    def _save_subtitle(self, url: str, workdir: Path, lang: str, kind: str) -> str | None:
        """成功返回 None。某一语言超时或写不出文件时，把原因交回去换下一条。"""
        last = f"字幕 {lang} 没有写到文件"
        for _attempt in range(_SUBTITLE_TRIES):
            try:
                self._extract(
                    url,
                    workdir,
                    {
                        "skip_download": True,
                        "writesubtitles": kind == "manual",
                        "writeautomaticsub": kind == "auto",
                        "subtitleslangs": [lang],
                        "subtitlesformat": "vtt",
                        "writethumbnail": True,
                    },
                )
            except DownloadError as exc:
                last = str(exc).removeprefix("下载失败：")
                continue
            path = _subtitle_file({}, workdir, lang)
            if path and path.exists() and path.stat().st_size > 0:
                return None
        return last

    def _extract(self, url: str, workdir: Path, extra: dict, *, download: bool = True) -> dict:
        workdir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--no-warnings",
            "--no-color",
            "--socket-timeout",
            "60",
            "--retries",
            "5",
            "-o",
            str(workdir / "%(id)s.%(ext)s"),
            "--print-json",
        ]
        if not download or extra.get("skip_download"):
            command.append("--skip-download")
        if extra.get("writesubtitles"):
            command.append("--write-subs")
        if extra.get("writeautomaticsub"):
            command.append("--write-auto-subs")
        langs = extra.get("subtitleslangs") or []
        if langs:
            command.extend(["--sub-langs", ",".join(langs), "--sub-format", extra.get("subtitlesformat") or "vtt"])
        if extra.get("writethumbnail"):
            command.append("--write-thumbnail")
        if extra.get("format"):
            command.extend(["-f", extra["format"]])
        if extra.get("merge_output_format"):
            command.extend(["--merge-output-format", extra["merge_output_format"]])
        command.append(url)
        try:
            completed = run_process(command)
        except TaskCancelled:
            raise
        except subprocess.CalledProcessError as exc:
            cancel = active_cancel.get()
            if cancel is not None and cancel.is_set():
                raise TaskCancelled() from exc
            err = (exc.stderr or b"").decode("utf-8", errors="replace")
            raise DownloadError(f"下载失败：{_plain_text(err) or _plain(exc)}") from exc
        info = _last_json(completed.stdout or b"")
        if not info:
            raise DownloadError("下载失败：没有取到视频信息")
        if info.get("entries"):
            info = next((item for item in info["entries"] if item), None)
        if not info:
            raise DownloadError("下载失败：没有取到视频信息")
        return info

    def _to_media(self, info: dict, workdir: Path, url: str) -> Media:
        cues = _loaded_cues(info, workdir)
        chapters = [
            Chapter(start=float(item.get("start_time") or 0), title=str(item.get("title") or "章节"))
            for item in (info.get("chapters") or [])
        ]
        return Media(
            video_id=str(info.get("id") or ""),
            title=str(info.get("title") or "未命名视频"),
            channel=str(info.get("channel") or info.get("uploader") or ""),
            duration=float(info.get("duration") or 0),
            url=str(info.get("webpage_url") or url),
            upload_date=info.get("upload_date"),
            cues=cues,
            chapters=chapters,
            thumbnail_path=_thumbnail(workdir),
        )


def _plain(exc: BaseException) -> str:
    text = _ANSI.sub("", str(exc))
    return " ".join(text.split())


def _plain_text(text: str) -> str:
    return " ".join(_ANSI.sub("", text).split())


def _last_json(raw: bytes) -> dict | None:
    for line in reversed(raw.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _loaded_cues(info: dict, workdir: Path) -> list[Cue] | None:
    for lang, _kind in english_track_candidates(info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        path = _subtitle_file(info, workdir, lang)
        if not path:
            continue
        parsed = parse_vtt(path.read_text(encoding="utf-8", errors="replace"))
        if parsed:
            return parsed
    return None


def _subtitle_file(info: dict, workdir: Path, lang: str) -> Path | None:
    requested = (info.get("requested_subtitles") or {}).get(lang) or {}
    filepath = requested.get("filepath")
    if filepath and Path(filepath).exists():
        return Path(filepath)
    suffix = f".{lang.lower()}.vtt"
    matches = sorted(path for path in workdir.glob("*.vtt") if path.name.lower().endswith(suffix))
    return matches[0] if matches else None


def _thumbnail(workdir: Path) -> Path | None:
    for path in sorted(workdir.iterdir()):
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return path
    return None
