from __future__ import annotations

import re
from collections.abc import Callable

from youtube2podcast.llm import ContentBlocked
from youtube2podcast.subtitles import collapse_echo, is_near_duplicate

_NUM_RE = re.compile(r"^\s*[\[\(]?(\d+)[\]\)\.\:、]\s*(.*)$")
_SPEAKER_LINE_RE = re.compile(
    r"^\s*[\[\(]?(\d+)[\]\)\.\:、]\s*(?:\[([A-Ha-h])\]|([A-Ha-h])[:：])\s*(.*)$"
)
_SKIP_MARKERS = ("[重复]", "【重复】", "(重复)", "重复", "[skip]", "[SKIP]")
_SPEAKER_LETTERS = set("ABCDEFGH")

CompleteFn = Callable[[str, str], str]


def align_numbered(content: str, segments: list[str], *, with_speakers: bool = False):
    """按编号回填译文。缺行或解析失败时保留原文，长度始终与输入一致。"""
    result = list(segments)
    speakers: list[str | None] = [None] * len(segments)
    for raw in content.splitlines():
        speaker_match = _SPEAKER_LINE_RE.match(raw)
        if speaker_match:
            idx = int(speaker_match.group(1))
            letter = (speaker_match.group(2) or speaker_match.group(3) or "").upper()
            text = (speaker_match.group(4) or "").strip()
            if 0 <= idx < len(result) and text:
                result[idx] = text
                if letter in _SPEAKER_LETTERS:
                    speakers[idx] = letter
            continue
        match = _NUM_RE.match(raw)
        if not match:
            continue
        idx = int(match.group(1))
        text = (match.group(2) or "").strip()
        if 0 <= idx < len(result) and text:
            result[idx] = text
    if with_speakers:
        return result, speakers
    return result


