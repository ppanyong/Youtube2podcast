from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from youtube2podcast.subtitles import Sentence
from youtube2podcast.voices import COSYVOICE_PRESETS, EDGE_FEMALE, EDGE_MALE

_MALE = ("david", "charles", "benjamin", "alex")
_FEMALE = ("diana", "bella", "claire", "anna")
_SPEAKER_NAMES = "ABCDEFGH"
_TEXT_BATCH = 60


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


@dataclass
class Spoken:
    start: float
    text: str
    speaker: str | None = None
    voice: str | None = None


def map_speaker_voices(
    spoken: list[Spoken],
    turns: list[Turn],
    *,
    model: str,
    default_voice: str,
    provider: str = "siliconflow",
    genders: dict[str, str] | None = None,
) -> dict[str, str]:
    """时长最长的人用配置里的音色，其余按性别轮换预设。"""
    names = _speaker_order(spoken, turns)
    if not names:
        return {}
    genders = genders or {}
    default_name = _voice_name(default_voice)
    used = {default_name} if default_name else set()
    mapping: dict[str, str] = {}
    for index, name in enumerate(names):
        if index == 0 and default_voice:
            mapping[name] = default_voice
            continue
        female = _is_female(name, genders)
        mapping[name] = _next_preset(model, female, used, provider=provider)
    return mapping


def parse_speaker_labels(text: str, *, total: int) -> tuple[list[str | None], dict[str, str]]:
    """解析模型返回的说话人标注。"""
    labels: list[str | None] = [None] * total
    genders: dict[str, str] = {}
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("GENDER"):
            section = "genders"
            continue
        if upper.startswith("LABEL"):
            section = "labels"
            continue
        if section == "genders":
            parts = line.replace(":", " ").split()
            if len(parts) >= 2 and parts[0][:1].upper() in _SPEAKER_NAMES:
                genders[parts[0][:1].upper()] = parts[1].lower()
            continue
        if section != "labels":
            continue
        parts = line.replace(":", " ").split()
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        speaker = parts[1][:1].upper()
        if 0 <= index < total and speaker in _SPEAKER_NAMES:
            labels[index] = speaker
    return labels, genders


def label_speakers_by_text(
    sentences: list[Sentence],
    complete: Callable[[str, str], str],
) -> tuple[list[Sentence], dict[str, str], list[Turn]]:
    """用大模型读英文字幕，给每句贴说话人，并估计男女。"""
    if not sentences:
        return [], {}, []
    labels: list[str | None] = [None] * len(sentences)
    genders: dict[str, str] = {}
    for start in range(0, len(sentences), _TEXT_BATCH):
        chunk = sentences[start : start + _TEXT_BATCH]
        previous = ""
        for index in range(start - 1, -1, -1):
            if labels[index]:
                previous = f"Previous speaker before this batch: {labels[index]}"
                break
        rows = "\n".join(f"{start + offset}|{item.text}" for offset, item in enumerate(chunk))
        prompt = (
            f"{previous}\n\nLines (index|english):\n{rows}\n\n"
            "Label speakers for a podcast/interview transcript.\n"
            "Reuse letters A,B,C... Same person keeps the same letter.\n"
            "Prefer fewer speakers. Monologue -> all A.\n"
            "Reply exactly in this format:\n"
            "GENDERS\nA male\nB female\n\n"
            "LABELS\n"
            f"{start} A\n"
            f"{start + 1} B\n"
        )
        system = "You assign speaker letters to transcript lines. Output only GENDERS and LABELS blocks."
        piece_labels, piece_genders = parse_speaker_labels(complete(system, prompt), total=len(sentences))
        for index, speaker in enumerate(piece_labels):
            if speaker and labels[index] is None:
                labels[index] = speaker
        genders.update(piece_genders)
    filled = _fill_labels(labels)
    labeled = [
        Sentence(text=item.text, start=item.start, end=item.end, speaker=filled[index])
        for index, item in enumerate(sentences)
    ]
    labeled = stick_speakers(labeled)
    turns = turns_from_sentences(labeled)
    if not genders:
        genders = _guess_genders(labeled)
    return labeled, genders, turns


