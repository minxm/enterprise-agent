"""Real PostgreSQL checks for character-pair keyword search.

Vectors are not requested from SiliconFlow. Quality numbers for the evaluation
corpus belong to --live-pgvector --search-strategy all.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError
from psycopg_pool import ConnectionPool

from app.core.config import Settings
from app.knowledge.hybrid import HybridRetriever
from app.knowledge.keyword_retrieval import KeywordRetriever
from app.knowledge.retrieval import RetrievalHit
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, tenant_transaction

TENANT = UUID("00000000-0000-4000-8000-00000000e221")
OTHER = UUID("00000000-0000-4000-8000-00000000e222")
INVOICE = UUID("00000000-0000-4000-8000-0000000000f1")
EMERGENCY = UUID("00000000-0000-4000-8000-0000000000f2")
FIFTY = UUID("00000000-0000-4000-8000-0000000000f3")
HUNDRED = UUID("00000000-0000-4000-8000-0000000000f4")
CONTRACT = UUID("00000000-0000-4000-8000-0000000000f5")
ARCHIVE = UUID("00000000-0000-4000-8000-0000000000f6")
STAMP_LOW = UUID("00000000-0000-4000-8000-00000000aa01")
STAMP_HIGH = UUID("00000000-0000-4000-8000-00000000aa02")
FOREIGN = UUID("00000000-0000-4000-8000-0000000000f9")
INVOICE_TEXT = "付款前必须取得增值税专用发票，发票抬头为公司全称。"
EMERGENCY_TEXT = "紧急付款需要部门负责人和财务负责人同时同意，发票可以在付款后五个工作日内补交。"


class _VectorStub:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self.hits = hits
        self.tenants: list[UUID] = []

    def search(self, tenant_id: UUID, _query: str, *, top_k: int = 5, document_id: UUID | None = None):
        del top_k, document_id
        self.tenants.append(tenant_id)
        return list(self.hits)


@pytest.fixture(scope="module")
def keyword_db():
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供数据库密码，跳过真实数据库测试")
    settings = _settings(env)
    try:
        apply_migrations(settings)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    except RuntimeError as exc:
        if "连接" in str(exc) or "PostgreSQL" in str(exc):
            pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
        raise
    pool = ConnectionPool(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_app_user,
            settings.agent_app_password,
        ),
        min_size=1,
        max_size=4,
        timeout=10,
        kwargs={"options": "-c search_path=agent"},
        open=True,
    )
    try:
        yield pool
    finally:
        pool.close()


@pytest.mark.postgres
def test_keyword_search_ranks_filters_and_stays_inside_the_tenant(keyword_db):
    try:
        invoice_doc = _insert(keyword_db, TENANT, INVOICE, INVOICE_TEXT)
        emergency_doc = _insert(keyword_db, TENANT, EMERGENCY, EMERGENCY_TEXT)
        _insert(keyword_db, TENANT, FIFTY, "单笔付款金额超过 50 万元时，必须再由总经理审批。")
        _insert(keyword_db, TENANT, HUNDRED, "合同金额超过 100 万元时，签署前必须经过总经理审批。")
        _insert(keyword_db, TENANT, CONTRACT, "合同编号 HT2024 已签署。")
        _insert(keyword_db, TENANT, ARCHIVE, "编号 20 已归档。")
        _insert(keyword_db, TENANT, STAMP_LOW, "双方盖章后生效。")
        _insert(keyword_db, TENANT, STAMP_HIGH, "双方盖章后生效。")
        _insert(keyword_db, OTHER, FOREIGN, INVOICE_TEXT)

        retriever = KeywordRetriever(keyword_db)
        invoice_hits = retriever.search(TENANT, "专用发票", top_k=5)
        amount_hits = retriever.search(TENANT, "超过 50 万元", top_k=5)
        number_hits = retriever.search(TENANT, "HT2024", top_k=5)
        tied = retriever.search(TENANT, "盖章", top_k=5)
        filtered = retriever.search(TENANT, "发票", top_k=5, document_id=emergency_doc)
        hidden = retriever.search(OTHER, "专用发票", top_k=5)

        assert invoice_hits[0].chunk_id == INVOICE
        assert FOREIGN not in {item.chunk_id for item in invoice_hits}
        assert [item.chunk_id for item in amount_hits[:2]] == [FIFTY, HUNDRED]
        assert number_hits[0].chunk_id == CONTRACT
        assert [item.chunk_id for item in tied[:2]] == [STAMP_LOW, STAMP_HIGH]
        assert [item.chunk_id for item in filtered] == [EMERGENCY]
        assert [item.chunk_id for item in hidden] == [FOREIGN]
        assert retriever.search(TENANT, "发") == []
        assert retriever.search(TENANT, "火星殖民") == []
        assert _index_exists(keyword_db)
        assert "发票" in _bigrams(keyword_db, "专用发票")

        stub = _VectorStub(
            [
                RetrievalHit(
                    invoice_doc,
                    HUNDRED,
                    0,
                    "合同金额超过 100 万元时，签署前必须经过总经理审批。",
                    0,
                    10,
                    0.2,
                    "Qwen/Qwen3-Embedding-0.6B",
                )
            ]
        )
        fused = HybridRetriever(stub, retriever).search(TENANT, "专用发票", top_k=5)
        by_id = {item.chunk_id: item for item in fused}
        assert by_id[INVOICE].keyword_rank == 1
        assert by_id[INVOICE].vector_rank is None
        assert by_id[HUNDRED].vector_rank == 1
        assert by_id[HUNDRED].similarity == 0.2
        assert stub.tenants == [TENANT]
    finally:
        for tenant_id, document_id in _documents_for(keyword_db, TENANT) + _documents_for(keyword_db, OTHER):
            _delete(keyword_db, tenant_id, document_id)


def _insert(pool: ConnectionPool, tenant_id: UUID, chunk_id: UUID, content: str) -> UUID:
    document_id = uuid4()
    checksum = hashlib.sha256(document_id.bytes).hexdigest()
    metadata = json.dumps({"char_start": 0, "char_end": len(content)}, separators=(",", ":"))
    with tenant_transaction(pool, str(tenant_id)) as connection:
        connection.execute(
            """
            insert into agent.knowledge_document (
                id, tenant_id, document_name, file_id, file_type, file_checksum, status, total_chunks
            ) values (%s, %s, '关键词测试', %s, 'docx', %s, 'ready', 1)
            """,
            (document_id, tenant_id, uuid4(), checksum),
        )
        connection.execute(
            """
            insert into agent.knowledge_chunk (
                id, tenant_id, document_id, chunk_index, content, char_count, metadata
            ) values (%s, %s, %s, 0, %s, %s, %s::jsonb)
            """,
            (chunk_id, tenant_id, document_id, content, len(content), metadata),
        )
    return document_id


def _documents_for(pool: ConnectionPool, tenant_id: UUID) -> list[tuple[UUID, UUID]]:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        rows = connection.execute(
            """
            select tenant_id, id
            from agent.knowledge_document
            where tenant_id = %s and document_name = '关键词测试'
            """,
            (tenant_id,),
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _delete(pool: ConnectionPool, tenant_id: UUID, document_id: UUID) -> None:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        connection.execute(
            "delete from agent.knowledge_document where id = %s and tenant_id = %s",
            (document_id, tenant_id),
        )


def _index_exists(pool: ConnectionPool) -> bool:
    with tenant_transaction(pool, str(TENANT)) as connection:
        row = connection.execute(
            """
            select 1
            from pg_indexes
            where schemaname = 'agent' and indexname = 'knowledge_chunk_content_bigram_idx'
            """
        ).fetchone()
    return row is not None


def _bigrams(pool: ConnectionPool, text: str) -> list[str]:
    with tenant_transaction(pool, str(TENANT)) as connection:
        row = connection.execute("select agent.character_bigrams(%s)", (text,)).fetchone()
    return list(row[0])


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
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
