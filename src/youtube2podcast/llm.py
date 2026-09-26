from __future__ import annotations

from collections.abc import Callable

import httpx

from youtube2podcast.cancel import TaskCancelled, active_cancel


def build_complete(base_url: str, api_key: str, model: str, *, temperature: float = 0) -> Callable[[str, str], str]:
    """返回一个调用 OpenAI 兼容聊天接口的函数。"""
    if not api_key or api_key.startswith("sk-xxxx"):

        def missing(system: str, user: str) -> str:
            raise RuntimeError("未配置有效的 LLM_API_KEY")

        return missing

    endpoint = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

    def complete(system: str, user: str) -> str:
        cancel = active_cancel.get()
        if cancel is not None and cancel.is_set():
            raise TaskCancelled()
        payload = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        with httpx.Client(timeout=180) as http:
            if cancel is not None and hasattr(cancel, "track_client"):
                cancel.track_client(http)
            try:
                response = http.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                if cancel is not None and cancel.is_set():
                    raise TaskCancelled() from exc
                raise RuntimeError(f"无法连接大模型服务：{exc}") from exc
            finally:
                if cancel is not None and hasattr(cancel, "untrack_client"):
                    cancel.untrack_client(http)
        if response.status_code in {401, 403}:
            raise RuntimeError(f"大模型 API Key 无效：{response.text[:200]}")
        if response.status_code >= 400:
            raise RuntimeError(f"大模型返回错误 {response.status_code}：{response.text[:200]}")
        data = response.json()
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    return complete
