from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx

from youtube2podcast.cancel import TaskCancelled, active_cancel, run_http, run_process
from youtube2podcast.voices import fallback_voice

try:
    import edge_tts
except ImportError:  # pragma: no cover - 依赖装好后就不会走到
    edge_tts = None


class SpeechError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def chunk_for_speech(sentences: list[str], max_chars: int = 300) -> list[str]:
    """把译文切成适合单次合成的段落，跳过空句和相邻复读。"""
    from youtube2podcast.subtitles import collapse_echo, is_near_duplicate
    from youtube2podcast.translate import is_skip_marker

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    last = ""
    for sentence in sentences:
        text = collapse_echo((sentence or "").strip())
        if not text or is_skip_marker(text):
            continue
        if last and is_near_duplicate(last, text):
            continue
        if buf and size + len(text) > max_chars:
            chunks.append("".join(buf))
            buf = []
            size = 0
        buf.append(text if text.endswith(("。", "！", "？", ".", "!", "?")) else text + "。")
        size += len(text)
        last = text
    if buf:
        chunks.append("".join(buf))
    return chunks


def chunk_spoken(items: list[tuple[str, str]], max_chars: int = 300) -> list[tuple[str, str]]:
    """同一说话人的句子才拼进一段，换人就切开。"""
    from youtube2podcast.subtitles import collapse_echo, is_near_duplicate
    from youtube2podcast.translate import is_skip_marker

    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    voice = ""
    size = 0
    last = ""

    def flush() -> None:
        nonlocal buf, size, last
        if buf:
            chunks.append(("".join(buf), voice))
        buf = []
        size = 0

    for sentence, current in items:
        text = collapse_echo((sentence or "").strip())
        if not text or is_skip_marker(text):
            continue
        if last and is_near_duplicate(last, text):
            continue
        if buf and (current != voice or size + len(text) > max_chars):
            flush()
        if not buf:
            voice = current
        buf.append(text if text.endswith(("。", "！", "？", ".", "!", "?")) else text + "。")
        size += len(text)
        last = text
    flush()
    return chunks


def parse_tts_body(content_type: str, body: bytes, fetch_url: Callable[[str], bytes]) -> bytes:
    """音频字节直接返回；JSON 里若有 url，再把那个文件下载下来。"""
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind.startswith("audio/") or kind in {"application/octet-stream", "binary/octet-stream"}:
        if not body:
            raise SpeechError("语音接口返回了空音频")
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if body and not kind.startswith("application/json") and not kind.startswith("text/"):
            return body
        raise SpeechError("语音接口没有返回音频") from exc
    url = payload.get("url") if isinstance(payload, dict) else None
    if not url:
        raise SpeechError("语音接口返回了 JSON，但没有音频地址")
    audio = fetch_url(url)
    if not audio:
        raise SpeechError("语音文件地址是空的")
    return audio


def concat_to_mp3(parts: list[Path], dest: Path, *, gap_seconds: float = 0.35) -> None:
    """把分段音频先统一成同一采样率，再拼成一个 MP3。"""
    if not parts:
        raise SpeechError("没有可拼接的音频")
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.parent / f".concat-{dest.stem}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    try:
        silence = work / "gap.wav"
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-t",
                str(gap_seconds),
                "-c:a",
                "pcm_s16le",
                str(silence),
            ]
        )
        wavs: list[Path] = []
        for index, part in enumerate(parts):
            wav = work / f"{index:03d}.wav"
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(part),
                    "-ac",
                    "1",
                    "-ar",
                    "44100",
                    "-c:a",
                    "pcm_s16le",
                    str(wav),
                ]
            )
            wavs.append(wav)
        listing = work / "list.txt"
        lines: list[str] = []
        for index, wav in enumerate(wavs):
            if index:
                lines.append(_ffmpeg_file(silence))
            lines.append(_ffmpeg_file(wav))
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(dest),
            ]
        )
    except SpeechError:
        dest.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _ffmpeg_file(path: Path) -> str:
    text = str(path.resolve()).replace("'", "'\\''")
    return f"file '{text}'"


def speech_endpoint(base_url: str) -> str:
    """接口根地址或完整的 /audio/speech 地址都可以。"""
    base = (base_url or "").rstrip("/")
    if base.endswith("/audio/speech"):
        return base
    return f"{base}/audio/speech"


def _run(args: list[str]) -> None:
    try:
        run_process(args)
    except TaskCancelled:
        raise
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", errors="replace")
        raise SpeechError(f"ffmpeg 失败：{_ffmpeg_message(err)}") from exc
    except FileNotFoundError as exc:
        raise SpeechError("未找到 ffmpeg，请先安装") from exc


