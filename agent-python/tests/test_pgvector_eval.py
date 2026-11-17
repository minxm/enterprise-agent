import inspect
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.config import EMBEDDING_MODEL_NAME, EMBEDDING_WIDTH, Settings
from app.documents.extract import extract_document
from app.evaluation.dataset import EvalCase, EvalPassage, EvaluationDataset, load_dataset
from app.evaluation.pgvector_eval import (
    PGVECTOR_EVAL_TENANT_ID,
    PassageBinding,
    PgvectorConfigError,
    PgvectorEvaluation,
    PgvectorEvaluationError,
    PostgresCatalog,
    StoredChunk,
    TimedEmbeddingClient,
    document_name_for,
    file_id_for,
    passage_document,
    require_pgvector_config,
)
from app.knowledge.chunking import chunk_text
from app.knowledge.embeddings import EmbeddingAPIError
from app.knowledge.ingest import IngestDocument, IngestResult
from app.knowledge.retrieval import RetrievalHit
from app.knowledge.vectorization import VectorizationResult
from scripts.evaluate_rag import _run_pgvector, main

DATASET = Path(__file__).resolve().parents[1] / "evaluation_data" / "enterprise_contract_eval.json"
PASSAGE_A = "00000000-0000-4000-8000-00000000aa01"
PASSAGE_B = "00000000-0000-4000-8000-00000000aa02"
DOCUMENT = "00000000-0000-4000-8000-00000000bb01"
TEXT_A = "付款申请须先由部门负责人审批。"
TEXT_B = "员工入职满一年后每年可休年假五天。"


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MemoryCatalog:
    def __init__(self) -> None:
        self.chunks: dict[UUID, list[StoredChunk]] = {}
        self.documents: dict[UUID, UUID] = {}
        self.deleted: list[tuple[UUID, list[UUID]]] = []

    def list_chunks(self, tenant_id: UUID, document_id: UUID) -> list[StoredChunk]:
        if tenant_id != PGVECTOR_EVAL_TENANT_ID:
            raise PgvectorEvaluationError("租户不正确")
        return list(self.chunks[document_id])

    def delete_documents(self, tenant_id: UUID, file_ids: list[UUID]) -> list[UUID]:
        if tenant_id != PGVECTOR_EVAL_TENANT_ID:
            raise PgvectorEvaluationError("租户不正确")
        self.deleted.append((tenant_id, list(file_ids)))
        deleted: list[UUID] = []
        for file_id in file_ids:
            document_id = self.documents.pop(file_id, None)
            if document_id is not None:
                deleted.append(document_id)
        return deleted


class MemoryIngest:
    def __init__(self, catalog: MemoryCatalog) -> None:
        self.catalog = catalog
        self.docs: dict[UUID, UUID] = {}
        self.created = 0
        self.calls = 0
        self.tenants: list[UUID] = []

    def ingest(self, document: IngestDocument) -> IngestResult:
        self.calls += 1
        self.tenants.append(document.tenant_id)
        extracted = extract_document(document.content, document.file_type)
        chunks = chunk_text(extracted.text)
        assert document.file_id is not None
        existing = self.docs.get(document.file_id)
        reused = existing is not None
        document_id = existing if existing is not None else uuid4()
        if not reused:
            self.created += 1
            self.docs[document.file_id] = document_id
            self.catalog.documents[document.file_id] = document_id
            self.catalog.chunks[document_id] = [
                StoredChunk(
                    chunk_id=uuid4(),
                    chunk_index=0,
                    content=chunks[0].content,
                    embedding_model=None,
                    dimensions=None,
                )
            ]
        return IngestResult(document_id, len(chunks), "ready", reused)


class MemoryVectorize:
    def __init__(self, catalog: MemoryCatalog) -> None:
        self.catalog = catalog
        self.calls = 0
        self.fail = False

    def vectorize(self, tenant_id: UUID, document_id: UUID) -> VectorizationResult:
        self.calls += 1
        if tenant_id != PGVECTOR_EVAL_TENANT_ID:
            raise AssertionError("租户不正确")
        if self.fail:
            raise EmbeddingAPIError("down")
        current = self.catalog.chunks[document_id][0]
        self.catalog.chunks[document_id] = [
            StoredChunk(
                chunk_id=current.chunk_id,
                chunk_index=0,
                content=current.content,
                embedding_model=EMBEDDING_MODEL_NAME,
                dimensions=EMBEDDING_WIDTH,
            )
        ]
        return VectorizationResult(document_id, 1, EMBEDDING_MODEL_NAME, "ready")


