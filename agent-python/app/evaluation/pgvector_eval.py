"""Evaluate retrieval against PostgreSQL and pgvector.

The dataset's chunk ids stay in the evaluation file. Each passage is ingested
as its own DOCX through KnowledgeIngestionService, embedded only by
VectorizationService, and searched with VectorRetriever. This module does not
call a chat model.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID, uuid5

from psycopg_pool import ConnectionPool

from app.core.config import EMBEDDING_MODEL_NAME, EMBEDDING_WIDTH, Settings
from app.documents.extract import extract_document
from app.evaluation.answers import aggregate_answers
from app.evaluation.dataset import EvalPassage, EvaluationDataset
from app.evaluation.metrics import aggregate_retrieval, score_retrieval
from app.knowledge.chunking import chunk_text
from app.knowledge.ingest import IngestDocument
from app.persistence.postgres import tenant_transaction

# Dedicated tenant for this evaluation task. It is not a business tenant and
# is intentionally absent from application settings.
PGVECTOR_EVAL_TENANT_ID = UUID("00000000-0000-4000-8000-00000000e021")
_FILE_NAMESPACE = UUID("00000000-0000-4000-8000-00000000e210")
_LATENCY_NOTE = (
    "embedding_latency_ms 只计 VectorRetriever.search 内部的查询向量耗时；"
    "retrieval_latency_ms 是 search 总耗时减去该值，包含 SQL 和少量 Python 开销。"
    "这些数字只描述当前机器和当前配置，不代表生产环境。"
)


class PgvectorConfigError(RuntimeError):
    """PostgreSQL or the embedding API key is not configured."""


class PgvectorEvaluationError(RuntimeError):
    """Evaluation data could not be prepared or mapped safely."""


@dataclass(frozen=True)
class StoredChunk:
    chunk_id: UUID
    chunk_index: int
    content: str
    embedding_model: str | None
    dimensions: int | None


@dataclass(frozen=True)
class PassageBinding:
    standard_chunk_id: str
    document_id: UUID
    chunk_id: UUID
    file_id: UUID
    embedding_model: str
    dimensions: int
    reused_document: bool
    reused_embedding: bool


class TimedEmbeddingClient:
    """Delegate embedding calls and time each query-side embed."""

    def __init__(self, inner: object, clock: Callable[[], float] | None = None) -> None:
        self._inner = inner
        self._clock = clock or perf_counter
        model_name = getattr(inner, "model_name", None)
        if not isinstance(model_name, str) or not model_name:
            raise PgvectorEvaluationError("Embedding 客户端缺少 model_name")
        self.model_name = model_name
        self.last_query_seconds: float | None = None
        self.query_calls = 0

    def embed(self, texts: object) -> object:
        return self._inner.embed(texts)  # type: ignore[attr-defined]

    def embed_query(self, query: str) -> object:
        started = self._clock()
        try:
            return self._inner.embed_query(query)  # type: ignore[attr-defined]
        finally:
            self.last_query_seconds = self._clock() - started
            self.query_calls += 1

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


class PostgresCatalog:
    """Read chunk embeddings and delete this task's documents inside RLS."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def list_chunks(self, tenant_id: UUID, document_id: UUID) -> list[StoredChunk]:
        tenant = _require_eval_tenant(tenant_id)
        with tenant_transaction(self._pool, str(tenant)) as connection:
            rows = connection.execute(
                """
                select
                    id,
                    chunk_index,
                    content,
                    embedding_model,
                    case
                        when embedding is null then null
                        else public.vector_dims(embedding)
                    end
                from agent.knowledge_chunk
                where tenant_id = %s and document_id = %s
                order by chunk_index
                """,
                (tenant, document_id),
            ).fetchall()
        return [
            StoredChunk(
                chunk_id=row[0],
                chunk_index=row[1],
                content=row[2],
                embedding_model=row[3],
                dimensions=None if row[4] is None else int(row[4]),
            )
            for row in rows
        ]

    def delete_documents(self, tenant_id: UUID, file_ids: Sequence[UUID]) -> list[UUID]:
        tenant = _require_eval_tenant(tenant_id)
        identifiers = [_require_uuid(file_id) for file_id in file_ids]
        if not identifiers:
            return []
        deleted: list[UUID] = []
        with tenant_transaction(self._pool, str(tenant)) as connection:
            for file_id in identifiers:
                row = connection.execute(
                    """
                    delete from agent.knowledge_document
                    where tenant_id = %s and file_id = %s
                    returning id
                    """,
                    (tenant, file_id),
                ).fetchone()
                if row is not None:
                    deleted.append(row[0])
        return deleted


