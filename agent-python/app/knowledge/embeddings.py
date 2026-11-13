"""Call SiliconFlow's OpenAI-compatible embedding API.

The client validates every vector before returning it. It does not write to
the database and does not log request or response bodies.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable

import httpx

from app.core.config import EMBEDDING_MODEL_NAME, EMBEDDING_WIDTH, Settings

_MAX_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 2.0


class EmbeddingError(RuntimeError):
    """Base error for embedding calls."""


class EmbeddingConfigurationError(EmbeddingError):
    """The client is missing a key or was given an invalid local input."""


class EmbeddingAPIError(EmbeddingError):
    """The provider request failed. The message never includes the API key."""


class EmbeddingResponseError(EmbeddingError):
    """The provider response cannot be used as vectors."""


class EmbeddingClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = EMBEDDING_MODEL_NAME,
        dimensions: int = EMBEDDING_WIDTH,
        timeout_seconds: float = 30,
        batch_size: int = 32,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if dimensions != EMBEDDING_WIDTH:
            raise EmbeddingConfigurationError("EMBEDDING_DIMENSIONS 必须是 1024")
        if model != EMBEDDING_MODEL_NAME:
            raise EmbeddingConfigurationError("EMBEDDING_MODEL 必须是 Qwen/Qwen3-Embedding-0.6B")
        if type(batch_size) is not int or not 1 <= batch_size <= 32:
            raise EmbeddingConfigurationError("EMBEDDING_BATCH_SIZE 必须在 1 到 32 之间")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise EmbeddingConfigurationError("EMBEDDING_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        if not 0 < float(timeout_seconds) <= 120:
            raise EmbeddingConfigurationError("EMBEDDING_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        normalized_url = base_url.strip().rstrip("/")
        if not (normalized_url.startswith("https://") or normalized_url.startswith("http://")):
            raise EmbeddingConfigurationError("SILICONFLOW_BASE_URL 必须是 http 或 https 地址")
        self.model_name = model
        self._api_key = api_key.strip()
        self._url = f"{normalized_url}/embeddings"
        self._batch_size = batch_size
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
    ) -> EmbeddingClient:
        return cls(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout_seconds=settings.embedding_timeout_seconds,
            batch_size=settings.embedding_batch_size,
            client=client,
            sleeper=sleeper,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def embed(self, texts: str | list[str]) -> list[list[float]]:
        items = _normalize_texts(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(items), self._batch_size):
            vectors.extend(self._embed_batch(items[start : start + self._batch_size]))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise EmbeddingConfigurationError("SILICONFLOW_API_KEY 未配置")
        last_error: EmbeddingAPIError | None = None
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
                        "input": batch,
                        "encoding_format": "float",
                        "dimensions": EMBEDDING_WIDTH,
                    },
                )
            except httpx.TimeoutException:
                last_error = EmbeddingAPIError("Embedding 请求超时")
            except httpx.TransportError:
                last_error = EmbeddingAPIError("Embedding 连接失败")
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = _status_error(response)
                elif response.status_code != 200:
                    raise _status_error(response)
                else:
                    return _parse_embeddings(response, expected_count=len(batch), model=self.model_name)
            if attempt + 1 < _MAX_ATTEMPTS:
                self._sleeper(min(_MAX_BACKOFF_SECONDS, 0.2 * (2**attempt)))
        assert last_error is not None
        raise last_error


def format_vector(values: list[float]) -> str:
    """Serialize an already validated vector for a parameterized SQL cast."""
    checked = _finite_vector(values)
    return json.dumps(checked, allow_nan=False, separators=(",", ":"))


def _normalize_texts(texts: str | list[str]) -> list[str]:
    if isinstance(texts, str):
        items = [texts]
    elif isinstance(texts, list):
        items = texts
    else:
        raise EmbeddingConfigurationError("Embedding 输入必须是字符串或字符串列表")
    if not items:
        raise EmbeddingConfigurationError("Embedding 输入不能为空")
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise EmbeddingConfigurationError("Embedding 输入不能为空")
    return items


def _parse_embeddings(response: httpx.Response, *, expected_count: int, model: str) -> list[list[float]]:
    try:
        payload = json.loads(response.text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, EmbeddingResponseError) as exc:
        if isinstance(exc, EmbeddingResponseError):
            raise
        raise EmbeddingResponseError("响应不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise EmbeddingResponseError("响应格式不正确")
    if payload.get("model") != model:
        raise EmbeddingResponseError("响应模型与请求不一致")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise EmbeddingResponseError("响应条数不正确")
    ordered: list[list[float] | None] = [None] * expected_count
    for item in data:
        if not isinstance(item, dict) or "index" not in item:
            raise EmbeddingResponseError("响应缺少 index")
        index = item.get("index")
        if type(index) is not int or not 0 <= index < expected_count:
            raise EmbeddingResponseError("响应 index 不合法")
        if ordered[index] is not None:
            raise EmbeddingResponseError("响应 index 重复")
        ordered[index] = _finite_vector(item.get("embedding"))
    if any(vector is None for vector in ordered):
        raise EmbeddingResponseError("响应 index 不完整")
    return [vector for vector in ordered if vector is not None]


def _finite_vector(value: object) -> list[float]:
    if not isinstance(value, list) or len(value) != EMBEDDING_WIDTH:
        raise EmbeddingResponseError("向量维度不正确")
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise EmbeddingResponseError("向量包含非法数值")
        number = float(item)
        if not math.isfinite(number):
            raise EmbeddingResponseError("向量包含非法数值")
        vector.append(number)
    return vector


def _reject_constant(name: str) -> float:
    raise EmbeddingResponseError("向量包含非法数值")


def _status_error(response: httpx.Response) -> EmbeddingAPIError:
    trace_id = response.headers.get("x-siliconcloud-trace-id", "")
    message = f"Embedding 请求失败，HTTP {response.status_code}"
    if trace_id and _safe_trace_id(trace_id):
        message = f"{message}，trace_id={trace_id}"
    return EmbeddingAPIError(message)


def _safe_trace_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(character.isalnum() or character in "-_" for character in value)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