class InnerEmbed:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.model_name = EMBEDDING_MODEL_NAME

    def embed(self, _texts: object) -> list[list[float]]:
        raise AssertionError("查询阶段不应调用文档向量接口")

    def embed_query(self, _query: str) -> list[float]:
        self.clock.advance(0.012)
        return [0.1] * EMBEDDING_WIDTH


class ScriptedRetriever:
    def __init__(self, client: TimedEmbeddingClient, clock: ManualClock) -> None:
        self.client = client
        self.clock = clock
        self.calls = 0
        self.script: dict[str, list[str]] = {}
        self.actual_ids: dict[str, UUID] = {}
        self.similarities: dict[str, float] = {}
        self.errors: set[str] = set()
        self.tenants: list[UUID] = []

    def search(self, tenant_id: UUID, query: str, *, top_k: int = 5, **_kwargs: object) -> list[RetrievalHit]:
        self.calls += 1
        self.tenants.append(tenant_id)
        if query in self.errors:
            raise RuntimeError("检索失败")
        self.client.embed_query(query)
        self.clock.advance(0.03)
        hits: list[RetrievalHit] = []
        for standard in self.script.get(query, [])[:top_k]:
            hits.append(
                RetrievalHit(
                    document_id=UUID(DOCUMENT),
                    chunk_id=self.actual_ids[standard],
                    chunk_index=0,
                    content=standard,
                    char_start=0,
                    char_end=1,
                    similarity=self.similarities.get(standard, 0.8),
                    embedding_model=EMBEDDING_MODEL_NAME,
                )
            )
        return hits


class World:
    def __init__(self) -> None:
        self.clock = ManualClock()
        self.catalog = MemoryCatalog()
        self.ingest = MemoryIngest(self.catalog)
        self.vectors = MemoryVectorize(self.catalog)
        self.timed = TimedEmbeddingClient(InnerEmbed(self.clock), clock=self.clock)
        self.retriever = ScriptedRetriever(self.timed, self.clock)
        self.runner = PgvectorEvaluation(
            catalog=self.catalog,
            ingestion=self.ingest,
            vectorization=self.vectors,
            retriever=self.retriever,
            embedding_client=self.timed,
            clock=self.clock,
        )

    def bind(self, bindings: tuple[PassageBinding, ...]) -> None:
        self.retriever.actual_ids = {item.standard_chunk_id: item.chunk_id for item in bindings}


def test_dataset_passages_stay_one_chunk_after_docx_extraction():
    dataset = load_dataset(DATASET)

    first = passage_document(dataset.passages[0])
    time.sleep(1.1)
    assert passage_document(dataset.passages[0]) == first
    for passage in dataset.passages:
        payload = passage_document(passage)
        extracted = extract_document(payload, "docx")
        chunks = chunk_text(extracted.text)

        assert len(chunks) == 1
        assert chunks[0].content == passage.content
        assert len(document_name_for(dataset.dataset_name, passage.chunk_id)) <= 200


def test_file_id_depends_on_dataset_and_standard_chunk():
    first = file_id_for("enterprise-contract-eval-v1", PASSAGE_A)
    again = file_id_for("enterprise-contract-eval-v1", PASSAGE_A)
    other_dataset = file_id_for("another-eval", PASSAGE_A)
    other_chunk = file_id_for("enterprise-contract-eval-v1", PASSAGE_B)

    assert first == again
    assert first != other_dataset
    assert first != other_chunk


