from __future__ import annotations

from pathlib import Path


def write_tags(
    mp3: Path,
    *,
    title: str,
    album: str,
    artist: str,
    cover_path: Path | None = None,
) -> None:
    """写入曲名、专辑、艺术家和封面，方便在音乐库里按专辑收听。"""
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1
    from mutagen.mp3 import MP3

    audio = MP3(mp3)
    tags = audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    assert isinstance(tags, ID3)
    tags.delall("TIT2")
    tags.delall("TALB")
    tags.delall("TPE1")
    tags.delall("APIC")
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TPE1(encoding=3, text=artist or "YouTube"))
    if cover_path and cover_path.exists():
        mime = "image/jpeg"
        if cover_path.suffix.lower() == ".png":
            mime = "image/png"
        elif cover_path.suffix.lower() == ".webp":
            mime = "image/webp"
        tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=cover_path.read_bytes(),
            )
        )
    audio.save()