def is_skip_marker(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    return cleaned in _SKIP_MARKERS or cleaned.lower() in {"[skip]", "skip"}


def polish_translated(lines: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """去掉相邻复读，并把句内叠词压平，供朗读使用。"""
    return [(item.start, item.text) for item in polish_spoken(lines)]


def polish_spoken(lines) -> list:
    """与 polish_translated 相同，但保留说话人。"""
    from youtube2podcast.speakers import Spoken

    kept: list[Spoken] = []
    for item in lines:
        if isinstance(item, Spoken):
            start, text, speaker = item.start, item.text, item.speaker
        elif len(item) == 3:
            start, text, speaker = item
        else:
            start, text = item
            speaker = None
        cleaned = collapse_echo((text or "").strip())
        if is_skip_marker(cleaned):
            continue
        current = Spoken(start=float(start), text=cleaned, speaker=speaker)
        if kept and is_near_duplicate(kept[-1].text, cleaned):
            if len(cleaned) > len(kept[-1].text):
                kept[-1] = Spoken(start=kept[-1].start, text=cleaned, speaker=speaker or kept[-1].speaker)
            continue
        kept.append(current)
    return kept


class LLMTranslator:
    def __init__(self, complete: CompleteFn, *, batch_size: int = 12) -> None:
        self.complete = complete
        self.batch_size = batch_size

    def translate(
        self,
        sentences: list[str],
        on_batch: Callable[[int, int], None] | None = None,
        on_partial: Callable[[list[str]], None] | None = None,
        *,
        previous: str = "",
        speakers: list[str | None] | None = None,
        previous_speaker: str | None = None,
        with_speakers: bool = False,
    ):
        if not sentences:
            return ([], []) if with_speakers else []
        translated: list[str] = []
        labeled: list[str | None] = []
        batches = [sentences[i : i + self.batch_size] for i in range(0, len(sentences), self.batch_size)]
        speaker_batches = None
        if speakers is not None:
            padded = list(speakers) + [None] * max(0, len(sentences) - len(speakers))
            speaker_batches = [padded[i : i + self.batch_size] for i in range(0, len(sentences), self.batch_size)]
        total = len(batches)
        carry = previous
        carry_speaker = previous_speaker
        for index, batch in enumerate(batches, start=1):
            if on_batch:
                on_batch(index, total)
            batch_speakers = speaker_batches[index - 1] if speaker_batches is not None else None
            piece, piece_speakers = self._translate_batch(
                batch,
                previous=carry,
                speakers=batch_speakers,
                previous_speaker=carry_speaker,
            )
            translated.extend(piece)
            labeled.extend(piece_speakers)
            if on_partial:
                on_partial(list(translated))
            for item, speaker in zip(reversed(piece), reversed(piece_speakers)):
                if item and not is_skip_marker(item):
                    carry = item
                    carry_speaker = speaker or carry_speaker
                    break
        if with_speakers:
            return translated, labeled
        return translated

    def _translate_batch(
        self,
        segments: list[str],
        *,
        previous: str = "",
        speakers: list[str | None] | None = None,
        previous_speaker: str | None = None,
    ) -> tuple[list[str], list[str | None]]:
        numbered = "\n".join(_format_source_line(i, text, speakers) for i, text in enumerate(segments))
        system = (
            "你是专业的视频口播翻译，把带编号的英文字幕译成适合一口气听完的简体中文。\n"
            "这常常是访谈或多人对话：每一行只属于一个人，换人时不要把两个人的话揉进同一句。\n"
            "YouTube 自动字幕常有叠词、半句复读和滚动重写，请根据上下文处理。\n"
            "输入里的 [A]/[B]/[C] 是说话人。同一字母必须是同一个人；换字母就是换人，沿用即可。\n"
            "若没有字母，根据问答、称呼、你/我、明显转场判断是否换人，用 A/B/C 标出；独白则全部标 A。\n"
            "要求：\n"
            "1) 每行输出 [序号] [字母] 简体中文译文，序号与输入一一对应；\n"
            "2) 口语自然、连贯，适合朗读；保留专有名词；人称要跟这个说话人一致；\n"
            "3) 去掉句内重复词和与上一句实质相同的复读；\n"
            "4) 若本句只是重复前文，没有新信息，该行只输出 [序号] [字母] [重复]；\n"
            "5) 不要合并不同说话人的台词，不要拆分序号，不要解释。"
        )
        context = ""
        if previous:
            who = f"，说话人 {previous_speaker}" if previous_speaker else ""
            context = f"上一句中文（供连贯，勿再复述{who}）：{previous}\n\n"
        user = f"{context}共 {len(segments)} 句：\n{numbered}"
        try:
            content = self.complete(system, user)
        except ContentBlocked:
            if len(segments) == 1:
                letter = None
                if speakers:
                    raw = speakers[0]
                    letter = str(raw).strip()[:1].upper() if raw else None
                    if letter not in _SPEAKER_LETTERS:
                        letter = None
                return ["[重复]"], [letter]
            mid = max(1, len(segments) // 2)
            left_speakers = speakers[:mid] if speakers is not None else None
            right_speakers = speakers[mid:] if speakers is not None else None
            left_texts, left_who = self._translate_batch(
                segments[:mid],
                previous=previous,
                speakers=left_speakers,
                previous_speaker=previous_speaker,
            )
            carry = previous
            carry_speaker = previous_speaker
            for item, speaker in zip(reversed(left_texts), reversed(left_who)):
                if item and not is_skip_marker(item):
                    carry = item
                    carry_speaker = speaker or carry_speaker
                    break
            right_texts, right_who = self._translate_batch(
                segments[mid:],
                previous=carry,
                speakers=right_speakers,
                previous_speaker=carry_speaker,
            )
            return left_texts + right_texts, left_who + right_who
        aligned, found = align_numbered(content, segments, with_speakers=True)
        cleaned: list[str] = []
        who: list[str | None] = []
        last = previous
        for index, text in enumerate(aligned):
            item = collapse_echo((text or "").strip())
            letter = found[index]
            if speakers and index < len(speakers) and speakers[index]:
                given = str(speakers[index]).strip()[:1].upper()
                if given in _SPEAKER_LETTERS:
                    letter = given
            if is_skip_marker(item) or (last and is_near_duplicate(last, item)):
                cleaned.append("[重复]")
                who.append(letter)
                continue
            cleaned.append(item)
            who.append(letter)
            last = item
        return cleaned, who


def _format_source_line(index: int, text: str, speakers: list[str | None] | None) -> str:
    letter = ""
    if speakers and index < len(speakers) and speakers[index]:
        raw = str(speakers[index]).strip()[:1].upper()
        if raw in _SPEAKER_LETTERS:
            letter = f"[{raw}] "
    return f"[{index}] {letter}{text}"
