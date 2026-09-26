from youtube2podcast.summarize import (
    LLMSummarizer,
    parse_summary,
    render_markdown,
)


def test_parse_summary_and_markdown():
    raw = """说明
    {"title":"注意力机制入门","intro":"先听这一段导语。","points":[{"t":"0:12","text":"为什么需要注意力"}],"terms":[{"en":"attention","zh":"注意力"}]}
    """
    summary = parse_summary(raw, "Attention", [(0, "句子")])
    text = render_markdown(summary, original_title="Attention is all you need", url="https://youtu.be/abc", duration=95)
    assert "# 注意力机制入门" in text
    assert "原标题：Attention is all you need" in text
    assert "0:12 为什么需要注意力" in text
    assert "attention：注意力" in text
    assert "时长：1:35" in text


def test_parse_summary_falls_back_when_json_is_broken():
    summary = parse_summary("不是 json", "原标题", [(3, "第一句中文。")])
    assert summary.title == "原标题"
    assert summary.points[0].text.startswith("第一句")


def test_summarizer_uses_complete():
    def complete(system, user):
        assert "0:01" in user
        return '{"title":"短标题","intro":"导语","points":[{"t":1,"text":"要点"}],"terms":[]}'

    summary = LLMSummarizer(complete).summarize("English title", [(1, "中文")])
    assert summary.title == "短标题"
