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


def voice_choices(model: str, custom: list[dict] | None = None) -> list[dict]:
    """系统预设只在模型名像 CosyVoice2 时给出；自定义音色来自接口返回。"""
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


def fallback_voice(model: str, voice: str) -> str:
    """所选音色不可用时，退回 CosyVoice2 的 diana。其他模型没有通用替代。"""
    if "cosyvoice2" not in (model or "").lower():
        return ""
    default = f"{model}:diana"
    return "" if voice == default else default
