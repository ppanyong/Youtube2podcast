from pathlib import Path

from youtube2podcast.tts import EdgeSpeaker, edge_rate
from youtube2podcast.voices import edge_voice_choices


def test_edge_rate_converts_speed():
    assert edge_rate(1.0) == "+0%"
    assert edge_rate(1.2) == "+20%"
    assert edge_rate(0.8) == "-20%"


def test_edge_voice_choices_lists_chinese_neural_voices():
    voices = edge_voice_choices()
    ids = {item["id"] for item in voices}
    assert "zh-CN-XiaoxiaoNeural" in ids
    assert "zh-CN-YunxiNeural" in ids
    assert all(item["id"].startswith("zh-") for item in voices)


def test_edge_speaker_writes_mp3(monkeypatch, tmp_path):
    calls = []

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%"):
            calls.append((text, voice, rate))

        async def save(self, path):
            Path(path).write_bytes(b"ID3fake-mp3-bytes-for-test" + b"0" * 80)

    monkeypatch.setattr("youtube2podcast.tts.edge_tts.Communicate", FakeCommunicate)
    monkeypatch.setattr(
        "youtube2podcast.tts.concat_to_mp3",
        lambda parts, dest: Path(dest).write_bytes(b"ID3merged" + b"0" * 80),
    )
    dest = tmp_path / "out.mp3"
    speaker = EdgeSpeaker(voice="zh-CN-XiaoxiaoNeural", speed=1.1)
    speaker.speak(["你好世界", "第二句"], dest)
    assert dest.exists() and dest.stat().st_size > 50
    assert calls[0][1] == "zh-CN-XiaoxiaoNeural"
    assert calls[0][2] == "+10%"
