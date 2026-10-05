from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path


def load_env(path: Path | None = None) -> None:
    """读取 .env，不覆盖已经存在的环境变量。"""
    file = path or Path.cwd() / ".env"
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    tts_provider: str
    tts_base_url: str
    tts_api_key: str
    tts_model: str
    tts_voice: str
    tts_speed: float
    output_dir: str
    album: str
    db_path: Path
    host: str
    port: int


def normalize_tts_provider(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"edge", "edge-tts", "microsoft"}:
        return "edge"
    return "siliconflow"


def load_settings() -> Settings:
    load_env()
    speed = os.getenv("TTS_SPEED", "1.0")
    try:
        speed_value = float(speed)
    except ValueError:
        speed_value = 1.0
    port_raw = os.getenv("PORT", "8765")
    try:
        port = int(port_raw)
    except ValueError:
        port = 8765
    db = os.getenv("TASK_DB", "")
    return Settings(
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        tts_provider=normalize_tts_provider(os.getenv("TTS_PROVIDER", "siliconflow")),
        tts_base_url=os.getenv("TTS_BASE_URL", "https://api.siliconflow.cn/v1"),
        tts_api_key=os.getenv("TTS_API_KEY", ""),
        tts_model=os.getenv("TTS_MODEL", "FunAudioLLM/CosyVoice2-0.5B"),
        tts_voice=os.getenv("TTS_VOICE", "FunAudioLLM/CosyVoice2-0.5B:diana"),
        tts_speed=speed_value,
        output_dir=os.getenv("OUTPUT_DIR", ""),
        album=os.getenv("ALBUM_NAME", ""),
        db_path=Path(db).expanduser() if db else Path.cwd() / "data" / "tasks.db",
        host=os.getenv("HOST", "127.0.0.1"),
        port=port,
    )


class SettingsError(ValueError):
    pass


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"••••{value[-4:]}"


def _keep_secret(incoming: str) -> bool:
    text = (incoming or "").strip()
    return not text or text.startswith("••••")


def _http_url(value: str, label: str) -> str:
    text = value.strip().rstrip("/")
    if not text.startswith(("http://", "https://")):
        raise SettingsError(f"{label}需要以 http:// 或 https:// 开头")
    return text


def write_env(path: Path, settings: Settings) -> None:
    """把可改的配置写回 .env，保留原来的注释和未管理的行。"""
    values = {
        "LLM_BASE_URL": settings.llm_base_url,
        "LLM_API_KEY": settings.llm_api_key,
        "LLM_MODEL": settings.llm_model,
        "TTS_PROVIDER": settings.tts_provider,
        "TTS_BASE_URL": settings.tts_base_url,
        "TTS_API_KEY": settings.tts_api_key,
        "TTS_MODEL": settings.tts_model,
        "TTS_VOICE": settings.tts_voice,
        "TTS_SPEED": str(settings.tts_speed),
        "OUTPUT_DIR": settings.output_dir,
        "ALBUM_NAME": settings.album,
    }
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    updated: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            updated.append(raw)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            updated.append(f"{key}={_format_env(values[key])}")
            seen.add(key)
        else:
            updated.append(raw)
    if seen and lines:
        updated.append("")
    for key, value in values.items():
        if key not in seen:
            updated.append(f"{key}={_format_env(value)}")
    text = "\n".join(updated).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    for key, value in values.items():
        os.environ[key] = value


def _format_env(value: str) -> str:
    if value == "" or any(char in value for char in ' #\n"\''):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


class SettingsStore:
    """页面上改过的配置写进 .env，并通知流水线立刻换用新配置。"""

    def __init__(self, settings: Settings, env_path: Path) -> None:
        self.settings = settings
        self.env_path = env_path
        self.on_change: Callable[[Settings], None] | None = None
        self._lock = threading.Lock()

    def public(self) -> dict:
        current = self.settings
        return {
            "llm_base_url": current.llm_base_url,
            "llm_api_key_set": bool(current.llm_api_key),
            "llm_api_key_hint": mask_secret(current.llm_api_key),
            "llm_model": current.llm_model,
            "tts_provider": current.tts_provider,
            "tts_base_url": current.tts_base_url,
            "tts_api_key_set": bool(current.tts_api_key),
            "tts_api_key_hint": mask_secret(current.tts_api_key),
            "tts_model": current.tts_model,
            "tts_voice": current.tts_voice,
            "tts_speed": current.tts_speed,
            "output_dir": current.output_dir,
            "album": current.album,
        }

    def update(self, patch: dict) -> dict:
        with self._lock:
            current = self.settings
            llm_key = current.llm_api_key if _keep_secret(str(patch.get("llm_api_key", ""))) else str(patch["llm_api_key"]).strip()
            tts_key = current.tts_api_key if _keep_secret(str(patch.get("tts_api_key", ""))) else str(patch["tts_api_key"]).strip()
            try:
                speed = float(patch.get("tts_speed", current.tts_speed))
            except (TypeError, ValueError) as exc:
                raise SettingsError("语速需要是数字") from exc
            if not 0.25 <= speed <= 4:
                raise SettingsError("语速需要在 0.25 到 4 之间")
            provider = normalize_tts_provider(str(patch.get("tts_provider", current.tts_provider)))
            model = str(patch.get("llm_model", "")).strip()
            voice_model = str(patch.get("tts_model", "")).strip()
            voice = str(patch.get("tts_voice", "")).strip()
            if not model or not voice:
                raise SettingsError("模型和音色不能为空")
            if provider == "edge":
                voice_model = voice_model or "edge"
                base_url = str(patch.get("tts_base_url", current.tts_base_url) or "").strip().rstrip("/")
                if base_url and not base_url.startswith(("http://", "https://")):
                    raise SettingsError("语音接口需要以 http:// 或 https:// 开头")
            else:
                if not voice_model:
                    raise SettingsError("模型和音色不能为空")
                base_url = _http_url(str(patch.get("tts_base_url", "")), "语音接口")
            updated = replace(
                current,
                llm_base_url=_http_url(str(patch.get("llm_base_url", "")), "大模型接口"),
                llm_api_key=llm_key,
                llm_model=model,
                tts_provider=provider,
                tts_base_url=base_url,
                tts_api_key=tts_key,
                tts_model=voice_model,
                tts_voice=voice,
                tts_speed=speed,
                output_dir=str(patch.get("output_dir", "")).strip(),
                album=current.album if "album" not in patch else str(patch.get("album") or "").strip(),
            )
            write_env(self.env_path, updated)
            self.settings = updated
            if self.on_change:
                self.on_change(updated)
            return self.public()
