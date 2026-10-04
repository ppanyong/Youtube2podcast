from youtube2podcast.speakers import Spoken, Turn, label_speakers_by_text, map_speaker_voices, parse_speaker_labels
from youtube2podcast.subtitles import Sentence


def test_parse_speaker_labels_reads_genders_and_rows():
    text = """
GENDERS
A male
B female

LABELS
0 A
1 B
2 A
"""
    labels, genders = parse_speaker_labels(text, total=3)
    assert labels == ["A", "B", "A"]
    assert genders["A"].startswith("m")
    assert genders["B"].startswith("f")


def test_label_speakers_by_text_uses_model_output():
    sentences = [
        Sentence("Hello everyone", 0, 2),
        Sentence("Thanks for having me", 2, 4),
        Sentence("Let's begin", 4, 6),
    ]

    def complete(_system, _user):
        return "GENDERS\nA male\nB female\n\nLABELS\n0 A\n1 B\n2 A\n"

    labeled, genders, turns = label_speakers_by_text(sentences, complete)
    assert [item.speaker for item in labeled] == ["A", "B", "A"]
    assert genders["A"].startswith("m")
    assert len({turn.speaker for turn in turns}) == 2


def test_map_speaker_voices_for_edge_uses_neural_voices():
    spoken = [Spoken(0, "你好", "A"), Spoken(5, "谢谢", "B")]
    turns = [Turn(0, 4, "A"), Turn(5, 8, "B")]
    voices = map_speaker_voices(
        spoken,
        turns,
        model="edge",
        default_voice="zh-CN-XiaoxiaoNeural",
        provider="edge",
        genders={"A": "female", "B": "male"},
    )
    assert voices["A"] == "zh-CN-XiaoxiaoNeural"
    assert voices["B"].startswith("zh-CN-")
    assert voices["B"] != voices["A"]
