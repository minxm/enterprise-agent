"""Build a real RAGService over the evaluation corpus only.

Passage vectors stay in memory. This module does not read or write PostgreSQL.
"""

from __future__ import annotations

import math
from uuid import UUID

from app.core.config import Settings
from app.evaluation.dataset import EvalPassage
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.rag import RAGService
from app.knowledge.retrieval import RetrievalHit
from app.models.chat import ChatClient

class EvaluationConfigError(RuntimeError):
    """Live evaluation is missing a required setting."""


class EmbeddingCorpusRetriever:
    def __init__(self, passages: tuple[EvalPassage, ...] | list[EvalPassage], embedding_client: object) -> None:
        self._passages = tuple(passages)
        self._embedding_client = embedding_client
        self._vectors = self._embed_passages()

    def search(
        self,
        tenant_id: UUID,
        query: str,
        *,
        top_k: int = 5,
        similarity_threshold: float | None = None,
        document_id: UUID | None = None,
    ) -> list[RetrievalHit]:
        del tenant_id, similarity_threshold, document_id
        query_vector = self._embedding_client.embed_query(query)  # type: ignore[attr-defined]
        ranked = [(_cosine(query_vector, vector), passage) for passage, vector in self._vectors]
        ranked.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [
            _hit(passage, score, self._embedding_client.model_name)  # type: ignore[attr-defined]
            for score, passage in ranked[:top_k]
        ]

    def _embed_passages(self) -> list[tuple[EvalPassage, list[float]]]:
        if not self._passages:
            return []
        vectors = self._embedding_client.embed([passage.content for passage in self._passages])  # type: ignore[attr-defined]
        if not isinstance(vectors, list) or len(vectors) != len(self._passages):
            raise EvaluationConfigError("评估语料向量数量与片段数量不一致")
        return list(zip(self._passages, vectors, strict=True))


def build_live_rag(
    passages: tuple[EvalPassage, ...] | list[EvalPassage],
    settings: Settings,
) -> tuple[EmbeddingCorpusRetriever, RAGService]:
    if not settings.siliconflow_api_key.strip():
        raise EvaluationConfigError("未配置 SILICONFLOW_API_KEY，无法执行真实 RAG 评估")
    embedding_client = EmbeddingClient.from_settings(settings)
    chat_client = ChatClient.from_settings(settings)
    retriever = EmbeddingCorpusRetriever(passages, embedding_client)
    return retriever, RAGService(retriever, chat_client)


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _hit(passage: EvalPassage, score: float, model_name: str) -> RetrievalHit:
    return RetrievalHit(
        document_id=UUID(passage.document_id),
        chunk_id=UUID(passage.chunk_id),
        chunk_index=0,
        content=passage.content,
        char_start=0,
        char_end=len(passage.content),
        similarity=score,
        embedding_model=model_name,
    )
