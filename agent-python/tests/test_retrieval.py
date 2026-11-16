import hashlib
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from psycopg import OperationalError
from psycopg_pool import ConnectionPool

from app.core.config import EMBEDDING_MODEL_NAME, EMBEDDING_WIDTH, Settings
from app.knowledge.embeddings import (
    EmbeddingAPIError,
    EmbeddingClient,
    EmbeddingConfigurationError,
    EmbeddingResponseError,
    format_vector,
)
from app.knowledge.retrieval import RetrievalHit, RetrievalValidationError, VectorRetriever
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, tenant_transaction

API_KEY = "sk-test-not-a-real-key"
SECRET_QUESTION = "UNIQUE-QUESTION-SHOULD-NOT-LEAK"
_QUERY_TEXT = (
    "Instruct: Retrieve relevant enterprise knowledge passages that answer the user's question.\n"
    f"Query: {SECRET_QUESTION}"
)


def test_embed_query_sends_the_retrieval_instruction():
    seen: list[dict[str, object]] = []
    client = _http_client(_ok(seen, seed=0.25))

    vector = client.embed_query(f"  {SECRET_QUESTION}  ")

    assert seen[0]["input"] == [_QUERY_TEXT]
    assert seen[0]["model"] == EMBEDDING_MODEL_NAME
    assert seen[0]["encoding_format"] == "float"
    assert seen[0]["dimensions"] == 1024
    assert len(vector) == 1024
    assert vector[0] == 0.25
    assert all(math.isfinite(value) for value in vector)


def test_document_embed_does_not_add_the_query_instruction():
    seen: list[dict[str, object]] = []
    client = _http_client(_ok(seen, seed=0.1))

    client.embed("员工报销需要发票")

    assert seen[0]["input"] == ["员工报销需要发票"]
    assert "Instruct:" not in seen[0]["input"][0]


@pytest.mark.parametrize("value", ["", " ", "\n\t", " \u3000 ", None, 12])
def test_embed_query_rejects_blank_input(value):
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    client = _http_client(handler)
    with pytest.raises(EmbeddingConfigurationError, match="查询内容不能为空"):
        client.embed_query(value)  # type: ignore[arg-type]
    assert called is False


def test_search_returns_matching_chunks_in_similarity_order(retrieval_db, caplog):
    tenant_id = uuid4()
    query_vector = _axis(0)
    document_id = _insert_document(
        retrieval_db.pool,
        tenant_id,
        [
            _chunk("orthogonal", _axis(1), char_start=20, char_end=30),
            _chunk("best-match", query_vector, char_start=4, char_end=14),
        ],
    )
    seen: list[dict[str, object]] = []
    pool_guard = _QueryCounter(retrieval_db.pool)
    try:
        with caplog.at_level("DEBUG"):
            hits = VectorRetriever(pool_guard, _http_client(_fixed(seen, query_vector))).search(
                tenant_id,
                f"  {SECRET_QUESTION}\n",
            )
        assert seen[0]["input"] == [_QUERY_TEXT]
        assert [hit.content for hit in hits] == ["best-match", "orthogonal"]
        assert hits[0].similarity > hits[1].similarity
        best = hits[0]
        assert isinstance(best, RetrievalHit)
        assert best.document_id == document_id
        assert isinstance(best.chunk_id, UUID)
        assert best.chunk_index == 1
        assert best.char_start == 4
        assert best.char_end == 14
        assert best.embedding_model == EMBEDDING_MODEL_NAME
        assert math.isclose(best.similarity, 1.0, rel_tol=1e-5, abs_tol=1e-5)
        assert math.isclose(hits[1].similarity, 0.0, rel_tol=1e-5, abs_tol=1e-5)
        assert SECRET_QUESTION not in caplog.text
        assert API_KEY not in caplog.text
        assert format_vector(query_vector) not in caplog.text
        assert pool_guard.calls == 1
    finally:
        _delete_document(retrieval_db.pool, tenant_id, document_id)