class PgvectorEvaluation:
    def __init__(
        self,
        *,
        catalog: object,
        ingestion: object,
        vectorization: object,
        retriever: object,
        embedding_client: TimedEmbeddingClient,
        clock: Callable[[], float] | None = None,
        tenant_id: UUID = PGVECTOR_EVAL_TENANT_ID,
    ) -> None:
        if tenant_id != PGVECTOR_EVAL_TENANT_ID:
            raise PgvectorEvaluationError("评估数据只能写入专用评估租户")
        self._catalog = catalog
        self._ingestion = ingestion
        self._vectorization = vectorization
        self._retriever = retriever
        self._embedding_client = embedding_client
        self._clock = clock or perf_counter
        self._tenant_id = tenant_id

    def prepare(self, dataset: EvaluationDataset) -> tuple[PassageBinding, ...]:
        bindings: list[PassageBinding] = []
        for passage in dataset.passages:
            bindings.append(self._prepare_passage(dataset.dataset_name, passage))
        return tuple(bindings)

    def prepare_report(self, dataset: EvaluationDataset, *, top_k: int) -> dict[str, object]:
        started = self._clock()
        bindings = self.prepare(dataset)
        return _prepare_payload(dataset, bindings, top_k, self._clock() - started)

    def evaluate_dataset(self, dataset: EvaluationDataset, *, k: int) -> dict[str, object]:
        started = self._clock()
        bindings = self.prepare(dataset)
        prepare_elapsed = self._clock() - started
        return self.evaluate(dataset, bindings, k, prepare_elapsed=prepare_elapsed)

    def evaluate(
        self,
        dataset: EvaluationDataset,
        bindings: Sequence[PassageBinding],
        k: int,
        *,
        prepare_elapsed: float = 0.0,
    ) -> dict[str, object]:
        index = _binding_index(bindings)
        started = self._clock()
        cases: list[dict[str, object]] = []
        for case in dataset.cases:
            cases.append(self._evaluate_case(case, index, k))
        return _retrieval_payload(
            dataset=dataset,
            bindings=bindings,
            cases=cases,
            k=k,
            elapsed=self._clock() - started,
            prepare_elapsed=prepare_elapsed,
        )

    def cleanup(self, dataset: EvaluationDataset) -> list[UUID]:
        return cleanup_dataset(self._catalog, dataset)

    def _prepare_passage(self, dataset_name: str, passage: EvalPassage) -> PassageBinding:
        payload = passage_document(passage)
        file_id = file_id_for(dataset_name, passage.chunk_id)
        result = self._ingestion.ingest(  # type: ignore[attr-defined]
            IngestDocument(
                tenant_id=self._tenant_id,
                content=payload,
                file_type="docx",
                document_name=document_name_for(dataset_name, passage.chunk_id),
                file_id=file_id,
            )
        )
        if result.chunk_count != 1:
            raise PgvectorEvaluationError(
                f"标准片段 {passage.chunk_id} 被切成 {result.chunk_count} 段，已停止"
            )
        stored = self._read_single_chunk(result.document_id, passage)
        reused_embedding = _embedding_ready(stored)
        if not reused_embedding:
            try:
                self._vectorization.vectorize(self._tenant_id, result.document_id)  # type: ignore[attr-defined]
            except PgvectorEvaluationError:
                raise
            except Exception as exc:
                raise PgvectorEvaluationError(
                    f"向量化失败，已停止，不会进入检索：{exc.__class__.__name__}"
                ) from exc
            stored = self._read_single_chunk(result.document_id, passage)
            if not _embedding_ready(stored):
                raise PgvectorEvaluationError(
                    f"标准片段 {passage.chunk_id} 的向量维度或模型不正确，已停止，不会进入检索"
                )
        return PassageBinding(
            standard_chunk_id=passage.chunk_id,
            document_id=result.document_id,
            chunk_id=stored.chunk_id,
            file_id=file_id,
            embedding_model=stored.embedding_model or "",
            dimensions=stored.dimensions or 0,
            reused_document=bool(result.reused),
            reused_embedding=reused_embedding,
        )

    def _read_single_chunk(self, document_id: UUID, passage: EvalPassage) -> StoredChunk:
        stored = list(self._catalog.list_chunks(self._tenant_id, document_id))  # type: ignore[attr-defined]
        if len(stored) != 1 or stored[0].content != passage.content:
            raise PgvectorEvaluationError(
                f"标准片段 {passage.chunk_id} 与数据库 Chunk 不是一一对应，已停止"
            )
        return stored[0]

    def _evaluate_case(
        self,
        case: object,
        index: dict[str, str],
        k: int,
    ) -> dict[str, object]:
        question = case.question  # type: ignore[attr-defined]
        case_id = case.case_id  # type: ignore[attr-defined]
        expected = list(case.expected_chunk_ids)  # type: ignore[attr-defined]
        started = self._clock()
        calls_before = self._embedding_client.query_calls
        self._embedding_client.last_query_seconds = None
        try:
            hits = self._retriever.search(self._tenant_id, question, top_k=k)  # type: ignore[attr-defined]
            if hits is None or isinstance(hits, (str, bytes)):
                raise PgvectorEvaluationError("检索结果类型不正确")
            ranked = [_read_hit(hit) for hit in list(hits)]
            if self._embedding_client.query_calls != calls_before + 1:
                raise PgvectorEvaluationError("未能单独测量查询 Embedding 耗时")
            embed_seconds = self._embedding_client.last_query_seconds
            if embed_seconds is None:
                raise PgvectorEvaluationError("未能单独测量查询 Embedding 耗时")
            total_seconds = self._clock() - started
            retrieval_seconds = total_seconds - embed_seconds
            if retrieval_seconds < 0:
                if retrieval_seconds > -1e-6:
                    retrieval_seconds = 0.0
                else:
                    raise PgvectorEvaluationError("检索耗时小于查询向量耗时")
            actual_ids = [chunk_id for chunk_id, _similarity in ranked]
            mapped_ids = [index.get(chunk_id, chunk_id) for chunk_id in actual_ids]
            similarities = [similarity for _chunk_id, similarity in ranked]
            scored = score_retrieval(mapped_ids, expected, k)
        except Exception as exc:
            return _failed_case(case, f"{exc.__class__.__name__}: {exc}")
        return {
            "case_id": case_id,
            "description": case.description,  # type: ignore[attr-defined]
            "question": question,
            "status": "success",
            "error": None,
            "expected_chunk_ids": expected,
            "actual_chunk_ids": actual_ids,
            "retrieved_chunk_ids": mapped_ids,
            "similarities": similarities,
            "embedding_latency_ms": _milliseconds(embed_seconds),
            "retrieval_latency_ms": _milliseconds(retrieval_seconds),
            "total_latency_ms": _milliseconds(total_seconds),
            "hit_rate_at_k": scored.hit,
            "recall_at_k": scored.recall,
            "mrr_at_k": scored.mrr,
            "retrieval": {
                "applicable": scored.applicable,
                "hit": scored.hit,
                "recall": scored.recall,
                "mrr": scored.mrr,
                "first_relevant_rank": scored.first_relevant_rank,
                "duplicate_chunk_ids": list(scored.duplicate_chunk_ids),
            },
        }


