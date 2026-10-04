from __future__ import annotations

import re
from dataclasses import dataclass

_TS = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})\s+-->\s+"
    r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})"
)
_TAG = re.compile(r"<[^>]+>")
_SENTENCE_END = tuple(".?!。！？")
_WORD = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")
_COMPARE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

_MANUAL_LANGS = ("en", "en-US", "en-GB", "en-orig", "en-us", "en-gb")


@dataclass
class Cue:
    text: str
    start: float
    duration: float


@dataclass
class Sentence:
    text: str
    start: float
    end: float
    speaker: str | None = None


def _seconds(hours: str | None, minutes: str, secs: str, millis: str) -> float:
    ms = millis.ljust(3, "0")[:3]
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(secs) + int(ms) / 1000


def _clean_text(text: str) -> str:
    text = _TAG.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def parse_vtt(content: str) -> list[Cue]:
    """解析 WebVTT，忽略样式头和空字幕。"""
    cues: list[Cue] = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        match = _TS.search(lines[i])
        if not match:
            i += 1
            continue
        start = _seconds(*match.group(1, 2, 3, 4))
        end = _seconds(*match.group(5, 6, 7, 8))
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i].strip())
            i += 1
        text = _clean_text(" ".join(text_lines))
        if text and end > start:
            cues.append(Cue(text=text, start=start, duration=end - start))
    return cues


def normalize_compare(text: str) -> str:
    return _COMPARE.sub("", (text or "").lower())