def test_top_k_limits_results(retrieval_db):
    tenant_id = uuid4()
    primaries = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    document_id = _insert_document(
        retrieval_db.pool,
        tenant_id,
        [_chunk(f"s-{primary:.1f}", _unit_vector(primary)) for primary in primaries],
    )
    retriever = VectorRetriever(retrieval_db.pool, _http_client(_fixed([], _axis(0))))
    try:
        default_hits = retriever.search(tenant_id, "付款条件")
        limited = retriever.search(tenant_id, "付款条件", top_k=2)
        capped = retriever.search(tenant_id, "付款条件", top_k=20)
        assert [hit.content for hit in default_hits] == ["s-1.0", "s-0.9", "s-0.8", "s-0.7", "s-0.6"]
        assert all(hit.similarity > later.similarity for hit, later in zip(default_hits, default_hits[1:]))
        assert [hit.content for hit in limited] == ["s-1.0", "s-0.9"]
        assert [hit.content for hit in capped] == ["s-1.0", "s-0.9", "s-0.8", "s-0.7", "s-0.6", "s-0.5"]
    finally:
        _delete_document(retrieval_db.pool, tenant_id, document_id)


def test_similarity_threshold_filters_by_similarity(retrieval_db):
    tenant_id = uuid4()
    document_id = _insert_document(
        retrieval_db.pool,
        tenant_id,
        [
            _chunk("low", _unit_vector(0.3)),
            _chunk("high", _unit_vector(0.8)),
            _chunk("exact", _axis(0)),
            _chunk("opposite", _axis(0, sign=-1.0)),
        ],
    )
    retriever = VectorRetriever(retrieval_db.pool, _http_client(_fixed([], _axis(0))))
    try:
        above = retriever.search(tenant_id, "付款条件", similarity_threshold=0.75)
        zero = retriever.search(tenant_id, "付款条件", similarity_threshold=0)
        exact = retriever.search(tenant_id, "付款条件", similarity_threshold=1)
        unbounded = retriever.search(tenant_id, "付款条件", top_k=10)
        assert [hit.content for hit in above] == ["exact", "high"]
        assert [hit.content for hit in zero] == ["exact", "high", "low"]
        assert [hit.content for hit in exact] == ["exact"]
        assert [hit.content for hit in unbounded] == ["exact", "high", "low", "opposite"]
        assert above[0].similarity >= 0.75
        assert math.isclose(above[1].similarity, 0.8, rel_tol=1e-4, abs_tol=1e-4)
    finally:
        _delete_document(retrieval_db.pool, tenant_id, document_id)


def test_null_embeddings_are_excluded(retrieval_db):
    tenant_id = uuid4()
    document_id = _insert_document(
        retrieval_db.pool,
        tenant_id,
        [
            _chunk("未向量化条款", None),
            _chunk("已向量化条款", _axis(0)),
        ],
    )
    try:
        hits = VectorRetriever(retrieval_db.pool, _http_client(_fixed([], _axis(0)))).search(
            tenant_id,
            "付款条件",
        )
        assert [hit.content for hit in hits] == ["已向量化条款"]
    finally:
        _delete_document(retrieval_db.pool, tenant_id, document_id)


def test_other_embedding_models_are_excluded(retrieval_db):
    tenant_id = uuid4()
    document_id = _insert_document(
        retrieval_db.pool,
        tenant_id,
        [
            _chunk("旧模型条款", _axis(0), model="legacy-model"),
            _chunk("当前模型条款", _unit_vector(0.6)),
        ],
    )
    try:
        hits = VectorRetriever(retrieval_db.pool, _http_client(_fixed([], _axis(0)))).search(
            tenant_id,
            "付款条件",
        )
        assert [hit.content for hit in hits] == ["当前模型条款"]
        assert hits[0].embedding_model == EMBEDDING_MODEL_NAME
    finally:
        _delete_document(retrieval_db.pool, tenant_id, document_id)


def test_document_id_limits_the_search(retrieval_db):
    tenant_id = uuid4()
    first_id = _insert_document(
        retrieval_db.pool,
        tenant_id,
        [_chunk("指定文档条款", _axis(0), char_start=1, char_end=6)],
    )
    second_id = _insert_document(
        retrieval_db.pool,
        tenant_id,
        [_chunk("其他文档条款", _unit_vector(0.8))],
    )
    retriever = VectorRetriever(retrieval_db.pool, _http_client(_fixed([], _axis(0))))
    try:
        scoped = retriever.search(tenant_id, "付款条件", document_id=first_id)
        everything = retriever.search(tenant_id, "付款条件")
        assert [hit.content for hit in scoped] == ["指定文档条款"]
        assert scoped[0].document_id == first_id
        assert scoped[0].char_start == 1
        assert scoped[0].char_end == 6
        assert [hit.content for hit in everything] == ["指定文档条款", "其他文档条款"]
        assert {hit.document_id for hit in everything} == {first_id, second_id}
    finally:
        _delete_document(retrieval_db.pool, tenant_id, first_id)
        _delete_document(retrieval_db.pool, tenant_id, second_id)