def require_pgvector_config(settings: Settings, *, need_embedding: bool) -> None:
    missing: list[str] = []
    if not settings.postgres_host:
        missing.append("POSTGRES_HOST")
    if not settings.postgres_db:
        missing.append("POSTGRES_DB")
    if not settings.agent_app_password:
        missing.append("AGENT_APP_PASSWORD")
    if missing:
        raise PgvectorConfigError("未配置 PostgreSQL：" + "、".join(missing))
    if need_embedding and not settings.siliconflow_api_key.strip():
        raise PgvectorConfigError("未配置 SILICONFLOW_API_KEY，无法执行真实向量评估")


def file_id_for(dataset_name: str, standard_chunk_id: str) -> UUID:
    return uuid5(_FILE_NAMESPACE, f"{dataset_name}:{standard_chunk_id}")


def file_ids_for_dataset(dataset: EvaluationDataset) -> list[UUID]:
    return [file_id_for(dataset.dataset_name, passage.chunk_id) for passage in dataset.passages]


def document_name_for(dataset_name: str, standard_chunk_id: str) -> str:
    prefix = f"eval:{dataset_name.strip()[:150]}:"
    name = f"{prefix}{standard_chunk_id}"
    if not 1 <= len(name) <= 200:
        raise PgvectorEvaluationError("评估文档名称长度不合法")
    return name


