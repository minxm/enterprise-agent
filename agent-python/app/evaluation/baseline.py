"""Offline retriever and RAG wiring that never calls a model provider.

Lexical overlap only ranks the evaluation passages. The real RAGService still
builds the prompt and the empty-retrieval refusal. The chat stand-in echoes
that prompt so answer checks can run without an API key.
"""

from __future__ import annotations

import re
from uuid import UUID

from app.core.config import EMBEDDING_MODEL_NAME
from app.evaluation.dataset import EvalPassage, EvaluationDataset
from app.knowledge.rag import RAGService
from app.knowledge.retrieval import RetrievalHit

_TOKEN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+")
_STOP = frozenset("的了和是在与或及并对将把被有为到从中后前时要需须由再还可会吗呢吧啊个这那其以按让向")


class OfflineEchoChat:
    model_name = "offline-eval-echo"

    def complete(self, *, system: str, user: str) -> str:
        del system
        return user


class LexicalRetriever:
    def __init__(self, passages: tuple[EvalPassage, ...] | list[EvalPassage]) -> None:
        self._passages = tuple(passages)

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
        question_tokens = _tokens(query)
        ranked: list[tuple[float, EvalPassage]] = []
        for passage in self._passages:
            score = _overlap(question_tokens, _tokens(passage.content))
            if score > 0:
                ranked.append((score, passage))
        ranked.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [_hit(passage, score) for score, passage in ranked[:top_k]]


def build_offline_rag(dataset: EvaluationDataset) -> tuple[LexicalRetriever, RAGService]:
    retriever = LexicalRetriever(dataset.passages)
    if OfflineEchoChat.model_name == EMBEDDING_MODEL_NAME:
        raise RuntimeError("离线评估不能使用 Embedding 模型名称")
    return retriever, RAGService(retriever, OfflineEchoChat())


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN.findall(text) if token not in _STOP}


def _overlap(question: set[str], passage: set[str]) -> float:
    if not question or not passage:
        return 0.0
    return len(question & passage) / len(question)


def _hit(passage: EvalPassage, score: float) -> RetrievalHit:
    return RetrievalHit(
        document_id=UUID(passage.document_id),
        chunk_id=UUID(passage.chunk_id),
        chunk_index=0,
        content=passage.content,
        char_start=0,
        char_end=len(passage.content),
        similarity=score,
        embedding_model="offline-lexical",
    )
