import subprocess

from mutagen.mp3 import MP3

from youtube2podcast.tags import write_tags


def test_write_album_title_and_cover(tmp_path):
    mp3 = tmp_path / "lesson.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", "-q:a", "9", str(mp3)],
        check=True,
        capture_output=True,
    )
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xd9")
    write_tags(mp3, title="注意力机制入门", album="机器学习", artist="频道", cover_path=cover)
    tags = MP3(mp3).tags
    assert tags["TIT2"].text[0] == "注意力机制入门"
    assert tags["TALB"].text[0] == "机器学习"
    assert tags["TPE1"].text[0] == "频道"
    assert tags.getall("APIC")
