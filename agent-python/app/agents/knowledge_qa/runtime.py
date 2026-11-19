"""Assemble the production Knowledge QA service from the existing runtime.

This does not change retrieval or rerank behavior. It only connects the clients
that those modules already use. The checkpointer is the process-wide pool.
"""

from __future__ import annotations

from app.agents.knowledge_qa.service import KnowledgeQAService
from app.clients.java_backend.client import JavaBackendClient
from app.core.config import Settings
from app.tools.contract_query import ContractQueryTool
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.hybrid import HybridRetriever
from app.knowledge.keyword_retrieval import KeywordRetriever
from app.knowledge.rag import RAGService
from app.knowledge.reranker import SiliconFlowReranker
from app.knowledge.reranking import HybridRerankRetriever, RerankService
from app.knowledge.retrieval import VectorRetriever
from app.knowledge.strategies import RetrievalStrategies
from app.models.chat import ChatClient


class KnowledgeQaRuntime:
    def __init__(self, service: KnowledgeQAService, closers: list[object]) -> None:
        self.service = service
        self._closers = closers

    def close(self) -> None:
        for closer in self._closers:
            close = getattr(closer, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                continue


def build_knowledge_qa_runtime(settings: Settings, pool: object, checkpointer: object) -> KnowledgeQaRuntime:
    embedding = EmbeddingClient.from_settings(settings)
    chat = ChatClient.from_settings(settings)
    closers: list[object] = [embedding, chat]
    vector = VectorRetriever(pool, embedding)
    keyword = KeywordRetriever(pool)
    hybrid = HybridRetriever(vector, keyword, embedding_client=embedding)
    hybrid_rerank = None
    if settings.reranker_enabled:
        reranker = SiliconFlowReranker.from_settings(settings)
        closers.append(reranker)
        hybrid_rerank = HybridRerankRetriever(
            hybrid,
            RerankService(reranker, candidate_limit=settings.rag_retrieval_candidate_k),
            candidate_k=settings.rag_retrieval_candidate_k,
        )
    strategies = RetrievalStrategies(
        vector=vector,
        hybrid=hybrid,
        hybrid_rerank=hybrid_rerank,
        failure_policy=settings.rag_reranker_failure_policy,
        candidate_k=settings.rag_retrieval_candidate_k,
    )
    rag = RAGService(
        vector,
        chat,
        strategies=strategies,
        default_strategy=settings.rag_retrieval_strategy,
        default_top_k=settings.rag_retrieval_top_k,
    )
    contract_tool = None
    if settings.java_base_url and settings.agent_internal_token:
        backend = JavaBackendClient(
            settings.java_base_url,
            settings.agent_internal_token,
            timeout=float(settings.java_timeout_seconds),
        )
        closers.append(backend)
        contract_tool = ContractQueryTool(backend)
    return KnowledgeQaRuntime(
        KnowledgeQAService(rag, checkpointer=checkpointer, contract_tool=contract_tool),
        closers,
    )
