from __future__ import annotations

from collections.abc import Callable

import httpx

from youtube2podcast.cancel import TaskCancelled, active_cancel


class ContentBlocked(RuntimeError):
    """模型拒绝写出这一段，通常是内容被判为敏感。"""


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
            body = response.text[:500]
            if response.status_code == 422 and ("sensitive" in body.lower() or "1027" in body):
                raise ContentBlocked("模型拒绝写出这一段，内容被判为敏感")
            raise RuntimeError(f"大模型返回错误 {response.status_code}：{body[:200]}")
        return chat_text(response.json())

    return complete


def chat_text(data) -> str:
    """从不同厂商的聊天响应里取出正文。取不到时返回空字符串，由调用方决定如何回退。"""
    if not isinstance(data, dict):
        return data if isinstance(data, str) else ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        for key in ("content", "text", "reply"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return ""
    choice = choices[0]
    if isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.append(str(part.get("text") or part.get("content") or ""))
            return "".join(parts)
    text = choice.get("text")
    return text if isinstance(text, str) else ""
