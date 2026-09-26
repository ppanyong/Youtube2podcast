from youtube2podcast.names import safe_filename


def test_safe_filename_removes_illegal_characters_and_keeps_chinese():
    assert safe_filename('a/b:c*?"<>|') == "a b c"
    assert safe_filename("注意力机制入门") == "注意力机制入门"
    assert safe_filename("   ") == "untitled"
    assert len(safe_filename("字" * 200, max_len=20)) == 20
