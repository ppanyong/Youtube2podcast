from __future__ import annotations

import re

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SPACE = re.compile(r"\s+")


def safe_filename(name: str, max_len: int = 80) -> str:
    """去掉路径非法字符，便于当作文件夹名和曲名。"""
    cleaned = _ILLEGAL.sub(" ", name or "")
    cleaned = _SPACE.sub(" ", cleaned).strip(" .")
    if not cleaned:
        return "untitled"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or "untitled"


def format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
