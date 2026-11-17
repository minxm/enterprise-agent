"""Real PostgreSQL checks for the evaluation tenant.

Vectors in this file come from a deterministic stand-in so the suite does not
call SiliconFlow. Retrieval quality numbers belong to the --live-pgvector command.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError
from psycopg_pool import ConnectionPool

from app.core.config import EMBEDDING_MODEL_NAME, EMBEDDING_WIDTH, Settings
from app.evaluation.dataset import EvalPassage, EvaluationDataset, load_dataset
from app.evaluation.pgvector_eval import (
    PGVECTOR_EVAL_TENANT_ID,
    PgvectorEvaluation,
    PgvectorEvaluationError,
    PostgresCatalog,
    TimedEmbeddingClient,
    file_id_for,
    passage_document,
)
from app.knowledge.ingest import IngestDocument, KnowledgeIngestionService
from app.knowledge.retrieval import VectorRetriever
from app.knowledge.vectorization import VectorizationService
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, tenant_transaction

DATASET = Path(__file__).resolve().parents[1] / "evaluation_data" / "enterprise_contract_eval.json"
INTEGRATION_DATASET_NAME = "pgvector-integration-test"


class DeterministicClient:
    def __init__(self, dataset: EvaluationDataset) -> None:
        self.model_name = EMBEDDING_MODEL_NAME
        self.embed_calls = 0
        passages = dataset.passage_map()
        self._queries: dict[str, list[float]] = {}
        for case in dataset.cases:
            if case.expected_chunk_ids:
                content = passages[case.expected_chunk_ids[0]].content
                self._queries[case.question.strip()] = _vector(content)

    def embed(self, texts: str | list[str]) -> list[list[float]]:
        self.embed_calls += 1
        items = [texts] if isinstance(texts, str) else list(texts)
        return [_vector(item) for item in items]

    def embed_query(self, query: str) -> list[float]:
        return self._queries.get(query.strip(), _vector("missing-question"))


@pytest.fixture(scope="module")
def pgvector_db():
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
def test_postgres_reuses_vectors_and_cleanup_stays_inside_the_task(pgvector_db):
    dataset = _integration_dataset()
    client = DeterministicClient(dataset)
    timed = TimedEmbeddingClient(client)
    retriever = VectorRetriever(pgvector_db, timed)
    runner = PgvectorEvaluation(
        catalog=PostgresCatalog(pgvector_db),
        ingestion=KnowledgeIngestionService(pgvector_db),
        vectorization=VectorizationService(pgvector_db, timed),
        retriever=retriever,
        embedding_client=timed,
    )
    other_tenant = uuid4()
    other_file = uuid4()
    extra_file = uuid4()
    throwaway_file = uuid4()
    try:
        runner.cleanup(dataset)
        first = runner.prepare(dataset)
        embed_calls = client.embed_calls
        second = runner.prepare(dataset)

        assert len(first) == len(dataset.passages)
        assert all(item.dimensions == EMBEDDING_WIDTH for item in first)
        assert all(item.embedding_model == EMBEDDING_MODEL_NAME for item in first)
        assert all(not item.reused_document and not item.reused_embedding for item in first)
        assert client.embed_calls == embed_calls
        assert all(item.reused_document and item.reused_embedding for item in second)
        assert [item.chunk_id for item in second] == [item.chunk_id for item in first]

        case = next(item for item in dataset.cases if item.case_id == "single-pay-first-step")
        hits = retriever.search(PGVECTOR_EVAL_TENANT_ID, case.question, top_k=5)
        expected = next(item for item in second if item.standard_chunk_id == case.expected_chunk_ids[0])
        assert hits
        assert hits[0].chunk_id == expected.chunk_id
        assert math.isclose(hits[0].similarity, 1.0, rel_tol=0, abs_tol=1e-4)
        stored = PostgresCatalog(pgvector_db).list_chunks(PGVECTOR_EVAL_TENANT_ID, expected.document_id)
        assert stored[0].dimensions == EMBEDDING_WIDTH
        assert stored[0].embedding_model == EMBEDDING_MODEL_NAME

        other_document = _ingest(pgvector_db, other_tenant, "其他租户的合同文本不应被清理。", other_file, "其他租户.docx")
        extra_document = _ingest(pgvector_db, PGVECTOR_EVAL_TENANT_ID, "同租户的其他评估任务不应被清理。", extra_file, "其他任务.docx")
        throwaway = _ingest(pgvector_db, PGVECTOR_EVAL_TENANT_ID, "回滚测试文档必须保留。", throwaway_file, "回滚.docx")
        with pytest.raises(RuntimeError, match="rollback"):
            with tenant_transaction(pgvector_db, str(PGVECTOR_EVAL_TENANT_ID)) as connection:
                connection.execute(
                    """
                    delete from agent.knowledge_document
                    where tenant_id = %s and file_id = %s
                    """,
                    (PGVECTOR_EVAL_TENANT_ID, throwaway_file),
                )
                raise RuntimeError("rollback")
        assert _count(pgvector_db, PGVECTOR_EVAL_TENANT_ID, throwaway_file) == 1
        with pytest.raises(PgvectorEvaluationError, match="评估租户"):
            PostgresCatalog(pgvector_db).delete_documents(other_tenant, [other_file])

        deleted = set(runner.cleanup(dataset))

        assert deleted == {item.document_id for item in first}
        assert _count(pgvector_db, other_tenant, other_file) == 1
        assert _count(pgvector_db, PGVECTOR_EVAL_TENANT_ID, extra_file) == 1
        assert _count(pgvector_db, PGVECTOR_EVAL_TENANT_ID, throwaway_file) == 1
        for passage in dataset.passages:
            assert _count(pgvector_db, PGVECTOR_EVAL_TENANT_ID, file_id_for(dataset.dataset_name, passage.chunk_id)) == 0
        assert other_document.document_id != extra_document.document_id
        assert throwaway.document_id not in deleted
    finally:
        runner.cleanup(dataset)
        _delete(pgvector_db, other_tenant, other_file)
        _delete(pgvector_db, PGVECTOR_EVAL_TENANT_ID, extra_file)
        _delete(pgvector_db, PGVECTOR_EVAL_TENANT_ID, throwaway_file)


@pytest.mark.siliconflow
def test_real_siliconflow_pgvector_is_opt_in(tmp_path):
    if os.environ.get("RUN_PGVECTOR_LIVE") != "1":
        pytest.skip("设置 RUN_PGVECTOR_LIVE=1 才会调用真实 SiliconFlow。默认测试不访问 API。")
    from scripts.evaluate_rag import main

    output = tmp_path / "live-pgvector.json"
    code = main(
        [
            "--dataset",
            str(DATASET),
            "--top-k",
            "5",
            "--output",
            str(output),
            "--live-pgvector",
        ]
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code in (0, 1)
    assert report["evaluation_mode"] == "pgvector"
    assert report["answer_metrics"]["evaluated"] is False


def _integration_dataset() -> EvaluationDataset:
    source = load_dataset(DATASET)
    return EvaluationDataset(
        dataset_name=INTEGRATION_DATASET_NAME,
        passages=source.passages,
        cases=source.cases,
    )


def _ingest(pool: ConnectionPool, tenant_id: UUID, content: str, file_id: UUID, name: str):
    passage = EvalPassage(chunk_id=str(uuid4()), document_id=str(uuid4()), content=content, title=name)
    return KnowledgeIngestionService(pool).ingest(
        IngestDocument(
            tenant_id=tenant_id,
            content=passage_document(passage),
            file_type="docx",
            document_name=name,
            file_id=file_id,
        )
    )


def _count(pool: ConnectionPool, tenant_id: UUID, file_id: UUID) -> int:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        row = connection.execute(
            """
            select count(*)
            from agent.knowledge_document
            where tenant_id = %s and file_id = %s
            """,
            (tenant_id, file_id),
        ).fetchone()
    return int(row[0])


def _delete(pool: ConnectionPool, tenant_id: UUID, file_id: UUID) -> None:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        connection.execute(
            """
            delete from agent.knowledge_document
            where tenant_id = %s and file_id = %s
            """,
            (tenant_id, file_id),
        )


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [0.02] * EMBEDDING_WIDTH
    for index, byte in enumerate(digest):
        values[index] = 0.05 + (byte / 300.0)
    return values


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