def passage_document(passage: EvalPassage) -> bytes:
    payload = _docx_bytes(passage.content)
    extracted = extract_document(payload, "docx")
    chunks = chunk_text(extracted.text)
    if len(chunks) != 1 or chunks[0].content != passage.content:
        raise PgvectorEvaluationError(
            f"标准片段 {passage.chunk_id} 不能与切分结果一一对应，已停止，不会按错误的 Chunk ID 计分"
        )
    return payload


def cleanup_dataset(catalog: object, dataset: EvaluationDataset) -> list[UUID]:
    deleted = catalog.delete_documents(PGVECTOR_EVAL_TENANT_ID, file_ids_for_dataset(dataset))  # type: ignore[attr-defined]
    return [item if isinstance(item, UUID) else UUID(str(item)) for item in list(deleted)]


def cleanup_report(dataset: EvaluationDataset, deleted: Sequence[UUID]) -> dict[str, object]:
    return {
        "evaluation_mode": "pgvector-cleanup",
        "mode": "pgvector-cleanup",
        "dataset_name": dataset.dataset_name,
        "tenant_id": str(PGVECTOR_EVAL_TENANT_ID),
        "deleted_document_ids": [str(document_id) for document_id in deleted],
        "deleted_file_ids": [str(file_id) for file_id in file_ids_for_dataset(dataset)],
        "retrieval_metrics": None,
        "answer_metrics": None,
        "per_case_results": [],
        "note": "只删除当前数据集在评估租户下的 file_id。未删除其他租户或其他评估任务的文档。",
    }


def _prepare_payload(
    dataset: EvaluationDataset,
    bindings: Sequence[PassageBinding],
    top_k: int,
    elapsed: float,
) -> dict[str, object]:
    model, dimensions = _vector_identity(bindings)
    return {
        "evaluation_mode": "pgvector",
        "mode": "pgvector-prepare",
        "dataset_name": dataset.dataset_name,
        "tenant_id": str(PGVECTOR_EVAL_TENANT_ID),
        "embedding_model": model,
        "database_vector_dimension": dimensions,
        "top_k": top_k,
        "documents": [_binding_dict(binding) for binding in bindings],
        "retrieval_metrics": None,
        "answer_metrics": None,
        "per_case_results": [],
        "failures": [],
        "total_cases": len(dataset.cases),
        "successful_cases": 0,
        "failed_cases": 0,
        "elapsed_seconds": elapsed,
        "latency_note": _LATENCY_NOTE,
    }