def test_other_tenant_chunks_are_invisible(retrieval_db):
    owner = uuid4()
    intruder = uuid4()
    owner_document = _insert_document(
        retrieval_db.pool,
        owner,
        [_chunk("其他租户机密条款", _axis(0))],
    )
    intruder_document = _insert_document(
        retrieval_db.pool,
        intruder,
        [_chunk("本租户可见条款", _unit_vector(0.4))],
    )
    retriever = VectorRetriever(retrieval_db.pool, _http_client(_fixed([], _axis(0))))
    try:
        owner_hits = retriever.search(owner, "付款条件")
        intruder_hits = retriever.search(intruder, "付款条件")
        assert [hit.content for hit in owner_hits] == ["其他租户机密条款"]
        assert [hit.content for hit in intruder_hits] == ["本租户可见条款"]
        assert all(hit.document_id == intruder_document for hit in intruder_hits)
    finally:
        _delete_document(retrieval_db.pool, owner, owner_document)
        _delete_document(retrieval_db.pool, intruder, intruder_document)


def test_unknown_or_foreign_document_does_not_leak(retrieval_db):
    owner = uuid4()
    intruder = uuid4()
    owner_document = _insert_document(
        retrieval_db.pool,
        owner,
        [_chunk("其他租户机密条款", _axis(0))],
    )
    intruder_document = _insert_document(
        retrieval_db.pool,
        intruder,
        [_chunk("本租户可见条款", _axis(0))],
    )
    retriever = VectorRetriever(retrieval_db.pool, _http_client(_fixed([], _axis(0))))
    try:
        foreign = retriever.search(intruder, "付款条件", document_id=owner_document)
        missing = retriever.search(intruder, "付款条件", document_id=uuid4())
        own = retriever.search(intruder, "付款条件", document_id=intruder_document)
        assert foreign == []
        assert missing == []
        assert [hit.content for hit in own] == ["本租户可见条款"]
    finally:
        _delete_document(retrieval_db.pool, owner, owner_document)
        _delete_document(retrieval_db.pool, intruder, intruder_document)


def test_no_match_returns_empty_list(retrieval_db):
    tenant_id = uuid4()
    document_id = _insert_document(
        retrieval_db.pool,
        tenant_id,
        [_chunk("低相关条款", _unit_vector(0.2))],
    )
    retriever = VectorRetriever(retrieval_db.pool, _http_client(_fixed([], _axis(0))))
    try:
        assert retriever.search(uuid4(), "付款条件") == []
        assert retriever.search(tenant_id, "付款条件", similarity_threshold=0.99) == []
    finally:
        _delete_document(retrieval_db.pool, tenant_id, document_id)


@pytest.mark.parametrize("value", ["", " ", "\n\t", " \u3000 ", None, 3])
def test_blank_query_is_rejected(value):
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    pool = GuardPool()
    retriever = VectorRetriever(pool, _http_client(handler))
    with pytest.raises(RetrievalValidationError, match="查询内容不能为空"):
        retriever.search(uuid4(), value)  # type: ignore[arg-type]
    assert called is False
    assert pool.calls == 0


@pytest.mark.parametrize("value", [0, -1, 21, 100, True, False, 1.5, "5", None])
def test_invalid_top_k_is_rejected(value):
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    pool = GuardPool()
    retriever = VectorRetriever(pool, _http_client(handler))
    with pytest.raises(RetrievalValidationError, match="top_k"):
        retriever.search(uuid4(), "付款条件", top_k=value)  # type: ignore[arg-type]
    assert called is False
    assert pool.calls == 0


@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf, -math.inf, True, False, "0.5"])
def test_similarity_threshold_out_of_range_is_rejected(value):
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    pool = GuardPool()
    retriever = VectorRetriever(pool, _http_client(handler))
    with pytest.raises(RetrievalValidationError, match="similarity_threshold"):
        retriever.search(uuid4(), "付款条件", similarity_threshold=value)  # type: ignore[arg-type]
    assert called is False
    assert pool.calls == 0


