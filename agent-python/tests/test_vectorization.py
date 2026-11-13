import hashlib
import math
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import Connection, OperationalError
from psycopg.errors import InsufficientPrivilege
from psycopg_pool import ConnectionPool

from app.core.config import EMBEDDING_MODEL_NAME, Settings
from app.knowledge.embeddings import EmbeddingAPIError
from app.knowledge.vectorization import (
    DocumentNotFoundError,
    VectorizationConflictError,
    VectorizationService,
)
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, tenant_transaction


def test_vectors_are_written_in_chunk_order(vector_db):
    tenant_id = uuid4()
    contents = ["chunk-zero", "chunk-one", "chunk-two"]
    document_id = _insert_document(vector_db.pool, tenant_id, contents)
    client = FakeEmbeddingClient(seed=0.2)
    try:
        result = VectorizationService(vector_db.pool, client).vectorize(tenant_id, document_id)
        stored = _stored_vectors(vector_db.pool, tenant_id, document_id)

        assert result.status == "ready"
        assert result.chunk_count == 3
        assert result.embedding_model == EMBEDDING_MODEL_NAME
        assert client.inputs == [contents]
        assert [row[0] for row in stored] == [0, 1, 2]
        assert len({row[3] for row in stored}) == 1
        for index, row in enumerate(stored):
            values = _parse_vector(row[1])
            assert len(values) == 1024
            assert math.isclose(values[0], 0.2 + index, rel_tol=1e-5, abs_tol=1e-5)
            assert row[2] == EMBEDDING_MODEL_NAME
            assert row[3] is not None
    finally:
        _delete_document(vector_db.pool, tenant_id, document_id)


def test_missing_document_does_not_call_the_model(vector_db):
    client = FakeEmbeddingClient()
    with pytest.raises(DocumentNotFoundError):
        VectorizationService(vector_db.pool, client).vectorize(uuid4(), uuid4())
    assert client.inputs == []


def test_other_tenant_cannot_vectorize_or_read(vector_db):
    owner = uuid4()
    other = uuid4()
    document_id = _insert_document(vector_db.pool, owner, ["owned"])
    client = FakeEmbeddingClient()
    try:
        with pytest.raises(DocumentNotFoundError):
            VectorizationService(vector_db.pool, client).vectorize(other, document_id)
        assert client.inputs == []
        with tenant_transaction(vector_db.pool, str(other)) as connection:
            visible = connection.execute(
                "select count(*) from agent.knowledge_chunk where document_id = %s",
                (document_id,),
            ).fetchone()
        assert visible[0] == 0
    finally:
        _delete_document(vector_db.pool, owner, document_id)


def test_chunk_changes_during_embedding_are_rejected(vector_db):
    tenant_id = uuid4()
    document_id = _insert_document(vector_db.pool, tenant_id, ["stable", "also-stable"])

    def change() -> None:
        with tenant_transaction(vector_db.pool, str(tenant_id)) as connection:
            connection.execute(
                """
                update agent.knowledge_chunk
                set content = content || ' changed',
                    char_count = char_length(content || ' changed')
                where document_id = %s and chunk_index = 0
                """,
                (document_id,),
            )

    client = FakeEmbeddingClient(on_call=change)
    try:
        with pytest.raises(VectorizationConflictError, match="重新向量化"):
            VectorizationService(vector_db.pool, client).vectorize(tenant_id, document_id)
        stored = _stored_vectors(vector_db.pool, tenant_id, document_id)
        assert [row[1] for row in stored] == [None, None]
    finally:
        _delete_document(vector_db.pool, tenant_id, document_id)