def _retrieval_payload(
    *,
    dataset: EvaluationDataset,
    bindings: Sequence[PassageBinding],
    cases: list[dict[str, object]],
    k: int,
    elapsed: float,
    prepare_elapsed: float,
) -> dict[str, object]:
    from datetime import datetime, timezone
    from uuid import uuid4

    successes = [item for item in cases if item["status"] == "success"]
    failures = [
        {"case_id": str(item["case_id"]), "reason": str(item["error"] or "未知错误")}
        for item in cases
        if item["status"] == "failed"
    ]
    scores = []
    for item in successes:
        retrieval = item["retrieval"]
        if isinstance(retrieval, dict):
            scores.append(_score_from_case(retrieval))
    metrics = aggregate_retrieval(scores)
    metrics["k"] = k
    embedding_ms = _sum_latency(successes, "embedding_latency_ms")
    retrieval_ms = _sum_latency(successes, "retrieval_latency_ms")
    total_ms = _sum_latency(successes, "total_latency_ms")
    model, dimensions = _vector_identity(bindings)
    answers = aggregate_answers([])
    answers["evaluated"] = False
    answers["reason"] = "本次只评估 pgvector 检索，未调用 Chat Model"
    return {
        "evaluation_id": str(uuid4()),
        "evaluation_mode": "pgvector",
        "mode": "pgvector",
        "dataset_name": dataset.dataset_name,
        "tenant_id": str(PGVECTOR_EVAL_TENANT_ID),
        "embedding_model": model,
        "database_vector_dimension": dimensions,
        "top_k": k,
        "k": k,
        "total_cases": len(cases),
        "successful_cases": len(successes),
        "failed_cases": len(failures),
        "hit_rate_at_k": metrics["hit_rate"],
        "recall_at_k": metrics["recall"],
        "mrr_at_k": metrics["mrr"],
        "embedding_latency_ms": embedding_ms,
        "retrieval_latency_ms": retrieval_ms,
        "total_latency_ms": total_ms,
        "retrieval_metrics": metrics,
        "answer_metrics": answers,
        "per_case_results": cases,
        "failures": failures,
        "documents": [_binding_dict(binding) for binding in bindings],
        "evaluation_time": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": elapsed,
        "prepare_elapsed_seconds": prepare_elapsed,
        "latency_note": _LATENCY_NOTE,
    }


def _binding_index(bindings: Sequence[PassageBinding]) -> dict[str, str]:
    index: dict[str, str] = {}
    for binding in bindings:
        key = str(binding.chunk_id)
        if key in index:
            raise PgvectorEvaluationError("实际 Chunk ID 重复，无法映射")
        index[key] = binding.standard_chunk_id
    return index


def _binding_dict(binding: PassageBinding) -> dict[str, object]:
    return {
        "standard_chunk_id": binding.standard_chunk_id,
        "document_id": str(binding.document_id),
        "actual_chunk_id": str(binding.chunk_id),
        "file_id": str(binding.file_id),
        "embedding_model": binding.embedding_model,
        "dimensions": binding.dimensions,
        "reused_document": binding.reused_document,
        "reused_embedding": binding.reused_embedding,
    }


def _vector_identity(bindings: Sequence[PassageBinding]) -> tuple[str, int]:
    if not bindings:
        raise PgvectorEvaluationError("没有可评估的知识片段")
    model = bindings[0].embedding_model
    dimensions = bindings[0].dimensions
    for binding in bindings:
        if binding.embedding_model != model or binding.dimensions != dimensions:
            raise PgvectorEvaluationError("评估片段的向量模型或维度不一致")
        if binding.embedding_model != EMBEDDING_MODEL_NAME or binding.dimensions != EMBEDDING_WIDTH:
            raise PgvectorEvaluationError("入库向量的模型或维度与配置不一致")
    return model, dimensions


