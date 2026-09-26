from __future__ import annotations

from urllib.parse import parse_qs, urlparse


def extract_video_id(url: str) -> str | None:
    """从常见 YouTube 链接里取出视频 ID。认不出来就返回 None。"""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.strip("/")
    if host == "youtu.be":
        return path.split("/")[0] or None
    if "youtube.com" not in host and host != "music.youtube.com":
        return None
    if parsed.path.startswith("/watch"):
        values = parse_qs(parsed.query).get("v") or []
        return values[0] if values else None
    for prefix in ("shorts/", "embed/", "live/", "v/"):
        if path.startswith(prefix):
            rest = path[len(prefix) :].split("/")[0]
            return rest or None
    return None