def test_standard_ids_map_to_one_database_chunk():
    world = World()
    dataset = _dataset(
        (
            _passage(PASSAGE_A, TEXT_A),
            _passage(PASSAGE_B, TEXT_B),
        ),
        (_case("ordered", "按返回顺序提问", (PASSAGE_A,)),),
    )
    bindings = world.runner.prepare(dataset)
    world.bind(bindings)
    world.retriever.script = {"按返回顺序提问": [PASSAGE_B, PASSAGE_A]}
    world.retriever.similarities = {PASSAGE_B: 0.2, PASSAGE_A: 0.9}

    report = world.runner.evaluate(dataset, bindings, 5)
    case = report["per_case_results"][0]
    by_standard = {item.standard_chunk_id: item for item in bindings}

    assert case["actual_chunk_ids"] == [
        str(by_standard[PASSAGE_B].chunk_id),
        str(by_standard[PASSAGE_A].chunk_id),
    ]
    assert case["retrieved_chunk_ids"] == [PASSAGE_B, PASSAGE_A]
    assert case["similarities"] == [0.2, 0.9]
    assert case["mrr_at_k"] == 0.5
    assert by_standard[PASSAGE_A].chunk_id != UUID(PASSAGE_A)


def test_prepare_uses_only_the_eval_tenant():
    world = World()
    dataset = _two_passages()

    with pytest.raises(PgvectorEvaluationError, match="专用评估租户"):
        PgvectorEvaluation(
            catalog=world.catalog,
            ingestion=world.ingest,
            vectorization=world.vectors,
            retriever=world.retriever,
            embedding_client=world.timed,
            tenant_id=uuid4(),
        )
    world.runner.prepare(dataset)

    assert world.ingest.tenants
    assert set(world.ingest.tenants) == {PGVECTOR_EVAL_TENANT_ID}
    assert PGVECTOR_EVAL_TENANT_ID != UUID("00000000-0000-4000-8000-00000000e001")


def test_existing_embeddings_are_reused():
    world = World()
    dataset = _two_passages()
    world.runner.prepare(dataset)
    calls = world.vectors.calls

    again = world.runner.prepare(dataset)

    assert world.vectors.calls == calls
    assert all(item.reused_embedding for item in again)
    assert all(item.reused_document for item in again)


def test_second_run_does_not_create_documents():
    world = World()
    dataset = _two_passages()
    first = world.runner.prepare(dataset)
    created = world.ingest.created

    second = world.runner.prepare(dataset)

    assert world.ingest.created == created == 2
    assert world.ingest.calls == 4
    assert [item.chunk_id for item in second] == [item.chunk_id for item in first]
    assert [item.document_id for item in second] == [item.document_id for item in first]


def test_vectorization_failure_does_not_search():
    world = World()
    world.vectors.fail = True

    with pytest.raises(PgvectorEvaluationError, match="不会进入检索"):
        world.runner.evaluate_dataset(_two_passages(), k=5)

    assert world.retriever.calls == 0


def test_retrieved_database_ids_map_back_to_standard_ids():
    world = World()
    dataset = _dataset(
        (_passage(PASSAGE_A, TEXT_A),),
        (_case("map", "映射问题", (PASSAGE_A,)),),
    )
    bindings = world.runner.prepare(dataset)
    world.bind(bindings)
    world.retriever.script = {"映射问题": [PASSAGE_A]}

    report = world.runner.evaluate(dataset, bindings, 5)
    case = report["per_case_results"][0]

    assert case["actual_chunk_ids"] == [str(bindings[0].chunk_id)]
    assert case["retrieved_chunk_ids"] == [PASSAGE_A]
    assert case["expected_chunk_ids"] == [PASSAGE_A]
    assert case["hit_rate_at_k"] == 1.0


def test_refuse_cases_are_excluded_from_retrieval_averages():
    world = World()
    dataset = _dataset(
        (_passage(PASSAGE_A, TEXT_A), _passage(PASSAGE_B, TEXT_B)),
        (
            _case("hit", "命中问题", (PASSAGE_A,)),
            _case("miss", "未命中问题", (PASSAGE_B,)),
            _case("refuse", "食堂补贴", (), refuse=True),
        ),
    )
    bindings = world.runner.prepare(dataset)
    world.bind(bindings)
    world.retriever.script = {
        "命中问题": [PASSAGE_A],
        "未命中问题": [],
        "食堂补贴": [PASSAGE_A],
    }

    report = world.runner.evaluate(dataset, bindings, 5)

    assert report["retrieval_metrics"]["applicable_cases"] == 2
    assert report["retrieval_metrics"]["skipped_cases"] == 1
    assert report["hit_rate_at_k"] == 0.5
    assert report["recall_at_k"] == 0.5
    assert report["mrr_at_k"] == 0.5
    refuse = report["per_case_results"][2]
    assert refuse["status"] == "success"
    assert refuse["hit_rate_at_k"] is None