def apply_voices(spoken: list[Spoken], voices: dict[str, str], fallback: str) -> list[Spoken]:
    for item in spoken:
        if item.speaker and item.speaker in voices:
            item.voice = voices[item.speaker]
        else:
            item.voice = fallback
    return spoken


def stick_speakers(sentences: list[Sentence]) -> list[Sentence]:
    """前后都是同一个人时，中间那句很短的跳号算误判，钉回原来的人。"""
    if len(sentences) < 3:
        return sentences
    stuck = list(sentences)
    for index in range(1, len(stuck) - 1):
        prev, current, nxt = stuck[index - 1], stuck[index], stuck[index + 1]
        if current.speaker == prev.speaker:
            continue
        if prev.speaker != nxt.speaker:
            continue
        if current.end - current.start > 1.2:
            continue
        if current.start - prev.end > 2.0 or nxt.start - current.end > 2.0:
            continue
        stuck[index] = Sentence(text=current.text, start=current.start, end=current.end, speaker=prev.speaker)
    return stuck


def turns_from_sentences(sentences) -> list[Turn]:
    """按字幕句的说话人拼出时间轴，给后面绑音色用。"""
    turns: list[Turn] = []
    for item in sentences:
        speaker = item.speaker or "A"
        end = float(getattr(item, "end", None) or item.start)
        if turns and turns[-1].speaker == speaker and item.start <= turns[-1].end + 1.5:
            turns[-1].end = max(turns[-1].end, end)
            continue
        turns.append(Turn(start=item.start, end=end, speaker=speaker))
    return turns


def _speaker_order(spoken: list[Spoken], turns: list[Turn]) -> list[str]:
    duration: dict[str, float] = {}
    for turn in turns:
        duration[turn.speaker] = duration.get(turn.speaker, 0.0) + max(0.0, turn.end - turn.start)
    if not duration:
        for item in spoken:
            if item.speaker:
                duration[item.speaker] = duration.get(item.speaker, 0.0) + 1
    return sorted(duration, key=lambda name: (-duration[name], name))


def _voice_name(voice: str) -> str:
    text = (voice or "").strip()
    if text.lower().startswith("zh-"):
        return text.lower()
    return text.split(":")[-1].strip().lower()


def _is_female(name: str, genders: dict[str, str]) -> bool:
    hint = (genders.get(name) or "").lower()
    if hint:
        return hint.startswith("f") or "女" in hint or "woman" in hint
    return False


def _next_preset(model: str, female: bool, used: set[str], *, provider: str = "siliconflow") -> str:
    if (provider or "").strip().lower() == "edge":
        names = list(EDGE_FEMALE if female else EDGE_MALE) + list(EDGE_MALE if female else EDGE_FEMALE)
        for name in names:
            key = name.lower()
            if key in used:
                continue
            used.add(key)
            return name
        return "zh-CN-XiaoxiaoNeural"
    names = list(_FEMALE if female else _MALE) + list(_MALE if female else _FEMALE)
    for extra in COSYVOICE_PRESETS:
        if extra not in names:
            names.append(extra)
    for name in names:
        if name in used:
            continue
        used.add(name)
        prefix = (model or "").rstrip(":")
        return f"{prefix}:{name}" if prefix else name
    prefix = (model or "").rstrip(":")
    return f"{prefix}:diana" if prefix else "diana"


def _fill_labels(labels: list[str | None]) -> list[str]:
    filled: list[str] = []
    carry = "A"
    for item in labels:
        if item:
            carry = item
        filled.append(carry)
    return filled


def _guess_genders(sentences: list[Sentence]) -> dict[str, str]:
    names = []
    for item in sentences:
        if item.speaker and item.speaker not in names:
            names.append(item.speaker)
    return {name: ("female" if index % 2 else "male") for index, name in enumerate(names)}