def test_document_deleted_during_embedding_is_rejected(vector_db):
    tenant_id = uuid4()
    document_id = _insert_document(vector_db.pool, tenant_id, ["going-away"])

    def remove() -> None:
        with tenant_transaction(vector_db.pool, str(tenant_id)) as connection:
            connection.execute(
                "delete from agent.knowledge_document where id = %s and tenant_id = %s",
                (document_id, tenant_id),
            )

    client = FakeEmbeddingClient(on_call=remove)
    with pytest.raises(VectorizationConflictError, match="不存在"):
        VectorizationService(vector_db.pool, client).vectorize(tenant_id, document_id)
    assert _document_count(vector_db.pool, tenant_id) == 0


def test_embedding_failure_writes_nothing(vector_db):
    tenant_id = uuid4()
    document_id = _insert_document(vector_db.pool, tenant_id, ["left", "right"])
    client = FakeEmbeddingClient(fail=True)
    try:
        with pytest.raises(EmbeddingAPIError):
            VectorizationService(vector_db.pool, client).vectorize(tenant_id, document_id)
        stored = _stored_vectors(vector_db.pool, tenant_id, document_id)
        assert [row[1] for row in stored] == [None, None]
        assert [row[2] for row in stored] == [None, None]
    finally:
        _delete_document(vector_db.pool, tenant_id, document_id)


def test_write_failure_rolls_back_the_whole_batch(vector_db):
    tenant_id = uuid4()
    document_id = _insert_document(vector_db.pool, tenant_id, ["one", "two"])
    try:
        _set_chunk_update(vector_db.env, granted=False)
        try:
            with pytest.raises(InsufficientPrivilege):
                VectorizationService(vector_db.pool, FakeEmbeddingClient()).vectorize(tenant_id, document_id)
        finally:
            _set_chunk_update(vector_db.env, granted=True)
        stored = _stored_vectors(vector_db.pool, tenant_id, document_id)
        assert [row[1] for row in stored] == [None, None]
    finally:
        _delete_document(vector_db.pool, tenant_id, document_id)


def test_failed_revectorize_keeps_existing_vectors(vector_db):
    tenant_id = uuid4()
    document_id = _insert_document(vector_db.pool, tenant_id, ["keep-me"])
    try:
        VectorizationService(vector_db.pool, FakeEmbeddingClient(seed=0.25)).vectorize(tenant_id, document_id)
        original = _stored_vectors(vector_db.pool, tenant_id, document_id)
        _set_chunk_update(vector_db.env, granted=False)
        try:
            with pytest.raises(InsufficientPrivilege):
                VectorizationService(vector_db.pool, FakeEmbeddingClient(seed=0.9)).vectorize(
                    tenant_id, document_id
                )
        finally:
            _set_chunk_update(vector_db.env, granted=True)
        current = _stored_vectors(vector_db.pool, tenant_id, document_id)
        assert current[0][1] == original[0][1]
        assert current[0][2] == EMBEDDING_MODEL_NAME
        assert math.isclose(_parse_vector(current[0][1])[0], 0.25, rel_tol=1e-5, abs_tol=1e-5)
    finally:
        _delete_document(vector_db.pool, tenant_id, document_id)


def test_embedding_migration_is_idempotent_and_typed(vector_db):
    apply_migrations(vector_db.settings)
    with _migrator(vector_db.env) as connection:
        version = connection.execute(
            "select count(*) from agent.schema_migrations where version = '002'"
        ).fetchone()
        column = connection.execute(
            """
            select type_namespace.nspname, format_type(attribute.atttypid, attribute.atttypmod)
            from pg_attribute attribute
            join pg_class relation on relation.oid = attribute.attrelid
            join pg_namespace relation_namespace on relation_namespace.oid = relation.relnamespace
            join pg_type column_type on column_type.oid = attribute.atttypid
            join pg_namespace type_namespace on type_namespace.oid = column_type.typnamespace
            where relation_namespace.nspname = 'agent'
              and relation.relname = 'knowledge_chunk'
              and attribute.attname = 'embedding'
              and not attribute.attisdropped
            """
        ).fetchone()
        indexes = connection.execute(
            "select indexdef from pg_indexes where schemaname = 'agent' and tablename = 'knowledge_chunk'"
        ).fetchall()
    assert version[0] == 1
    assert column == ("public", "public.vector(1024)")
    assert all("hnsw" not in row[0].lower() and "ivfflat" not in row[0].lower() for row in indexes)


