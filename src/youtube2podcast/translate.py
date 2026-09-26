from __future__ import annotations

import re
from collections.abc import Callable

from youtube2podcast.subtitles import collapse_echo, is_near_duplicate

_NUM_RE = re.compile(r"^\s*[\[\(]?(\d+)[\]\)\.\:、]\s*(.*)$")
_SKIP_MARKERS = ("[重复]", "【重复】", "(重复)", "重复", "[skip]", "[SKIP]")

CompleteFn = Callable[[str, str], str]


def align_numbered(content: str, segments: list[str]) -> list[str]:
    """按编号回填译文。缺行或解析失败时保留原文，长度始终与输入一致。"""
    result = list(segments)
    for raw in content.splitlines():
        match = _NUM_RE.match(raw)
        if not match:
            continue
        idx = int(match.group(1))
        text = (match.group(2) or "").strip()
        if 0 <= idx < len(result) and text:
            result[idx] = text
    return result


def is_skip_marker(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    return cleaned in _SKIP_MARKERS or cleaned.lower() in {"[skip]", "skip"}


def polish_translated(lines: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """去掉相邻复读，并把句内叠词压平，供朗读使用。"""
    kept: list[tuple[float, str]] = []
    for start, text in lines:
        cleaned = collapse_echo((text or "").strip())
        if is_skip_marker(cleaned):
            continue
        if kept and is_near_duplicate(kept[-1][1], cleaned):
            prev_start, prev_text = kept[-1]
            if len(cleaned) > len(prev_text):
                kept[-1] = (prev_start, cleaned)
            continue
        kept.append((start, cleaned))
    return kept


class LLMTranslator:
    def __init__(self, complete: CompleteFn, *, batch_size: int = 12) -> None:
        self.complete = complete
        self.batch_size = batch_size

    def translate(
        self,
        sentences: list[str],
        on_batch: Callable[[int, int], None] | None = None,
        *,
        previous: str = "",
    ) -> list[str]:
        if not sentences:
            return []
        translated: list[str] = []
        batches = [sentences[i : i + self.batch_size] for i in range(0, len(sentences), self.batch_size)]
        total = len(batches)
        carry = previous
        for index, batch in enumerate(batches, start=1):
            if on_batch:
                on_batch(index, total)
            piece = self._translate_batch(batch, previous=carry)
            translated.extend(piece)
            for item in reversed(piece):
                if item and not is_skip_marker(item):
                    carry = item
                    break
        return translated

    def _translate_batch(self, segments: list[str], *, previous: str = "") -> list[str]:
        numbered = "\n".join(f"[{i}] {text}" for i, text in enumerate(segments))
        system = (
            "你是专业的视频口播翻译，把带编号的英文字幕译成适合一口气听完的简体中文。\n"
            "YouTube 自动字幕常有叠词、半句复读和滚动重写，请根据上下文处理。\n"
            "要求：\n"
            "1) 每行输出格式 [序号] 简体中文译文，序号与输入一一对应；\n"
            "2) 口语自然、连贯，适合朗读；保留专有名词；\n"
            "3) 去掉句内重复词和与上一句实质相同的复读；\n"
            "4) 若本句只是重复前文，没有新信息，该行只输出 [重复]；\n"
            "5) 不要合并、拆分序号，不要解释。"
        )
        context = f"上一句中文（供连贯，勿再复述）：{previous}\n\n" if previous else ""
        user = f"{context}共 {len(segments)} 句：\n{numbered}"
        content = self.complete(system, user)
        aligned = align_numbered(content, segments)
        cleaned: list[str] = []
        last = previous
        for text in aligned:
            item = collapse_echo((text or "").strip())
            if is_skip_marker(item) or (last and is_near_duplicate(last, item)):
                cleaned.append("[重复]")
                continue
            cleaned.append(item)
            last = item
        return cleaned
