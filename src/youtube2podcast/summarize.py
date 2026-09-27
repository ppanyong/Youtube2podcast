from __future__ import annotations

import json
import re
from dataclasses import dataclass

from youtube2podcast.names import format_clock
from youtube2podcast.translate import CompleteFn

_TS = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})$")


@dataclass
class KeyPoint:
    timestamp: float
    text: str


@dataclass
class Term:
    source: str
    zh: str


@dataclass
class Chapter:
    start: float
    title: str


@dataclass
class Summary:
    title: str
    intro: str
    points: list[KeyPoint]
    terms: list[Term]


def _as_seconds(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = _TS.match(text)
    if match:
        hours = int(match.group(1) or 0)
        return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3))
    return float(text)


def parse_summary(content: str, fallback_title: str, lines: list[tuple[float, str]]) -> Summary:
    """解析模型返回的 JSON。解析失败时用译文本身拼一份还能读的小结。"""
    try:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("没有 JSON")
        data = json.loads(content[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("小结不是对象")
        title = str(data.get("title") or "").strip() or fallback_title
        intro = str(data.get("intro") or "").strip()
        points = []
        for item in data.get("points") or []:
            point = _point_from(item)
            if point is not None:
                points.append(point)
        terms = []
        for item in data.get("terms") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("en") or item.get("source") or "").strip()
            zh = str(item.get("zh") or "").strip()
            if source and zh:
                terms.append(Term(source=source, zh=zh))
        if not intro:
            intro = _fallback_intro(lines)
        if not points:
            points = _fallback_points(lines)
        return Summary(title=title, intro=intro, points=points[:12], terms=terms[:20])
    except (ValueError, TypeError, json.JSONDecodeError, KeyError, AttributeError):
        return Summary(
            title=fallback_title or "学习笔记",
            intro=_fallback_intro(lines),
            points=_fallback_points(lines),
            terms=[],
        )


def _point_from(item) -> KeyPoint | None:
    if isinstance(item, str):
        text = item.strip()
        return KeyPoint(timestamp=0.0, text=text) if text else None
    if not isinstance(item, dict):
        return None
    text = str(item.get("text") or item.get("point") or "").strip()
    if not text:
        return None
    try:
        timestamp = max(0.0, _as_seconds(item.get("t", item.get("timestamp", 0))))
    except (TypeError, ValueError):
        timestamp = 0.0
    return KeyPoint(timestamp=timestamp, text=text)


def _fallback_intro(lines: list[tuple[float, str]]) -> str:
    text = "".join(line for _, line in lines).strip()
    return text[:180] or "（未能生成导语）"


def _fallback_points(lines: list[tuple[float, str]]) -> list[KeyPoint]:
    points = []
    for start, text in lines[:8]:
        snippet = text.strip()
        if snippet:
            points.append(KeyPoint(timestamp=start, text=snippet[:40]))
    return points


class LLMSummarizer:
    def __init__(self, complete: CompleteFn) -> None:
        self.complete = complete

    def summarize(self, original_title: str, lines: list[tuple[float, str]]) -> Summary:
        body = "\n".join(f"[{format_clock(start)}] {text}" for start, text in lines)
        system = (
            "你在为一段讲解视频写收听笔记。根据带时间戳的中文口播，输出 JSON，不要 Markdown。\n"
            "格式：\n"
            '{"title":"12字以内的中文标题","intro":"一段导语，适合开听前扫一眼",'
            '"points":[{"t":12.5,"text":"要点，口语短句"}],'
            '"terms":[{"en":"原术语","zh":"中文"}]}\n'
            "要求：title 简短；points 4 到 8 条，t 必须来自输入时间戳（秒）；只输出 JSON。"
        )
        user = f"原标题：{original_title}\n\n{body}"
        return parse_summary(self.complete(system, user), original_title, lines)


def render_markdown(
    summary: Summary,
    *,
    original_title: str,
    url: str,
    duration: float,
) -> str:
    rows = [
        f"# {summary.title}",
        "",
        f"- 原标题：{original_title}",
        f"- 链接：{url}",
        f"- 时长：{format_clock(duration)}",
        "",
        summary.intro,
        "",
        "## 要点",
        "",
    ]
    for point in summary.points:
        rows.append(f"- {format_clock(point.timestamp)} {point.text}")
    rows.extend(["", "## 术语", ""])
    if summary.terms:
        rows.extend(f"- {term.source}：{term.zh}" for term in summary.terms)
    else:
        rows.append("- （无）")
    rows.append("")
    return "\n".join(rows)