def is_near_duplicate(left: str, right: str, *, min_ratio: float = 0.55) -> bool:
    """判断两句是否实质重复：相同，或一句几乎被另一句包住。"""
    a = normalize_compare(left)
    b = normalize_compare(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer and len(shorter) / len(longer) >= min_ratio:
        return True
    return False


def collapse_echo(text: str) -> str:
    """去掉句内英文叠词，以及连续复读的短语；保留句末标点。"""
    original = (text or "").strip()
    if not original:
        return ""
    trailing = ""
    match = re.search(r"[.!?。！？]+$", original)
    if match:
        trailing = match.group(0)
        body = original[: match.start()]
    else:
        body = original
    tokens = _WORD.findall(body)
    if not tokens:
        return original
    cleaned: list[str] = []
    for token in tokens:
        if (
            cleaned
            and cleaned[-1].lower() == token.lower()
            and re.fullmatch(r"[A-Za-z0-9]+", token)
        ):
            continue
        cleaned.append(token)
    # 连续短语复读：至少 3 个词才压，避免误伤「甲甲甲甲」一类叠字。
    changed = True
    while changed and len(cleaned) >= 6:
        changed = False
        for size in range(min(12, len(cleaned) // 2), 2, -1):
            if len(cleaned) < size * 2:
                continue
            head = [item.lower() for item in cleaned[-size * 2 : -size]]
            tail = [item.lower() for item in cleaned[-size:]]
            if head == tail:
                cleaned = cleaned[:-size]
                changed = True
                break
    pieces: list[str] = []
    for token in cleaned:
        if pieces and re.match(r"[A-Za-z0-9]", token) and re.match(r"[A-Za-z0-9]", pieces[-1][-1:]):
            pieces.append(" ")
        pieces.append(token)
    return ("".join(pieces) + trailing).strip()


def dedupe_rolling(cues: list[Cue]) -> list[Cue]:
    """去掉自动字幕里逐步变长的叠句，以及紧挨着的复读。"""
    kept: list[Cue] = []
    for cue in cues:
        if not kept:
            kept.append(cue)
            continue
        prev = kept[-1]
        gap = cue.start - (prev.start + prev.duration)
        overlaps = gap < 0.05
        close = gap < 0.9
        longer = cue.text if len(cue.text) >= len(prev.text) else prev.text
        shorter = prev.text if longer == cue.text else cue.text
        rolling = overlaps and (cue.text.startswith(prev.text) or prev.text in cue.text or prev.text.startswith(cue.text))
        repeated = close and is_near_duplicate(prev.text, cue.text)
        if rolling or (repeated and len(cue.text) >= len(prev.text)):
            kept[-1] = Cue(
                text=longer if repeated or cue.text.startswith(prev.text) or prev.text in cue.text else prev.text,
                start=prev.start,
                duration=max(prev.duration, (cue.start + cue.duration) - prev.start),
            )
            continue
        if (overlaps or close) and (prev.text.startswith(cue.text) or is_near_duplicate(prev.text, cue.text)):
            continue
        if shorter and longer and close and shorter in longer and shorter != longer:
            kept[-1] = Cue(
                text=longer,
                start=prev.start,
                duration=max(prev.duration, (cue.start + cue.duration) - prev.start),
            )
            continue
        kept.append(cue)
    return kept


def merge_cues(
    cues: list[Cue],
    *,
    gap_seconds: float = 1.2,
    max_chars: int = 220,
) -> list[Sentence]:
    """按句末标点和停顿，把碎片字幕并成适合朗读的句子。"""
    sentences: list[Sentence] = []
    buf: list[Cue] = []

    def flush() -> None:
        if not buf:
            return
        text = collapse_echo(re.sub(r"\s+", " ", " ".join(c.text.strip() for c in buf if c.text.strip())).strip())
        if text:
            start = buf[0].start
            end = buf[-1].start + buf[-1].duration
            sentences.append(Sentence(text=text, start=start, end=end, speaker=None))
        buf.clear()

    for cue in dedupe_rolling(cues):
        text = cue.text.strip()
        if not text:
            continue
        if buf:
            prev = buf[-1]
            if is_near_duplicate(prev.text, text) or text.startswith(prev.text) or prev.text.startswith(text):
                longer = text if len(text) >= len(prev.text) else prev.text
                buf[-1] = Cue(
                    text=longer,
                    start=prev.start,
                    duration=max(prev.duration, (cue.start + cue.duration) - prev.start),
                )
                if longer.endswith(_SENTENCE_END):
                    flush()
                continue
            gap = cue.start - (prev.start + prev.duration)
            current_len = sum(len(item.text) for item in buf)
            if gap > gap_seconds or current_len >= max_chars:
                flush()
        buf.append(cue)
        if text.endswith(_SENTENCE_END):
            flush()
    flush()
    return dedupe_adjacent_sentences(sentences)


def dedupe_adjacent_sentences(sentences: list[Sentence]) -> list[Sentence]:
    """去掉相邻实质重复的整句，保留时间较长/较完整的那一句。"""
    kept: list[Sentence] = []
    for sentence in sentences:
        text = collapse_echo(sentence.text)
        if not text:
            continue
        current = Sentence(text=text, start=sentence.start, end=sentence.end, speaker=sentence.speaker)
        if kept and is_near_duplicate(kept[-1].text, current.text):
            prev = kept[-1]
            speaker = current.speaker or prev.speaker
            if len(current.text) >= len(prev.text):
                kept[-1] = Sentence(text=current.text, start=prev.start, end=max(prev.end, current.end), speaker=speaker)
            else:
                kept[-1] = Sentence(text=prev.text, start=prev.start, end=max(prev.end, current.end), speaker=speaker)
            continue
        kept.append(current)
    return kept


def _lang_rank(code: str) -> int:
    lowered = code.lower()
    preferred = [item.lower() for item in _MANUAL_LANGS]
    if lowered in preferred:
        return preferred.index(lowered)
    if lowered.startswith("en"):
        return len(preferred)
    return 10_000


def _english_codes(tracks: dict) -> list[str]:
    viable = [code for code, items in (tracks or {}).items() if items]
    english = [code for code in viable if code.lower().startswith("en")]
    return sorted(english, key=_lang_rank)


def english_track_candidates(subtitles: dict, automatic: dict) -> list[tuple[str, str]]:
    """按优先级列出可尝试的英文字幕。人工轨在前，同一语言不重复尝试自动轨。"""
    manual = [(code, "manual") for code in _english_codes(subtitles)]
    seen = {code.lower() for code, _kind in manual}
    auto = [(code, "auto") for code in _english_codes(automatic) if code.lower() not in seen]
    return manual + auto


def pick_english_track(subtitles: dict, automatic: dict) -> tuple[str, str] | None:
    """优先人工英文字幕，其次自动英文字幕。返回 (语言代码, manual|auto)。"""
    candidates = english_track_candidates(subtitles, automatic)
    return candidates[0] if candidates else None
