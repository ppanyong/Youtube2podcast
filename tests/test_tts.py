import json
import subprocess

from youtube2podcast.tts import chunk_for_speech, concat_to_mp3, parse_tts_body, speech_endpoint


def test_speech_endpoint_accepts_root_or_full_path():
    assert speech_endpoint("https://api.siliconflow.cn/v1") == "https://api.siliconflow.cn/v1/audio/speech"
    assert speech_endpoint("https://api.siliconflow.cn/v1/audio/speech") == "https://api.siliconflow.cn/v1/audio/speech"
    sentences = ["甲" * 4, "乙" * 4, "丙" * 4]
    assert chunk_for_speech(sentences, max_chars=8) == ["甲甲甲甲。乙乙乙乙。", "丙丙丙丙。"]


def test_chunk_for_speech_skips_duplicate_lines():
    chunks = chunk_for_speech(["先听这一段。", "先听这一段。", "进入下一节。"], max_chars=80)
    assert chunks == ["先听这一段。进入下一节。"]


def test_parse_tts_audio_bytes():
    audio = parse_tts_body("audio/mpeg", b"mp3-bytes", lambda url: b"")
    assert audio == b"mp3-bytes"


def test_parse_tts_json_url():
    body = json.dumps({"url": "https://example.test/a.mp3"}).encode()
    audio = parse_tts_body("application/json", body, lambda url: b"downloaded:" + url.encode())
    assert audio.startswith(b"downloaded:")


def test_concat_mp3_mixed_sample_rates(tmp_path):
    parts = []
    for index, rate in ((1, 32000), (2, 44100)):
        part = tmp_path / f"{index}.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate={rate}",
                "-t",
                "0.25",
                "-c:a",
                "libmp3lame",
                str(part),
            ],
            check=True,
            capture_output=True,
        )
        parts.append(part)
    dest = tmp_path / "out.mp3"
    concat_to_mp3(parts, dest, gap_seconds=0.1)
    assert dest.stat().st_size > 500


def test_concat_mp3(tmp_path):
    parts = []
    for index in (1, 2):
        part = tmp_path / f"{index}.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", "-q:a", "9", str(part)],
            check=True,
            capture_output=True,
        )
        parts.append(part)
    dest = tmp_path / "out.mp3"
    concat_to_mp3(parts, dest, gap_seconds=0.1)
    assert dest.stat().st_size > 500
