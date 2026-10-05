import os

from fastapi.testclient import TestClient

from youtube2podcast.config import Settings, SettingsStore
from youtube2podcast.runtime import AppRuntime
from youtube2podcast.server import JobRunner, create_app


def _restore_env(before: dict) -> None:
    keys = {
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "TTS_PROVIDER",
        "TTS_BASE_URL",
        "TTS_API_KEY",
        "TTS_MODEL",
        "TTS_VOICE",
        "TTS_SPEED",
        "OUTPUT_DIR",
        "ALBUM_NAME",
    }
    for key in keys:
        if key in before:
            os.environ[key] = before[key]
        else:
            os.environ.pop(key, None)


def _settings(**kwargs) -> Settings:
    data = dict(
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-old-key",
        llm_model="gpt-4o-mini",
        tts_provider="siliconflow",
        tts_base_url="https://api.siliconflow.cn/v1",
        tts_api_key="tts-old",
        tts_model="FunAudioLLM/CosyVoice2-0.5B",
        tts_voice="FunAudioLLM/CosyVoice2-0.5B:diana",
        tts_speed=1.0,
        output_dir="/listen",
        album="机器学习",
        db_path=__import__("pathlib").Path("/tmp/unused.db"),
        host="127.0.0.1",
        port=8765,
    )
    data.update(kwargs)
    return Settings(**data)


def test_blank_secret_keeps_the_saved_key_and_rewrites_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep\nPORT=9000\nLLM_MODEL=old\n", encoding="utf-8")
    before = os.environ.copy()
    seen = []
    store = SettingsStore(_settings(), env)
    store.on_change = lambda current: seen.append(current.llm_model)
    try:
        saved = store.update(
            {
                "llm_base_url": "https://open.bigmodel.cn/api/paas/v4",
                "llm_api_key": "",
                "llm_model": "glm-4-flash",
                "tts_provider": "siliconflow",
                "tts_base_url": "https://api.siliconflow.cn/v1",
                "tts_api_key": "••••old",
                "tts_model": "FunAudioLLM/CosyVoice2-0.5B",
                "tts_voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
                "tts_speed": 1.2,
                "output_dir": "/tmp/learn",
                "album": "新专辑",
            }
        )
    finally:
        _restore_env(before)
    text = env.read_text(encoding="utf-8")
    assert "PORT=9000" in text
    assert "# keep" in text
    assert "LLM_MODEL=glm-4-flash" in text
    assert "LLM_API_KEY=sk-old-key" in text
    assert "TTS_API_KEY=tts-old" in text
    assert saved["llm_api_key_hint"] == "••••-key"
    assert saved["tts_provider"] == "siliconflow"
    assert "sk-old-key" not in str(saved)
    assert seen == ["glm-4-flash"]


def test_edge_provider_skips_tts_api_url_requirement(tmp_path):
    env = tmp_path / ".env"
    before = os.environ.copy()
    store = SettingsStore(_settings(), env)
    try:
        saved = store.update(
            {
                "llm_base_url": "https://api.openai.com/v1",
                "llm_api_key": "",
                "llm_model": "gpt-4o-mini",
                "tts_provider": "edge",
                "tts_base_url": "",
                "tts_api_key": "",
                "tts_model": "edge",
                "tts_voice": "zh-CN-XiaoxiaoNeural",
                "tts_speed": 1,
                "output_dir": "/tmp/learn",
            }
        )
    finally:
        _restore_env(before)
    assert saved["tts_provider"] == "edge"
    assert saved["tts_voice"] == "zh-CN-XiaoxiaoNeural"


def test_settings_page_updates_the_next_pipeline(tmp_path):
    env = tmp_path / ".env"
    runtime = AppRuntime(_settings(db_path=tmp_path / "tasks.db"), env)
    app = create_app(runtime.store, JobRunner(runtime.store, runtime.run_task), settings_store=runtime.settings_store)
    before = os.environ.copy()
    try:
        with TestClient(app) as client:
            current = client.get("/api/settings").json()
            assert current["llm_model"] == "gpt-4o-mini"
            page = client.get("/settings").text
            assert "保存并生效" in page
            assert "speaker-mode" not in page
            assert "文本分人" in page
            saved = client.put(
                "/api/settings",
                json={
                    "llm_base_url": "https://api.openai.com/v1",
                    "llm_api_key": "sk-new-key",
                    "llm_model": "gpt-4.1-mini",
                    "tts_provider": "siliconflow",
                    "tts_base_url": "https://api.siliconflow.cn/v1",
                    "tts_api_key": "",
                    "tts_model": "FunAudioLLM/CosyVoice2-0.5B",
                    "tts_voice": "FunAudioLLM/CosyVoice2-0.5B:diana",
                    "tts_speed": 1,
                    "output_dir": "/tmp/out",
                    "album": "改后的专辑",
                },
            )
            assert saved.status_code == 200
            body = saved.json()
            assert body["llm_model"] == "gpt-4.1-mini"
            assert body["llm_api_key_hint"].endswith("key")
            assert runtime._pipeline.speaker.api_key == "tts-old"
            assert _closed_model(runtime) == "gpt-4.1-mini"
            assert client.get("/api/config").json()["album"] == "改后的专辑"
            rejected = client.put(
                "/api/settings",
                json={
                    "llm_base_url": "not-a-url",
                    "llm_model": "x",
                    "tts_base_url": "https://api.siliconflow.cn/v1",
                    "tts_model": "m",
                    "tts_voice": "v",
                    "tts_speed": 1,
                },
            )
            assert rejected.status_code == 400
    finally:
        _restore_env(before)


def _closed_model(runtime) -> str:
    function = runtime._pipeline.translator.complete
    names = function.__code__.co_freevars
    values = [cell.cell_contents for cell in function.__closure__]
    return dict(zip(names, values))["model"]