def test_empty_hits_are_success_and_exceptions_are_failures():
    world = World()
    dataset = _dataset(
        (_passage(PASSAGE_A, TEXT_A),),
        (
            _case("empty", "没有结果", (PASSAGE_A,)),
            _case("boom", "执行失败", (PASSAGE_A,)),
        ),
    )
    bindings = world.runner.prepare(dataset)
    world.bind(bindings)
    world.retriever.script = {"没有结果": []}
    world.retriever.errors.add("执行失败")

    report = world.runner.evaluate(dataset, bindings, 5)
    empty = report["per_case_results"][0]
    failed = report["per_case_results"][1]

    assert empty["status"] == "success"
    assert empty["actual_chunk_ids"] == []
    assert empty["hit_rate_at_k"] == 0.0
    assert failed["status"] == "failed"
    assert "检索失败" in str(failed["error"])
    assert report["successful_cases"] == 1
    assert report["failed_cases"] == 1
    assert report["hit_rate_at_k"] == 0.0
    assert report["failures"][0]["case_id"] == "boom"


def test_embedding_and_retrieval_latency_are_separate():
    world = World()
    dataset = _dataset(
        (_passage(PASSAGE_A, TEXT_A),),
        (_case("timed", "计时问题", (PASSAGE_A,)),),
    )
    bindings = world.runner.prepare(dataset)
    world.bind(bindings)
    world.retriever.script = {"计时问题": [PASSAGE_A]}

    report = world.runner.evaluate(dataset, bindings, 5)
    case = report["per_case_results"][0]

    assert case["embedding_latency_ms"] == 12.0
    assert case["retrieval_latency_ms"] == 30.0
    assert case["total_latency_ms"] == 42.0
    assert report["embedding_latency_ms"] == 12.0
    assert report["retrieval_latency_ms"] == 30.0
    assert report["total_latency_ms"] == 42.0


def test_pgvector_report_contains_required_fields():
    world = World()
    dataset = _dataset(
        (_passage(PASSAGE_A, TEXT_A),),
        (_case("fields", "字段问题", (PASSAGE_A,)),),
    )
    bindings = world.runner.prepare(dataset)
    world.bind(bindings)
    world.retriever.script = {"字段问题": [PASSAGE_A]}
    report = world.runner.evaluate(dataset, bindings, 5)

    for key in (
        "evaluation_id",
        "dataset_name",
        "mode",
        "evaluation_mode",
        "tenant_id",
        "embedding_model",
        "database_vector_dimension",
        "top_k",
        "k",
        "total_cases",
        "successful_cases",
        "failed_cases",
        "hit_rate_at_k",
        "recall_at_k",
        "mrr_at_k",
        "embedding_latency_ms",
        "retrieval_latency_ms",
        "total_latency_ms",
        "retrieval_metrics",
        "answer_metrics",
        "per_case_results",
        "failures",
        "evaluation_time",
        "elapsed_seconds",
    ):
        assert key in report
    assert report["mode"] == "pgvector"
    assert report["evaluation_mode"] == "pgvector"
    assert report["tenant_id"] == str(PGVECTOR_EVAL_TENANT_ID)
    assert report["embedding_model"] == EMBEDDING_MODEL_NAME
    assert report["database_vector_dimension"] == 1024
    assert report["answer_metrics"]["evaluated"] is False
    assert report["answer_metrics"]["case_count"] == 0
    case = report["per_case_results"][0]
    for key in (
        "actual_chunk_ids",
        "expected_chunk_ids",
        "hit_rate_at_k",
        "recall_at_k",
        "mrr_at_k",
        "similarities",
        "embedding_latency_ms",
        "retrieval_latency_ms",
        "total_latency_ms",
    ):
        assert key in case