def test_embedding_api_failure_does_not_query_the_database(caplog):
    calls = {"count": 0}
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500, json={"message": API_KEY, "question": SECRET_QUESTION})

    pool = GuardPool()
    client = _http_client(handler, sleeper=delays.append)
    with caplog.at_level("DEBUG"):
        with pytest.raises(EmbeddingAPIError, match="HTTP 500") as caught:
            VectorRetriever(pool, client).search(uuid4(), SECRET_QUESTION)
    assert calls["count"] == 3
    assert delays == [0.2, 0.4]
    assert pool.calls == 0
    assert API_KEY not in str(caught.value)
    assert SECRET_QUESTION not in str(caught.value)
    assert API_KEY not in caplog.text
    assert SECRET_QUESTION not in caplog.text


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("dimension", "维度"),
        ("nan", "非法数值"),
        ("infinity", "非法数值"),
    ],
)
def test_provider_query_vector_is_rejected_before_the_database(payload: str, message: str):
    called = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        if payload == "dimension":
            body = {
                "model": EMBEDDING_MODEL_NAME,
                "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
            }
            return httpx.Response(200, json=body)
        token = "NaN" if payload == "nan" else "Infinity"
        return httpx.Response(200, content=_raw_constant(token))

    pool = GuardPool()
    with pytest.raises(EmbeddingResponseError, match=message) as caught:
        VectorRetriever(pool, _http_client(handler)).search(uuid4(), SECRET_QUESTION)
    assert called["count"] == 1
    assert pool.calls == 0
    assert SECRET_QUESTION not in str(caught.value)
    assert API_KEY not in str(caught.value)


@pytest.mark.parametrize(
    "vector",
    [
        [0.1, 0.2],
        [0.1] * 1023,
        [0.1] * 1025,
        [math.nan] * 1024,
        [math.inf] * 1024,
        [-math.inf] + [0.0] * 1023,
        [0.1] * 1023 + ["bad"],
        [True] + [0.1] * 1023,
    ],
)
def test_scripted_query_vector_is_rejected_before_the_database(vector):
    pool = GuardPool()
    client = ScriptedClient(vector)
    with pytest.raises(EmbeddingResponseError):
        VectorRetriever(pool, client).search(uuid4(), "  付款条件\n")
    assert client.calls == ["付款条件"]
    assert pool.calls == 0


def test_wrong_embedding_model_does_not_query_the_database():
    pool = GuardPool()
    client = ScriptedClient(_axis(0), model_name="other-model")
    with pytest.raises(EmbeddingConfigurationError, match="模型"):
        VectorRetriever(pool, client).search(uuid4(), "付款条件")
    assert pool.calls == 0


@pytest.mark.parametrize("tenant_id", ["tenant-from-client", None, 7])
def test_untrusted_tenant_id_is_rejected(tenant_id):
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    pool = GuardPool()
    with pytest.raises(RetrievalValidationError, match="tenant_id"):
        VectorRetriever(pool, _http_client(handler)).search(tenant_id, "付款条件")  # type: ignore[arg-type]
    assert called is False
    assert pool.calls == 0


def test_invalid_document_id_is_rejected():
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    pool = GuardPool()
    with pytest.raises(RetrievalValidationError, match="document_id"):
        VectorRetriever(pool, _http_client(handler)).search(
            uuid4(),
            "付款条件",
            document_id="not-a-uuid",  # type: ignore[arg-type]
        )
    assert called is False
    assert pool.calls == 0


class ScriptedClient:
    def __init__(self, vector: object, *, model_name: str = EMBEDDING_MODEL_NAME) -> None:
        self.model_name = model_name
        self.vector = vector
        self.calls: list[str] = []

    def embed_query(self, query: str) -> object:
        self.calls.append(query)
        return self.vector


class GuardPool:
    def __init__(self) -> None:
        self.calls = 0

    def connection(self):
        self.calls += 1
        raise RuntimeError("不应访问数据库")


