import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import EMBEDDING_MODEL_NAME, Settings
from app.knowledge.embeddings import (
    EmbeddingAPIError,
    EmbeddingClient,
    EmbeddingConfigurationError,
    EmbeddingResponseError,
)

API_KEY = "sk-test-not-a-real-key"
SECRET_TEXT = "UNIQUE-CHUNK-TEXT-SHOULD-NOT-LEAK"


def test_single_text_request():
    seen: list[dict[str, object]] = []
    client = _client(_ok(seen, texts=1))

    vectors = client.embed("第一段")

    assert seen[0]["input"] == ["第一段"]
    assert seen[0]["model"] == EMBEDDING_MODEL_NAME
    assert seen[0]["encoding_format"] == "float"
    assert seen[0]["dimensions"] == 1024
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024
    assert vectors[0][0] == 0.1


def test_multiple_texts_keep_input_order():
    seen: list[dict[str, object]] = []
    client = _client(_ok(seen, texts=2, reverse=True))

    vectors = client.embed(["第一段", "第二段"])

    assert [item[0] for item in vectors] == [0.1, 0.2]


def test_batches_are_split_and_recombined():
    seen: list[dict[str, object]] = []
    client = _client(_ok(seen, texts=None, reverse=True), batch_size=2)

    vectors = client.embed(["a", "b", "c", "d", "e"])

    assert [body["input"] for body in seen] == [["a", "b"], ["c", "d"], ["e"]]
    assert [item[0] for item in vectors] == [0.1, 0.2, 0.1, 0.2, 0.1]


def test_scrambled_indexes_are_restored_inside_each_batch():
    seen: list[dict[str, object]] = []
    client = _client(_ok(seen, texts=3, reverse=True))

    vectors = client.embed(["甲", "乙", "丙"])

    assert [item[0] for item in vectors] == [0.1, 0.2, 0.3]


def test_missing_api_key_does_not_call_the_provider():
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    client = _client(handler, api_key="")

    with pytest.raises(EmbeddingConfigurationError, match="SILICONFLOW_API_KEY"):
        client.embed("第一段")
    assert called is False


@pytest.mark.parametrize("value", ["", "   \n", ["ok", "  "], [], 12, ["ok", 1]])
def test_empty_or_invalid_input_is_rejected(value):
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    client = _client(handler)
    with pytest.raises(EmbeddingConfigurationError):
        client.embed(value)  # type: ignore[arg-type]
    assert called is False


def test_response_count_must_match():
    client = _client(lambda _request: _response([_vector(0.1)]))
    with pytest.raises(EmbeddingResponseError, match="条数"):
        client.embed(["第一段", "第二段"])


def test_missing_index_is_rejected():
    body = {
        "model": EMBEDDING_MODEL_NAME,
        "data": [{"embedding": _vector(0.1)}],
    }
    client = _client(lambda _request: httpx.Response(200, json=body))
    with pytest.raises(EmbeddingResponseError, match="index"):
        client.embed("第一段")


def test_duplicate_index_is_rejected():
    body = {
        "model": EMBEDDING_MODEL_NAME,
        "data": [
            {"index": 0, "embedding": _vector(0.1)},
            {"index": 0, "embedding": _vector(0.2)},
        ],
    }
    client = _client(lambda _request: httpx.Response(200, json=body))
    with pytest.raises(EmbeddingResponseError, match="重复"):
        client.embed(["第一段", "第二段"])


def test_wrong_dimension_is_rejected():
    body = {
        "model": EMBEDDING_MODEL_NAME,
        "data": [{"index": 0, "embedding": [0.1, 0.2]}],
    }
    client = _client(lambda _request: httpx.Response(200, json=body))
    with pytest.raises(EmbeddingResponseError, match="维度"):
        client.embed("第一段")


def test_nan_is_rejected():
    client = _client(lambda _request: httpx.Response(200, content=_raw_constant("NaN")))
    with pytest.raises(EmbeddingResponseError, match="非法数值"):
        client.embed("第一段")


def test_infinity_is_rejected():
    client = _client(lambda _request: httpx.Response(200, content=_raw_constant("Infinity")))
    with pytest.raises(EmbeddingResponseError, match="非法数值"):
        client.embed("第一段")