def _ffmpeg_message(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    picked = [
        line
        for line in lines
        if any(word in line.lower() for word in ("error", "invalid", "conversion failed", "failed"))
    ]
    text = " ".join(picked or lines[-3:])
    return text[-800:]


class HttpSpeaker:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        voice: str,
        speed: float = 1.0,
        max_chars: int = 300,
        timeout: float = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.speed = speed
        self.max_chars = max_chars
        self.timeout = timeout
        self._voice_alias: dict[str, str] = {}

    def speak(
        self,
        sentences: list[str],
        dest: Path,
        on_chunk: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], None] | None = None,
        voices: list[str] | None = None,
    ) -> None:
        if not self.api_key:
            raise SpeechError("未配置 TTS_API_KEY")
        voices = list(voices or [])
        if voices and len(voices) == len(sentences):
            chunks = chunk_spoken(list(zip(sentences, voices)), self.max_chars)
        else:
            chunks = [(text, self.voice) for text in chunk_for_speech(sentences, self.max_chars)]
        if not chunks:
            raise SpeechError("没有可朗读的中文文本")
        work = dest.parent / f".tts-{dest.stem}"
        work.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        total = len(chunks)
        timeout = httpx.Timeout(connect=15.0, read=float(self.timeout), write=30.0, pool=15.0)
        self._voice_alias: dict[str, str] = {}
        try:
            with httpx.Client(timeout=timeout) as client:
                cancel = active_cancel.get()
                if cancel is not None and hasattr(cancel, "track_client"):
                    cancel.track_client(client)
                try:
                    for index, (text, voice) in enumerate(chunks, start=1):
                        if should_stop:
                            should_stop()
                        if cancel is not None and cancel.is_set():
                            raise TaskCancelled()
                        part = work / f"{index:03d}.mp3"
                        if not (part.exists() and part.stat().st_size > 100):
                            part.write_bytes(self._audio(client, text, voice=voice))
                        if on_chunk:
                            on_chunk(index, total)
                        parts.append(part)
                finally:
                    if cancel is not None and hasattr(cancel, "untrack_client"):
                        cancel.untrack_client(client)
            if active_cancel.get() is not None and active_cancel.get().is_set():
                raise TaskCancelled()
            concat_to_mp3(parts, dest)
        except TaskCancelled:
            raise
        except Exception:
            raise
        else:
            shutil.rmtree(work, ignore_errors=True)

    def _audio(self, client: httpx.Client, text: str, *, voice: str | None = None) -> bytes:
        chosen = voice or self.voice
        chosen = self._voice_alias.get(chosen, chosen)
        try:
            return self._request(client, text, chosen)
        except SpeechError as exc:
            fallback = fallback_voice(self.model, chosen)
            if exc.status != 400 or not fallback:
                raise
            audio = self._request(client, text, fallback)
            original = voice or self.voice
            self._voice_alias[original] = fallback
            self._voice_alias[chosen] = fallback
            return audio

    def _request(self, client: httpx.Client, text: str, voice: str) -> bytes:
        try:
            response = run_http(
                client,
                lambda: client.post(
                    speech_endpoint(self.base_url),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "input": text,
                        "voice": voice,
                        "response_format": "mp3",
                        "speed": self.speed,
                    },
                ),
            )
        except TaskCancelled:
            raise
        except httpx.HTTPError as exc:
            if active_cancel.get() is not None and active_cancel.get().is_set():
                raise TaskCancelled() from exc
            raise SpeechError(f"无法连接语音服务：{exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise SpeechError(f"语音接口返回 {response.status_code}：{detail}", status=response.status_code)

        def fetch_url(url: str) -> bytes:
            try:
                downloaded = run_http(client, lambda: client.get(url))
                downloaded.raise_for_status()
            except TaskCancelled:
                raise
            except httpx.HTTPError as exc:
                if active_cancel.get() is not None and active_cancel.get().is_set():
                    raise TaskCancelled() from exc
                raise SpeechError(f"无法下载语音文件：{exc}") from exc
            return downloaded.content

        return parse_tts_body(response.headers.get("content-type", ""), response.content, fetch_url)


def edge_rate(speed: float) -> str:
    """把 1.0 语速转成 Edge TTS 的 +0% 写法。"""
    try:
        value = float(speed)
    except (TypeError, ValueError):
        value = 1.0
    percent = int(round((value - 1.0) * 100))
    return f"{percent:+d}%"


class EdgeSpeaker:
    """微软 Edge 在线朗读。不需要 API Key。"""

    def __init__(self, *, voice: str, speed: float = 1.0, max_chars: int = 300) -> None:
        self.model = "edge"
        self.voice = voice or "zh-CN-XiaoxiaoNeural"
        self.speed = speed
        self.max_chars = max_chars
        self.api_key = ""
        self.provider = "edge"

    def speak(
        self,
        sentences: list[str],
        dest: Path,
        on_chunk: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], None] | None = None,
        voices: list[str] | None = None,
    ) -> None:
        if edge_tts is None:
            raise SpeechError("未安装 edge-tts，请先 pip install edge-tts")
        voices = list(voices or [])
        if voices and len(voices) == len(sentences):
            chunks = chunk_spoken(list(zip(sentences, voices)), self.max_chars)
        else:
            chunks = [(text, self.voice) for text in chunk_for_speech(sentences, self.max_chars)]
        if not chunks:
            raise SpeechError("没有可朗读的中文文本")
        work = dest.parent / f".tts-{dest.stem}"
        work.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        total = len(chunks)
        rate = edge_rate(self.speed)
        try:
            for index, (text, voice) in enumerate(chunks, start=1):
                if should_stop:
                    should_stop()
                if active_cancel.get() is not None and active_cancel.get().is_set():
                    raise TaskCancelled()
                part = work / f"{index:03d}.mp3"
                if not (part.exists() and part.stat().st_size > 100):
                    self._synthesize(text, voice or self.voice, part, rate)
                if on_chunk:
                    on_chunk(index, total)
                parts.append(part)
            if active_cancel.get() is not None and active_cancel.get().is_set():
                raise TaskCancelled()
            concat_to_mp3(parts, dest)
        except TaskCancelled:
            raise
        else:
            shutil.rmtree(work, ignore_errors=True)

    def _synthesize(self, text: str, voice: str, dest: Path, rate: str) -> None:
        async def run() -> None:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await communicate.save(str(dest))

        try:
            asyncio.run(run())
        except TaskCancelled:
            raise
        except Exception as exc:
            if active_cancel.get() is not None and active_cancel.get().is_set():
                raise TaskCancelled() from exc
            raise SpeechError(f"Edge TTS 合成失败：{exc}") from exc
