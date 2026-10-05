from __future__ import annotations

COSYVOICE_PRESETS = (
    "alex",
    "anna",
    "bella",
    "benjamin",
    "charles",
    "claire",
    "david",
    "diana",
)

EDGE_FEMALE = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-XiaochenNeural",
    "zh-CN-XiaohanNeural",
)
EDGE_MALE = (
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-YunxiaNeural",
)
EDGE_VOICES = (
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女"),
    ("zh-CN-XiaochenNeural", "晓辰 · 女"),
    ("zh-CN-XiaohanNeural", "晓涵 · 女"),
    ("zh-CN-YunxiNeural", "云希 · 男"),
    ("zh-CN-YunjianNeural", "云健 · 男"),
    ("zh-CN-YunyangNeural", "云扬 · 男"),
    ("zh-CN-YunxiaNeural", "云夏 · 男"),
)


def edge_voice_choices() -> list[dict]:
    return [{"id": voice_id, "label": label} for voice_id, label in EDGE_VOICES]


def voice_choices(model: str, custom: list[dict] | None = None, *, provider: str = "siliconflow") -> list[dict]:
    """系统预设只在模型名像 CosyVoice2 时给出；Edge 用微软中文 Neural；自定义音色来自接口返回。"""
    if (provider or "").strip().lower() == "edge":
        return edge_voice_choices()
    choices: list[dict] = []
    seen: set[str] = set()
    if "cosyvoice2" in (model or "").lower():
        for name in COSYVOICE_PRESETS:
            voice_id = f"{model}:{name}"
            seen.add(voice_id)
            choices.append({"id": voice_id, "label": name})
    for item in custom or []:
        uri = str(item.get("uri") or "").strip()
        if not uri or uri in seen:
            continue
        seen.add(uri)
        label = str(item.get("customName") or "").strip() or "自定义音色"
        choices.append({"id": uri, "label": label})
    return choices


def fallback_voice(model: str, voice: str, *, provider: str = "siliconflow") -> str:
    """所选音色不可用时，退回 CosyVoice2 的 diana 或 Edge 的晓晓。"""
    if (provider or "").strip().lower() == "edge":
        default = "zh-CN-XiaoxiaoNeural"
        return "" if voice == default else default
    if "cosyvoice2" not in (model or "").lower():
        return ""
    default = f"{model}:diana"
    return "" if voice == default else default