def test_cleanup_is_off_unless_requested(tmp_path, monkeypatch):
    calls = {"delete": 0}

    def explode(*_args: object, **_kwargs: object) -> None:
        calls["delete"] += 1
        raise AssertionError("默认不应清理")

    monkeypatch.setattr("app.evaluation.pgvector_eval.PostgresCatalog.delete_documents", explode)
    output = tmp_path / "offline.json"

    code = main(["--dataset", str(DATASET), "--top-k", "5", "--output", str(output)])

    assert code == 0
    assert calls["delete"] == 0
    assert main(["--dataset", str(DATASET), "--output", str(tmp_path / "cleanup.json"), "--cleanup"]) == 2
    assert calls["delete"] == 0


def test_cleanup_deletes_only_this_dataset_file_ids():
    world = World()
    dataset = _two_passages()
    world.runner.prepare(dataset)
    foreign_file = uuid4()
    foreign_document = uuid4()
    world.catalog.documents[foreign_file] = foreign_document
    other_task = file_id_for("another-eval", PASSAGE_A)
    world.catalog.documents[other_task] = uuid4()

    deleted = world.runner.cleanup(dataset)

    assert world.catalog.deleted == [
        (
            PGVECTOR_EVAL_TENANT_ID,
            [file_id_for(dataset.dataset_name, PASSAGE_A), file_id_for(dataset.dataset_name, PASSAGE_B)],
        )
    ]
    assert set(deleted) == {world.ingest.docs[file_id_for(dataset.dataset_name, PASSAGE_A)], world.ingest.docs[file_id_for(dataset.dataset_name, PASSAGE_B)]}
    assert world.catalog.documents[foreign_file] == foreign_document
    assert other_task in world.catalog.documents


def test_catalog_rejects_other_tenants_without_using_the_pool():
    class ExplodingPool:
        def connection(self) -> None:
            raise AssertionError("不应连接数据库")

    catalog = PostgresCatalog(ExplodingPool())  # type: ignore[arg-type]

    with pytest.raises(PgvectorEvaluationError, match="评估租户"):
        catalog.delete_documents(uuid4(), [uuid4()])
    with pytest.raises(PgvectorEvaluationError, match="评估租户"):
        catalog.list_chunks(uuid4(), uuid4())


def test_missing_api_key_stops_before_clients(monkeypatch, capsys, tmp_path):
    counters = {"pool": 0, "embed": 0}

    def pool(*_args: object, **_kwargs: object) -> None:
        counters["pool"] += 1
        raise AssertionError("不应连接数据库")

    def embed(*_args: object, **_kwargs: object) -> None:
        counters["embed"] += 1
        raise AssertionError("不应创建 Embedding 客户端")

    monkeypatch.setattr("psycopg_pool.ConnectionPool", pool)
    monkeypatch.setattr("app.knowledge.embeddings.EmbeddingClient.from_settings", embed)

    code = main(
        ["--dataset", str(DATASET), "--output", str(tmp_path / "missing-key.json"), "--live-pgvector"],
        settings=_settings(siliconflow_api_key=""),
    )

    assert code == 2
    assert "SILICONFLOW_API_KEY" in capsys.readouterr().err
    assert counters == {"pool": 0, "embed": 0}
    with pytest.raises(PgvectorConfigError, match="SILICONFLOW_API_KEY"):
        require_pgvector_config(_settings(siliconflow_api_key=""), need_embedding=True)


def test_missing_database_stops_before_clients(monkeypatch, capsys, tmp_path):
    counters = {"pool": 0, "embed": 0}

    def pool(*_args: object, **_kwargs: object) -> None:
        counters["pool"] += 1
        raise AssertionError("不应连接数据库")

    def embed(*_args: object, **_kwargs: object) -> None:
        counters["embed"] += 1
        raise AssertionError("不应创建 Embedding 客户端")

    monkeypatch.setattr("psycopg_pool.ConnectionPool", pool)
    monkeypatch.setattr("app.knowledge.embeddings.EmbeddingClient.from_settings", embed)

    code = main(
        ["--dataset", str(DATASET), "--output", str(tmp_path / "missing-db.json"), "--live-pgvector"],
        settings=_settings(postgres_host=""),
    )

    assert code == 2
    assert "POSTGRES_HOST" in capsys.readouterr().err
    assert counters == {"pool": 0, "embed": 0}


