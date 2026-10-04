from youtube2podcast.speakers import Spoken, Turn, apply_voices, map_speaker_voices, stick_speakers
from youtube2podcast.subtitles import Sentence
from youtube2podcast.tts import chunk_spoken


def test_map_longest_speaker_keeps_the_configured_voice():
    spoken = [
        Spoken(0, "你好", "A"),
        Spoken(5, "谢谢", "B"),
        Spoken(8, "继续", "A"),
        Spoken(12, "补充", "C"),
    ]
    turns = [
        Turn(0, 4, "A"),
        Turn(5, 7, "B"),
        Turn(8, 12, "A"),
        Turn(12, 15, "C"),
    ]
    voices = map_speaker_voices(
        spoken,
        turns,
        model="FunAudioLLM/CosyVoice2-0.5B",
        default_voice="FunAudioLLM/CosyVoice2-0.5B:david",
        genders={"A": "male", "B": "female", "C": "female"},
    )
    assert voices["A"] == "FunAudioLLM/CosyVoice2-0.5B:david"
    assert voices["B"] != voices["A"]
    assert voices["C"] != voices["A"]
    assert voices["C"] != voices["B"]
    applied = apply_voices(spoken, voices, "FunAudioLLM/CosyVoice2-0.5B:david")
    assert applied[0].voice.endswith(":david")
    assert applied[1].voice != applied[0].voice
    assert applied[3].voice != applied[1].voice


def test_stick_speakers_ignores_a_brief_flicker():
    sentences = [
        Sentence("第一句", 0, 3, "A"),
        Sentence("误标", 3.1, 3.8, "B"),
        Sentence("继续讲", 3.9, 9, "A"),
    ]
    labeled = stick_speakers(sentences)
    assert [item.speaker for item in labeled] == ["A", "A", "A"]


def test_same_speaker_keeps_one_voice():
    spoken = [
        Spoken(0, "开场", "A"),
        Spoken(4, "插一句", "B"),
        Spoken(6, "我再讲", "A"),
        Spoken(10, "还是我", "A"),
    ]
    turns = [
        Turn(0, 4, "A"),
        Turn(4, 6, "B"),
        Turn(6, 14, "A"),
    ]
    voices = map_speaker_voices(
        spoken,
        turns,
        model="FunAudioLLM/CosyVoice2-0.5B",
        default_voice="FunAudioLLM/CosyVoice2-0.5B:david",
        genders={"A": "male", "B": "female"},
    )
    applied = apply_voices(spoken, voices, "FunAudioLLM/CosyVoice2-0.5B:david")
    host = [item.voice for item in applied if item.speaker == "A"]
    assert len(set(host)) == 1
    assert applied[1].voice != host[0]


def test_chunk_spoken_does_not_merge_different_voices():
    chunks = chunk_spoken(
        [("先听这一段", "v1"), ("然后换人", "v2"), ("还是第二个人", "v2")],
        max_chars=80,
    )
    assert chunks[0] == ("先听这一段。", "v1")
    assert chunks[1][1] == "v2"
    assert "然后换人" in chunks[1][0]
