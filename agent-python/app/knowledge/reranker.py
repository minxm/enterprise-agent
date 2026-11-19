"""Score candidate passages with a reranker.

A reranker does not search, write to the database, or draft an answer. The
SiliconFlow client calls POST /rerank. It does not call the embedding endpoint
and does not log the API key or the passage text.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from app.core.config import RERANKER_MODEL_NAME, RERANKER_MODELS, Settings

_MAX_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 2.0
_INSTRUCTION = "Rank enterprise knowledge passages by how well they answer the query."


class RerankerError(RuntimeError):
    """Base error for reranker calls."""


class RerankConfigurationError(RerankerError):
    """The client is missing a key or was given an invalid local input."""


class RerankAPIError(RerankerError):
    """The provider request failed. The message never includes the API key."""


class RerankResponseError(RerankerError):
    """The provider response cannot be used as scores."""


@dataclass(frozen=True)
class RerankScore:
    index: int
    score: float


class Reranker:
    """Score documents and return one finite score per input index."""

    def score(self, query: str, documents: Sequence[str]) -> list[RerankScore]:
        raise NotImplementedError


class SiliconFlowReranker(Reranker):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = RERANKER_MODEL_NAME,
        timeout_seconds: float = 30,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        normalized_model = model.strip()
        if normalized_model not in RERANKER_MODELS:
            raise RerankConfigurationError("RERANKER_MODEL 必须是 Qwen/Qwen3-Reranker-0.6B、4B 或 8B")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise RerankConfigurationError("RERANKER_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        if not 0 < float(timeout_seconds) <= 120:
            raise RerankConfigurationError("RERANKER_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        normalized_url = base_url.strip().rstrip("/")
        if not (normalized_url.startswith("https://") or normalized_url.startswith("http://")):
            raise RerankConfigurationError("RERANKER_BASE_URL 必须是 http 或 https 地址")
        self.model_name = normalized_model
        self._api_key = api_key.strip()
        self._url = f"{normalized_url}/rerank"
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
    ) -> SiliconFlowReranker:
        return cls(
            api_key=settings.reranker_api_key or settings.siliconflow_api_key,
            base_url=settings.reranker_base_url or settings.siliconflow_base_url,
            model=settings.reranker_model,
            timeout_seconds=settings.reranker_timeout_seconds,
            client=client,
            sleeper=sleeper,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def score(self, query: str, documents: Sequence[str]) -> list[RerankScore]:
        question = _require_query(query)
        passages = _require_documents(documents)
        if not self._api_key:
            raise RerankConfigurationError("Reranker API Key 未配置")
        last_error: RerankAPIError | None = None
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
                        "query": question,
                        "documents": passages,
                        "instruction": _INSTRUCTION,
                        "top_n": len(passages),
                        "return_documents": False,
                    },
                )
            except httpx.TimeoutException:
                last_error = RerankAPIError("Rerank 请求超时")
            except httpx.TransportError:
                last_error = RerankAPIError("Rerank 连接失败")
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = _status_error(response)
                elif response.status_code != 200:
                    raise _status_error(response)
                else:
                    return _parse_scores(response, expected_count=len(passages))
            if attempt + 1 < _MAX_ATTEMPTS:
                self._sleeper(min(_MAX_BACKOFF_SECONDS, 0.2 * (2**attempt)))
        assert last_error is not None
        raise last_error


def _require_query(query: object) -> str:
    if not isinstance(query, str) or not query.strip():
        raise RerankConfigurationError("查询内容不能为空")
    return query.strip()


def _require_documents(documents: object) -> list[str]:
    if isinstance(documents, str) or not isinstance(documents, Sequence):
        raise RerankConfigurationError("Rerank 候选必须是字符串列表")
    items = list(documents)
    if not items:
        raise RerankConfigurationError("Rerank 候选不能为空")
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise RerankConfigurationError("Rerank 候选不能为空")
    return items


def _parse_scores(response: httpx.Response, *, expected_count: int) -> list[RerankScore]:
    try:
        payload = json.loads(response.text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RerankResponseError) as exc:
        if isinstance(exc, RerankResponseError):
            raise
        raise RerankResponseError("响应不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise RerankResponseError("响应格式不正确")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != expected_count:
        raise RerankResponseError("响应条数不正确")
    found: set[int] = set()
    scores: list[RerankScore] = []
    for item in results:
        if not isinstance(item, dict) or "index" not in item:
            raise RerankResponseError("响应缺少 index")
        index = item.get("index")
        if type(index) is not int or not 0 <= index < expected_count:
            raise RerankResponseError("响应 index 不合法")
        if index in found:
            raise RerankResponseError("响应 index 重复")
        found.add(index)
        scores.append(RerankScore(index, _finite_score(item.get("relevance_score"))))
    if len(found) != expected_count:
        raise RerankResponseError("响应 index 不完整")
    return scores


def _finite_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RerankResponseError("响应包含非法分数")
    score = float(value)
    if not math.isfinite(score):
        raise RerankResponseError("响应包含非法分数")
    return score


def _reject_constant(name: str) -> float:
    del name
    raise RerankResponseError("响应包含非法分数")


def _status_error(response: httpx.Response) -> RerankAPIError:
    trace_id = response.headers.get("x-siliconcloud-trace-id", "")
    message = f"Rerank 请求失败，HTTP {response.status_code}"
    if trace_id and _safe_trace_id(trace_id):
        message = f"{message}，trace_id={trace_id}"
    return RerankAPIError(message)


def _safe_trace_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(character.isalnum() or character in "-_" for character in value)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