def test_app_role_cannot_alter_the_chunk_table(vector_db):
    with pytest.raises(InsufficientPrivilege):
        with vector_db.pool.connection() as connection:
            connection.execute("alter table agent.knowledge_chunk add column day17_denied int")


class FakeEmbeddingClient:
    def __init__(self, *, seed: float = 0.2, on_call=None, fail: bool = False) -> None:
        self.model_name = EMBEDDING_MODEL_NAME
        self.inputs: list[list[str]] = []
        self.seed = seed
        self.on_call = on_call
        self.fail = fail

    def embed(self, texts: str | list[str]) -> list[list[float]]:
        items = [texts] if isinstance(texts, str) else list(texts)
        self.inputs.append(items)
        if self.on_call is not None:
            self.on_call()
        if self.fail:
            raise EmbeddingAPIError("Embedding 请求失败，HTTP 500")
        return [[self.seed + index] * 1024 for index in range(len(items))]


@pytest.fixture(scope="module")
def vector_db():
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
        yield type("VectorDatabase", (), {"pool": pool, "env": env, "settings": settings})()
    finally:
        pool.close()


def _insert_document(pool: ConnectionPool, tenant_id: UUID, contents: list[str]) -> UUID:
    document_id = uuid4()
    checksum = hashlib.sha256(document_id.bytes).hexdigest()
    with tenant_transaction(pool, str(tenant_id)) as connection:
        connection.execute(
            """
            insert into agent.knowledge_document (
                id, tenant_id, document_name, file_type, file_checksum, status, total_chunks
            ) values (%s, %s, '向量测试', 'pdf', %s, 'ready', %s)
            """,
            (document_id, tenant_id, checksum, len(contents)),
        )
        for index, content in enumerate(contents):
            connection.execute(
                """
                insert into agent.knowledge_chunk (
                    tenant_id, document_id, chunk_index, content, char_count, metadata
                ) values (%s, %s, %s, %s, %s, '{}'::jsonb)
                """,
                (tenant_id, document_id, index, content, len(content)),
            )
    return document_id


def _stored_vectors(pool: ConnectionPool, tenant_id: UUID, document_id: UUID):
    with tenant_transaction(pool, str(tenant_id)) as connection:
        return connection.execute(
            """
            select chunk_index, embedding::text, embedding_model, embedded_at
            from agent.knowledge_chunk
            where document_id = %s and tenant_id = %s
            order by chunk_index
            """,
            (document_id, tenant_id),
        ).fetchall()


def _document_count(pool: ConnectionPool, tenant_id: UUID) -> int:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        row = connection.execute(
            "select count(*) from agent.knowledge_document where tenant_id = %s",
            (tenant_id,),
        ).fetchone()
    return int(row[0])


def _delete_document(pool: ConnectionPool, tenant_id: UUID, document_id: UUID) -> None:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        connection.execute(
            "delete from agent.knowledge_document where id = %s and tenant_id = %s",
            (document_id, tenant_id),
        )


def _parse_vector(value: str) -> list[float]:
    return [float(part) for part in value.strip()[1:-1].split(",") if part.strip()]


def _set_chunk_update(env: dict[str, str], *, granted: bool) -> None:
    verb = "grant" if granted else "revoke"
    direction = "to" if granted else "from"
    with _migrator(env) as connection:
        connection.execute(f"{verb} update on table agent.knowledge_chunk {direction} agent_app")
        connection.commit()


def _migrator(env: dict[str, str]) -> Connection:
    settings = _settings(env)
    return Connection.connect(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_migrator_user,
            settings.agent_migrator_password,
        ),
        options="-c search_path=agent",
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
