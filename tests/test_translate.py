from youtube2podcast.translate import LLMTranslator, align_numbered, polish_translated


def test_align_numbered_keeps_original_when_a_line_is_missing():
    result = align_numbered("[0] 你好\n垃圾\n[2] 再见", ["Hello", "world", "bye"])
    assert result == ["你好", "world", "再见"]


def test_translator_keeps_batch_alignment():
    def complete(system, user):
        assert "[0]" in user
        assert "叠词" in system or "重复" in system
        return "[1] 世界\n[0] 你好"

    translator = LLMTranslator(complete, batch_size=2)
    seen = []
    result = translator.translate(["Hello", "world", "Next"], on_batch=lambda i, n: seen.append((i, n)))
    assert result[0] == "你好"
    assert result[1] == "世界"
    assert seen == [(1, 2), (2, 2)]


def test_translator_marks_near_duplicate_as_skip():
    def complete(system, user):
        return "[0] 今天我们讲注意力。\n[1] 今天我们讲注意力。"

    result = LLMTranslator(complete).translate(["Today we talk attention.", "Today we talk about attention."])
    assert result[0] == "今天我们讲注意力。"
    assert result[1] == "[重复]"


def test_polish_translated_drops_skips_and_adjacent_echo():
    lines = polish_translated(
        [
            (0.0, "先听这一段。"),
            (1.0, "[重复]"),
            (2.0, "先听这一段。"),
            (3.0, "然后进入下一节。"),
        ]
    )
    assert lines == [(0.0, "先听这一段。"), (3.0, "然后进入下一节。")]
