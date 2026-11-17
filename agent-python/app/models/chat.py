"""Call SiliconFlow's OpenAI-compatible chat API.

The client shares the embedding provider's base URL and API key. It does not
log request or response bodies, and error messages never include the key.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from app.core.config import CHAT_MODEL_NAME, EMBEDDING_MODEL_NAME, Settings

_MAX_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 2.0
_TEMPERATURE = 0.1


class ChatError(RuntimeError):
    """Base error for chat calls."""


class ChatConfigurationError(ChatError):
    """The client is missing a key or was given an invalid local input."""


class ChatAPIError(ChatError):
    """The provider request failed. The message never includes the API key."""


class ChatResponseError(ChatError):
    """The provider response cannot be used as an answer."""


class ChatClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = CHAT_MODEL_NAME,
        timeout_seconds: float = 60,
        max_tokens: int = 1024,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model or len(normalized_model) > 200:
            raise ChatConfigurationError("CHAT_MODEL 不能为空")
        if normalized_model == EMBEDDING_MODEL_NAME:
            raise ChatConfigurationError("CHAT_MODEL 不能使用 Embedding 模型")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ChatConfigurationError("CHAT_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        if not 0 < float(timeout_seconds) <= 120:
            raise ChatConfigurationError("CHAT_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise ChatConfigurationError("CHAT_MAX_TOKENS 必须在 1 到 4096 之间")
        normalized_url = base_url.strip().rstrip("/")
        if not (normalized_url.startswith("https://") or normalized_url.startswith("http://")):
            raise ChatConfigurationError("SILICONFLOW_BASE_URL 必须是 http 或 https 地址")
        self.model_name = normalized_model
        self._api_key = api_key.strip()
        self._url = f"{normalized_url}/chat/completions"
        self._max_tokens = max_tokens
        self._sleeper = sleeper or _sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(float(timeout_seconds)))

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> ChatClient:
        return cls(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            model=settings.chat_model,
            timeout_seconds=settings.chat_timeout_seconds,
            max_tokens=settings.chat_max_tokens,
            client=client,
            sleeper=sleeper,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(self, *, system: str, user: str) -> str:
        if not isinstance(system, str) or not system.strip():
            raise ChatConfigurationError("系统提示不能为空")
        if not isinstance(user, str) or not user.strip():
            raise ChatConfigurationError("用户提示不能为空")
        if not self._api_key:
            raise ChatConfigurationError("SILICONFLOW_API_KEY 未配置")
        last_error: ChatAPIError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._client.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model_name,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": _TEMPERATURE,
                        "max_tokens": self._max_tokens,
                    },
                )
            except httpx.TimeoutException:
                last_error = ChatAPIError("Chat 请求超时")
            except httpx.TransportError:
                last_error = ChatAPIError("Chat 连接失败")
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = _status_error(response)
                elif response.status_code != 200:
                    raise _status_error(response)
                else:
                    return _parse_completion(response, model=self.model_name)
            if attempt + 1 < _MAX_ATTEMPTS:
                self._sleeper(min(_MAX_BACKOFF_SECONDS, 0.2 * (2**attempt)))
        assert last_error is not None
        raise last_error


def _parse_completion(response: httpx.Response, *, model: str) -> str:
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        raise ChatResponseError("响应不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise ChatResponseError("响应格式不正确")
    if payload.get("model") != model:
        raise ChatResponseError("响应模型与请求不一致")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ChatResponseError("响应缺少 choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ChatResponseError("响应缺少 message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ChatResponseError("模型输出为空")
    return content.strip()


def _status_error(response: httpx.Response) -> ChatAPIError:
    trace_id = response.headers.get("x-siliconcloud-trace-id", "")
    message = f"Chat 请求失败，HTTP {response.status_code}"
    if trace_id and _safe_trace_id(trace_id):
        message = f"{message}，trace_id={trace_id}"
    return ChatAPIError(message)


def _safe_trace_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(character.isalnum() or character in "-_" for character in value)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