class _QueryCounter:
    """Forward the real pool while counting checkout calls."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool
        self.calls = 0

    def connection(self):
        self.calls += 1
        return self.pool.connection()


@dataclass(frozen=True)
class _SeedChunk:
    content: str
    vector: list[float] | None
    model: str | None
    char_start: int
    char_end: int


def _chunk(
    content: str,
    vector: list[float] | None,
    *,
    model: str | None = None,
    char_start: int = 0,
    char_end: int | None = None,
) -> _SeedChunk:
    return _SeedChunk(
        content=content,
        vector=vector,
        model=model,
        char_start=char_start,
        char_end=len(content) if char_end is None else char_end,
    )


def _axis(index: int, *, sign: float = 1.0) -> list[float]:
    values = [0.0] * EMBEDDING_WIDTH
    values[index] = sign
    return values


def _unit_vector(primary: float) -> list[float]:
    secondary = math.sqrt(max(0.0, 1.0 - primary * primary))
    values = [0.0] * EMBEDDING_WIDTH
    values[0] = primary
    values[1] = secondary
    norm = math.sqrt(values[0] * values[0] + values[1] * values[1])
    scaled = [component / norm for component in values]
    return [_float32(component) for component in scaled]


def _float32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def _http_client(handler, *, sleeper=None) -> EmbeddingClient:
    if sleeper is None:
        def sleeper(_seconds: float) -> None:
            return None
    return EmbeddingClient(
        api_key=API_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=sleeper,
    )


def _ok(seen: list[dict[str, object]], *, seed: float):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _embedding_response([seed] * EMBEDDING_WIDTH)

    return handler


def _fixed(seen: list[dict[str, object]], vector: list[float]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _embedding_response(vector)

    return handler


def _embedding_response(vector: list[float]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": EMBEDDING_MODEL_NAME,
            "data": [{"object": "embedding", "index": 0, "embedding": vector}],
        },
    )


def _raw_constant(token: str) -> bytes:
    values = ", ".join([token] + ["0.1"] * (EMBEDDING_WIDTH - 1))
    body = {"model": EMBEDDING_MODEL_NAME, "data": [{"index": 0, "embedding": []}]}
    return json.dumps(body).replace("[]", f"[{values}]").encode("utf-8")


@pytest.fixture(scope="module")
def retrieval_db():
    env = _root_env()
    if (
        not env.get("AGENT_APP_PASSWORD")
        or not env.get("AGENT_MIGRATOR_PASSWORD")
        or not env.get("POSTGRES_DB")
    ):
        pytest.skip(
            "仓库根目录 .env 未提供 AGENT_APP_PASSWORD 或 AGENT_MIGRATOR_PASSWORD，跳过真实数据库测试"
        )
    settings = _settings(env)
    try:
        apply_migrations(settings)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    pool = ConnectionPool(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_app_user,
            settings.agent_app_password,
        ),
        min_size=2,
        max_size=4,
        timeout=10,
        kwargs={"options": "-c search_path=agent"},
        open=True,
    )
    try:
        yield type("RetrievalDatabase", (), {"pool": pool, "env": env, "settings": settings})()
    finally:
        pool.close()


def _insert_document(pool: ConnectionPool, tenant_id: UUID, chunks: list[_SeedChunk]) -> UUID:
    document_id = uuid4()
    checksum = hashlib.sha256(document_id.bytes).hexdigest()
    with tenant_transaction(pool, str(tenant_id)) as connection:
        connection.execute(
            """
            insert into agent.knowledge_document (
                id, tenant_id, document_name, file_type, file_checksum, status, total_chunks
            ) values (%s, %s, '检索测试', 'pdf', %s, 'ready', %s)
            """,
            (document_id, tenant_id, checksum, len(chunks)),
        )
        for index, chunk in enumerate(chunks):
            metadata = json.dumps(
                {"char_start": chunk.char_start, "char_end": chunk.char_end},
                separators=(",", ":"),
            )
            if chunk.vector is None:
                connection.execute(
                    """
                    insert into agent.knowledge_chunk (
                        tenant_id, document_id, chunk_index, content, char_count, metadata
                    ) values (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (tenant_id, document_id, index, chunk.content, len(chunk.content), metadata),
                )
                continue
            model_name = EMBEDDING_MODEL_NAME if chunk.model is None else chunk.model
            connection.execute(
                """
                insert into agent.knowledge_chunk (
                    tenant_id, document_id, chunk_index, content, char_count, metadata,
                    embedding, embedding_model, embedded_at
                ) values (
                    %s, %s, %s, %s, %s, %s::jsonb,
                    %s::public.vector, %s, now()
                )
                """,
                (
                    tenant_id,
                    document_id,
                    index,
                    chunk.content,
                    len(chunk.content),
                    metadata,
                    format_vector(chunk.vector),
                    model_name,
                ),
            )
    return document_id


def _delete_document(pool: ConnectionPool, tenant_id: UUID, document_id: UUID) -> None:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        connection.execute(
            "delete from agent.knowledge_document where id = %s and tenant_id = %s",
            (document_id, tenant_id),
        )


def _settings(env: dict[str, str]) -> Settings:
    return Settings(
        postgres_host=env.get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(env.get("POSTGRES_PORT", "5432")),
        postgres_db=env["POSTGRES_DB"],
        agent_app_user="agent_app",
        agent_app_password=env["AGENT_APP_PASSWORD"],
        agent_migrator_user="agent_migrator",
        agent_migrator_password=env["AGENT_MIGRATOR_PASSWORD"],
    )


def _root_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values
