from youtube2podcast.subtitles import Cue, english_track_candidates, merge_cues, parse_vtt, pick_english_track


VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello <c>world</c>.

00:00:05.500 --> 00:00:07.000
Next line
"""


def test_parse_vtt_strips_tags_and_keeps_times():
    cues = parse_vtt(VTT)
    assert len(cues) == 2
    assert cues[0].text == "Hello world."
    assert cues[0].start == 1
    assert cues[0].duration == 2
    assert cues[1].start == 5.5


def test_merge_splits_on_sentence_and_gap():
    cues = [
        Cue("Hello", 0, 1),
        Cue("there.", 1.1, 1),
        Cue("A new sentence starts here", 4, 1),
    ]
    sentences = merge_cues(cues)
    assert [item.text for item in sentences] == ["Hello there.", "A new sentence starts here"]
    assert sentences[0].start == 0
    assert sentences[1].start == 4


def test_merge_dedupes_rolling_captions():
    cues = [
        Cue("Hello", 0, 2),
        Cue("Hello world.", 0.4, 2),
        Cue("How are you?", 3.5, 1.5),
    ]
    sentences = merge_cues(cues)
    assert [item.text for item in sentences] == ["Hello world.", "How are you?"]
    assert sentences[0].start == 0


def test_merge_dedupes_close_repeated_captions_and_phrase_echo():
    cues = [
        Cue("so we have", 0, 1),
        Cue("so we have the model", 0.3, 1.2),
        Cue("so we have the model", 1.6, 1),
        Cue("Next topic begins here.", 4, 1),
    ]
    sentences = merge_cues(cues)
    assert [item.text for item in sentences] == ["so we have the model", "Next topic begins here."]


def test_english_candidates_try_plain_en_before_en_orig():
    tracks = {"en-orig": [{"url": "a"}], "en-US": [{"url": "b"}], "en": [{"url": "c"}]}
    assert english_track_candidates(tracks, {"en": [{"url": "auto"}]}) == [
        ("en", "manual"),
        ("en-US", "manual"),
        ("en-orig", "manual"),
    ]


def test_pick_english_prefers_manual_then_auto():
    assert pick_english_track({"en-US": [{"url": "x"}], "en": [{"url": "y"}]}, {}) == ("en", "manual")
    assert pick_english_track({}, {"en": [{"url": "x"}]}) == ("en", "auto")
    assert pick_english_track({}, {"fr": [{"url": "x"}]}) is None
    assert pick_english_track({"ja": [{}]}, {"de": [{}]}) is None
