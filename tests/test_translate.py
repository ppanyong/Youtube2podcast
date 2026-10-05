import httpx

from youtube2podcast.llm import post_chat
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


def test_translator_prompt_and_output_keep_speakers():
    captured = {}

    def complete(system, user):
        captured["system"] = system
        captured["user"] = user
        return "[0] [A] 大家好。\n[1] [B] 谢谢邀请。"

    translator = LLMTranslator(complete)
    texts, speakers = translator.translate(
        ["Hello everyone", "Thanks for having me"],
        speakers=["A", "B"],
        with_speakers=True,
    )
    assert texts == ["大家好。", "谢谢邀请。"]
    assert speakers == ["A", "B"]
    assert "[A]" in captured["user"]
    assert "[B]" in captured["user"]
    assert "说话人" in captured["system"]
    assert "换人" in captured["system"]


def test_align_numbered_reads_speaker_letters():
    texts, speakers = align_numbered(
        "[0] [A] 你好\n[1] [B] 谢谢\n[2] 再见",
        ["Hello", "thanks", "bye"],
        with_speakers=True,
    )
    assert texts == ["你好", "谢谢", "再见"]
    assert speakers == ["A", "B", None]


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


class _Http:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ReadTimeout("The read operation timed out")
        return "ok"


def test_read_timeout_retries_the_same_request():
    http = _Http(failures=2)
    assert post_chat(http, "https://example.test", "key", {}, attempts=3) == "ok"
    assert http.calls == 3


def test_read_timeout_is_not_reported_as_a_connection_failure():
    http = _Http(failures=3)
    try:
        post_chat(http, "https://example.test", "key", {}, attempts=3)
    except RuntimeError as exc:
        assert "无法连接" not in str(exc)
        assert "已重试 3 次" in str(exc)
    else:
        raise AssertionError("timeout should fail the request")