def test_offline_mode_does_not_open_database_or_models(monkeypatch, tmp_path):
    counters = {"pool": 0, "embed": 0, "chat": 0}

    def pool(*_args: object, **_kwargs: object) -> None:
        counters["pool"] += 1
        raise AssertionError("不应连接数据库")

    def embed(*_args: object, **_kwargs: object) -> None:
        counters["embed"] += 1
        raise AssertionError("不应创建 Embedding 客户端")

    def chat(*_args: object, **_kwargs: object) -> None:
        counters["chat"] += 1
        raise AssertionError("不应创建 Chat 客户端")

    monkeypatch.setattr("psycopg_pool.ConnectionPool", pool)
    monkeypatch.setattr("app.knowledge.embeddings.EmbeddingClient.__init__", embed)
    monkeypatch.setattr("app.models.chat.ChatClient.__init__", chat)

    code = main(["--dataset", str(DATASET), "--top-k", "5", "--output", str(tmp_path / "offline.json")])
    report = json.loads((tmp_path / "offline.json").read_text(encoding="utf-8"))

    assert code == 0
    assert report["mode"] == "offline"
    assert counters == {"pool": 0, "embed": 0, "chat": 0}


def test_pgvector_evaluation_does_not_call_chat(monkeypatch):
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("不应调用 Chat Model")

    monkeypatch.setattr("app.models.chat.ChatClient.__init__", explode)
    monkeypatch.setattr("app.models.chat.ChatClient.complete", explode)
    world = World()
    dataset = _dataset((_passage(PASSAGE_A, TEXT_A),), (_case("chat", "不调用对话", (PASSAGE_A,)),))
    bindings = world.runner.prepare(dataset)
    world.bind(bindings)
    world.retriever.script = {"不调用对话": [PASSAGE_A]}

    report = world.runner.evaluate(dataset, bindings, 5)

    assert report["answer_metrics"]["reason"]
    assert "ChatClient" not in inspect.getsource(_run_pgvector)
    assert "RAGService" not in inspect.getsource(_run_pgvector)
    import app.evaluation.pgvector_eval as pgvector_eval

    source = inspect.getsource(pgvector_eval)
    assert "ChatClient" not in source
    assert "RAGService" not in source


def test_prepare_only_does_not_search():
    world = World()

    report = world.runner.prepare_report(_two_passages(), top_k=5)

    assert world.retriever.calls == 0
    assert report["mode"] == "pgvector-prepare"
    assert report["retrieval_metrics"] is None
    assert len(report["documents"]) == 2


def test_conflicting_flags_and_missing_report_directory_exit_nonzero(tmp_path):
    output = str(tmp_path / "out.json")
    assert main(["--dataset", str(DATASET), "--output", output, "--live", "--live-pgvector"]) == 2
    assert main(["--dataset", str(DATASET), "--output", output, "--prepare-only"]) == 2
    assert main(["--dataset", str(DATASET), "--output", output, "--prepare-only", "--cleanup", "--live-pgvector"]) == 2
    assert main(["--dataset", str(DATASET), "--output", str(tmp_path / "missing" / "out.json")]) == 2


def _two_passages() -> EvaluationDataset:
    return _dataset(
        (_passage(PASSAGE_A, TEXT_A), _passage(PASSAGE_B, TEXT_B)),
        (_case("one", "问题一", (PASSAGE_A,)),),
    )


def _dataset(passages: tuple[EvalPassage, ...], cases: tuple[EvalCase, ...]) -> EvaluationDataset:
    return EvaluationDataset(dataset_name="unit-eval", passages=passages, cases=cases)


def _passage(chunk_id: str, content: str) -> EvalPassage:
    return EvalPassage(chunk_id=chunk_id, document_id=DOCUMENT, content=content, title=chunk_id)


def _case(case_id: str, question: str, expected: tuple[str, ...], *, refuse: bool = False) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        question=question,
        expected_chunk_ids=expected,
        expected_facts=() if refuse else ("事实",),
        expected_answer=None,
        should_refuse=refuse,
        description=case_id,
    )


def _settings(**overrides: str) -> Settings:
    values = {
        "postgres_host": "127.0.0.1",
        "postgres_db": "agent",
        "agent_app_password": "secret",
        "siliconflow_api_key": "sk-test",
    }
    values.update(overrides)
    return Settings(**values)