def _embedding_ready(chunk: StoredChunk) -> bool:
    return chunk.embedding_model == EMBEDDING_MODEL_NAME and chunk.dimensions == EMBEDDING_WIDTH


def _score_from_case(retrieval: dict[str, object]) -> object:
    from app.evaluation.metrics import RetrievalScore

    duplicates = retrieval["duplicate_chunk_ids"]
    if not isinstance(duplicates, list):
        duplicates = []
    return RetrievalScore(
        applicable=bool(retrieval["applicable"]),
        hit=_optional_float(retrieval["hit"]),
        recall=_optional_float(retrieval["recall"]),
        mrr=_optional_float(retrieval["mrr"]),
        first_relevant_rank=_optional_int(retrieval["first_relevant_rank"]),
        duplicate_chunk_ids=tuple(str(item) for item in duplicates),
    )


def _failed_case(case: object, error: str) -> dict[str, object]:
    return {
        "case_id": case.case_id,  # type: ignore[attr-defined]
        "description": case.description,  # type: ignore[attr-defined]
        "question": case.question,  # type: ignore[attr-defined]
        "status": "failed",
        "error": error,
        "expected_chunk_ids": list(case.expected_chunk_ids),  # type: ignore[attr-defined]
        "actual_chunk_ids": [],
        "retrieved_chunk_ids": [],
        "similarities": [],
        "embedding_latency_ms": None,
        "retrieval_latency_ms": None,
        "total_latency_ms": None,
        "hit_rate_at_k": None,
        "recall_at_k": None,
        "mrr_at_k": None,
        "retrieval": None,
    }


def _read_hit(hit: object) -> tuple[str, float]:
    chunk_id = getattr(hit, "chunk_id", None)
    if isinstance(chunk_id, UUID):
        chunk_id = str(chunk_id)
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise PgvectorEvaluationError("检索结果缺少 chunk_id")
    similarity = getattr(hit, "similarity", None)
    if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
        raise PgvectorEvaluationError("检索结果缺少相似度")
    if not math.isfinite(float(similarity)):
        raise PgvectorEvaluationError("检索结果相似度为非法数值")
    return chunk_id, float(similarity)


def _docx_bytes(text: str) -> bytes:
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

    from docx import Document

    document = Document()
    document.add_paragraph(text)
    raw = BytesIO()
    document.save(raw)
    # Zip entry timestamps change every second. Ingest reuses a document only
    # when the file checksum matches, so the package has to be byte-stable.
    source = ZipFile(BytesIO(raw.getvalue()))
    frozen = BytesIO()
    with ZipFile(frozen, "w") as target:
        for info in source.infolist():
            fixed = ZipInfo(filename=info.filename, date_time=(2020, 1, 1, 0, 0, 0))
            fixed.compress_type = ZIP_DEFLATED
            fixed.external_attr = info.external_attr
            target.writestr(fixed, source.read(info.filename))
    return frozen.getvalue()


def _require_eval_tenant(tenant_id: UUID) -> UUID:
    if not isinstance(tenant_id, UUID) or tenant_id != PGVECTOR_EVAL_TENANT_ID:
        raise PgvectorEvaluationError("清理和读取只能使用评估租户")
    return tenant_id


def _require_uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise PgvectorEvaluationError("file_id 必须是 UUID")
    return value


def _milliseconds(seconds: float) -> float:
    return round(seconds * 1000, 3)


def _sum_latency(cases: Sequence[dict[str, object]], key: str) -> float:
    total = 0.0
    for item in cases:
        value = item[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total += float(value)
    return round(total, 3)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PgvectorEvaluationError("检索指标类型不正确")
    return float(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise PgvectorEvaluationError("检索排名类型不正确")
    return value