def test_response_model_must_match_the_request():
    body = {"model": "other-model", "data": [{"index": 0, "embedding": _vector(0.1)}]}
    client = _client(lambda _request: httpx.Response(200, json=body))
    with pytest.raises(EmbeddingResponseError, match="模型"):
        client.embed("第一段")


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("timeout", "超时"),
        ("transport", "连接失败"),
        (429, "429"),
        (500, "500"),
    ],
)
def test_transient_failures_are_retried(failure: str | int, message: str):
    calls = {"count": 0}
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out")
            if failure == "transport":
                raise httpx.ConnectError("offline")
            return httpx.Response(int(failure), json={"message": SECRET_TEXT, "key": API_KEY})
        return _response([_vector(0.4)], request)

    client = _client(handler, sleeper=delays.append)
    vectors = client.embed(SECRET_TEXT)

    assert calls["count"] == 3
    assert delays == [0.2, 0.4]
    assert vectors[0][0] == 0.4
    assert message


def test_retries_stop_after_three_attempts():
    calls = {"count": 0}
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ReadTimeout("timed out")

    client = _client(handler, sleeper=delays.append)
    with pytest.raises(EmbeddingAPIError, match="超时") as caught:
        client.embed(SECRET_TEXT)
    assert calls["count"] == 3
    assert delays == [0.2, 0.4]
    assert API_KEY not in str(caught.value)
    assert SECRET_TEXT not in str(caught.value)


@pytest.mark.parametrize("status", [401, 400])
def test_client_errors_are_not_retried(status: int, caplog):
    calls = {"count": 0}
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            status,
            headers={"x-siliconcloud-trace-id": "trace-1"},
            json={"message": f"{API_KEY} {SECRET_TEXT}"},
        )

    client = _client(handler, sleeper=delays.append)
    with caplog.at_level("DEBUG"):
        with pytest.raises(EmbeddingAPIError, match=f"HTTP {status}") as caught:
            client.embed(SECRET_TEXT)
    assert calls["count"] == 1
    assert delays == []
    assert "trace_id=trace-1" in str(caught.value)
    assert API_KEY not in str(caught.value)
    assert SECRET_TEXT not in str(caught.value)
    assert API_KEY not in caplog.text
    assert SECRET_TEXT not in caplog.text


def test_settings_reject_a_different_dimension_or_model():
    with pytest.raises(ValidationError, match="1024"):
        Settings(embedding_dimensions=768)
    with pytest.raises(ValidationError, match="Qwen3-Embedding-0.6B"):
        Settings(embedding_model="other-model")
    with pytest.raises(EmbeddingConfigurationError):
        EmbeddingClient(api_key="x", dimensions=768)


def test_base_url_trailing_slash_is_removed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _response([_vector(0.1)], request)

    settings = Settings(
        siliconflow_api_key=API_KEY,
        siliconflow_base_url="https://api.siliconflow.cn/v1/",
    )
    client = EmbeddingClient.from_settings(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    client.embed("第一段")
    assert seen == ["https://api.siliconflow.cn/v1/embeddings"]
    assert settings.siliconflow_base_url == "https://api.siliconflow.cn/v1"


def _client(handler, *, api_key: str = API_KEY, batch_size: int = 32, sleeper=None):
    if sleeper is None:
        def sleeper(_seconds: float) -> None:
            return None
    return EmbeddingClient(
        api_key=api_key,
        batch_size=batch_size,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=sleeper,
    )


def _ok(seen: list[dict[str, object]], *, texts: int | None, reverse: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        count = len(body["input"]) if texts is None else texts
        vectors = [_vector((index + 1) / 10) for index in range(count)]
        return _response(vectors, request, reverse=reverse)

    return handler


def _response(vectors: list[list[float]], request: httpx.Request | None = None, *, reverse: bool = False):
    del request
    data = [
        {"object": "embedding", "index": index, "embedding": vector}
        for index, vector in enumerate(vectors)
    ]
    if reverse:
        data.reverse()
    return httpx.Response(
        200,
        json={"object": "list", "model": EMBEDDING_MODEL_NAME, "data": data},
    )


def _vector(seed: float) -> list[float]:
    return [seed] * 1024


def _raw_constant(token: str) -> bytes:
    values = ", ".join([token] + ["0.1"] * 1023)
    body = {
        "model": EMBEDDING_MODEL_NAME,
        "data": [{"index": 0, "embedding": []}],
    }
    encoded = json.dumps(body).replace("[]", f"[{values}]")
    return encoded.encode("utf-8")
