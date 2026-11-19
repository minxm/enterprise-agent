"""Call a separate judge model. This client does not answer user questions.

Errors name the HTTP status or timeout only. They do not include the API key,
the answer, or the source text.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from app.core.config import CHAT_MODEL_NAME, EMBEDDING_MODEL_NAME, Settings

_MAX_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 2.0


class JudgeError(RuntimeError):
    """Base error for judge calls."""


class JudgeConfigurationError(JudgeError):
    """The judge client is missing a key or was given an invalid local input."""


class JudgeAPIError(JudgeError):
    """The provider request failed. The message never includes the API key."""


class JudgeResponseError(JudgeError):
    """The provider response is not a usable judgement."""


class JudgeClient:
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
            raise JudgeConfigurationError("RAG_EVAL_JUDGE_MODEL 不能为空")
        if normalized_model == EMBEDDING_MODEL_NAME:
            raise JudgeConfigurationError("RAG_EVAL_JUDGE_MODEL 不能使用 Embedding 模型")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise JudgeConfigurationError("RAG_EVAL_JUDGE_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        if not 0 < float(timeout_seconds) <= 120:
            raise JudgeConfigurationError("RAG_EVAL_JUDGE_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        normalized_url = base_url.strip().rstrip("/")
        if not (normalized_url.startswith("https://") or normalized_url.startswith("http://")):
            raise JudgeConfigurationError("SILICONFLOW_BASE_URL 必须是 http 或 https 地址")
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
    ) -> JudgeClient:
        if not settings.rag_eval_judge_enabled:
            raise JudgeConfigurationError("RAG_EVAL_JUDGE_ENABLED 未开启")
        return cls(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            model=settings.rag_eval_judge_model,
            timeout_seconds=settings.rag_eval_judge_timeout_seconds,
            client=client,
            sleeper=sleeper,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete_json(self, *, system: str, user: str) -> dict[str, object]:
        if not isinstance(system, str) or not system.strip():
            raise JudgeConfigurationError("Judge 系统提示不能为空")
        if not isinstance(user, str) or not user.strip():
            raise JudgeConfigurationError("Judge 用户提示不能为空")
        if not self._api_key:
            raise JudgeConfigurationError("SILICONFLOW_API_KEY 未配置")
        last_error: JudgeAPIError | None = None
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
                        "temperature": 0,
                        "max_tokens": self._max_tokens,
                        "response_format": {"type": "json_object"},
                    },
                )
            except httpx.TimeoutException:
                last_error = JudgeAPIError("Judge 请求超时")
            except httpx.TransportError:
                last_error = JudgeAPIError("Judge 连接失败")
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = _status_error(response)
                elif response.status_code != 200:
                    raise _status_error(response)
                else:
                    return _parse_json_content(response, model=self.model_name)
            if attempt + 1 < _MAX_ATTEMPTS:
                self._sleeper(min(_MAX_BACKOFF_SECONDS, 0.2 * (2**attempt)))
        assert last_error is not None
        raise last_error


def _parse_json_content(response: httpx.Response, *, model: str) -> dict[str, object]:
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        raise JudgeResponseError("Judge 响应不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise JudgeResponseError("Judge 响应格式不正确")
    if payload.get("model") != model:
        raise JudgeResponseError("Judge 响应模型与请求不一致")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise JudgeResponseError("Judge 响应缺少 choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise JudgeResponseError("Judge 响应缺少 message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise JudgeResponseError("Judge 输出为空")
    return _load_object(content)


def _load_object(content: str) -> dict[str, object]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise JudgeResponseError("Judge 输出不是 JSON 对象")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        raise JudgeResponseError("Judge 输出不是合法 JSON") from None
    if not isinstance(parsed, dict):
        raise JudgeResponseError("Judge 输出不是 JSON 对象")
    return parsed


def _status_error(response: httpx.Response) -> JudgeAPIError:
    trace_id = response.headers.get("x-siliconcloud-trace-id", "")
    message = f"Judge 请求失败，HTTP {response.status_code}"
    if trace_id and 1 <= len(trace_id) <= 128 and all(character.isalnum() or character in "-_" for character in trace_id):
        message = f"{message}，trace_id={trace_id}"
    return JudgeAPIError(message)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
