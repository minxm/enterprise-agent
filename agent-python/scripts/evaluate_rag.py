"""Evaluate RAG retrieval and answers.

The default path is offline: it ranks the evaluation file in memory and does
not call SiliconFlow or PostgreSQL. Pass --live to call the existing RAGService
with the configured SiliconFlow key. Live mode still does not write to
PostgreSQL. Pass --live-pgvector to ingest the corpus and search with
pgvector. That mode does not call the chat model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.evaluation.baseline import build_offline_rag
from app.evaluation.dataset import EvaluationDataset, EvaluationDatasetError, load_dataset
from app.evaluation.evaluator import EvaluationReport, RAGEvaluator, report_to_dict
from app.evaluation.metrics import EvaluationInputError, require_k


def main(argv: list[str] | None = None, settings: object | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else int(code)
    mode_error = _mode_error(args)
    if mode_error:
        print(mode_error, file=sys.stderr)
        return 2
    try:
        dataset = load_dataset(args.dataset)
        require_k(args.top_k)
    except (EvaluationDatasetError, EvaluationInputError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.live_pgvector:
        return _run_pgvector(args, dataset, settings)
    try:
        if args.live:
            from app.core.config import settings as loaded_settings
            from app.evaluation.live import EvaluationConfigError, build_live_rag

            active = loaded_settings if settings is None else settings
            try:
                retriever, rag_service = build_live_rag(dataset.passages, active)
            except EvaluationConfigError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            mode = "live"
        else:
            retriever, rag_service = build_offline_rag(dataset)
            mode = "offline"
        report = RAGEvaluator().evaluate(
            dataset,
            retriever=retriever,
            rag_service=rag_service,
            k=args.top_k,
            mode=mode,
        )
        _write_report(Path(args.output), report)
    except (EvaluationDatasetError, EvaluationInputError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if report.failed_cases:
        print(f"评估完成，但有 {report.failed_cases} 条样本失败", file=sys.stderr)
        return 1
    print(f"评估完成：{report.successful_cases}/{report.total_cases}，报告 {args.output}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.evaluate_rag",
        description="评估 RAG。默认是离线词面评估，不调用模型，也不连接 PostgreSQL。",
    )
    parser.add_argument("--dataset", required=True, help="评估数据集 JSON 路径")
    parser.add_argument("--top-k", type=int, default=5, help="检索与指标使用的 K，范围 1 到 20")
    parser.add_argument("--output", required=True, help="JSON 报告输出路径")
    parser.add_argument(
        "--live",
        action="store_true",
        help="显式调用现有 RAGService 和 SiliconFlow，只在内存中检索。默认关闭。",
    )
    parser.add_argument(
        "--live-pgvector",
        action="store_true",
        help="显式把评估语料写入专用租户，并用 PostgreSQL + pgvector 检索。只有 --search-strategy answer 会调用 Chat Model。",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="只入库并向量化，不执行检索。必须同时指定 --live-pgvector。",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="只删除当前数据集在评估租户中的文档。默认关闭，必须同时指定 --live-pgvector。",
    )
    parser.add_argument(
        "--search-strategy",
        choices=("vector", "keyword", "hybrid", "all", "hybrid_rerank", "rerank", "answer"),
        default=None,
        help=(
            "真实 pgvector 评估的检索策略。all 比较 vector、keyword 和 hybrid。"
            "rerank 比较 vector、hybrid 和 hybrid_rerank。answer 比较三种策略的问答。默认仍是 vector。"
        ),
    )
    return parser


def _mode_error(args: argparse.Namespace) -> str | None:
    if args.live and args.live_pgvector:
        return "不能同时使用 --live 和 --live-pgvector"
    if args.prepare_only and args.cleanup:
        return "不能同时准备数据和清理数据"
    if (args.prepare_only or args.cleanup) and not args.live_pgvector:
        return "准备或清理评估数据必须同时指定 --live-pgvector"
    if args.search_strategy and not args.live_pgvector:
        return "比较检索策略必须同时指定 --live-pgvector"
    if args.search_strategy and (args.prepare_only or args.cleanup):
        return "准备或清理时不能同时指定 --search-strategy"
    return None


def _run_pgvector(args: argparse.Namespace, dataset: EvaluationDataset, settings: object | None) -> int:
    from psycopg import Error as PsycopgError
    from psycopg_pool import ConnectionPool

    from app.core.config import settings as loaded_settings
    from app.evaluation.pgvector_eval import (
        PgvectorConfigError,
        PgvectorEvaluation,
        PgvectorEvaluationError,
        PostgresCatalog,
        TimedEmbeddingClient,
        cleanup_dataset,
        cleanup_report,
        require_pgvector_config,
    )
    from app.knowledge.embeddings import (
        EmbeddingClient,
        EmbeddingConfigurationError,
        EmbeddingError,
    )
    from app.knowledge.reranker import RerankAPIError, RerankConfigurationError
    from app.knowledge.ingest import KnowledgeIngestionService
    from app.knowledge.retrieval import VectorRetriever
    from app.knowledge.vectorization import VectorizationService
    from app.persistence.postgres import build_agent_conninfo

    active = loaded_settings if settings is None else settings
    strategy = args.search_strategy or "vector"
    needs_rerank = strategy in {"hybrid_rerank", "rerank", "answer"}
    try:
        require_pgvector_config(
            active,
            need_embedding=not args.cleanup and strategy != "keyword",
        )
    except PgvectorConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if needs_rerank and not (getattr(active, "reranker_api_key", "") or getattr(active, "siliconflow_api_key", "")).strip():
        print("未配置 Reranker API Key，无法执行重排序评估", file=sys.stderr)
        return 2
    try:
        pool = ConnectionPool(
            build_agent_conninfo(active),
            min_size=1,
            max_size=4,
            timeout=10,
            kwargs={"options": "-c search_path=agent"},
            open=True,
        )
    except Exception as exc:
        if isinstance(exc, AssertionError):
            raise
        print(f"PostgreSQL 连接失败：{exc.__class__.__name__}", file=sys.stderr)
        return 2
    client: TimedEmbeddingClient | None = None
    opened_rerankers: list[object] = []
    try:
        catalog = PostgresCatalog(pool)
        if args.cleanup:
            deleted = cleanup_dataset(catalog, dataset)
            report = cleanup_report(dataset, deleted)
        else:
            inner = _StoredModelName() if strategy == "keyword" else EmbeddingClient.from_settings(active)
            client = TimedEmbeddingClient(inner)
            vector = VectorRetriever(pool, client)
            runner = PgvectorEvaluation(
                catalog=catalog,
                ingestion=KnowledgeIngestionService(pool),
                vectorization=VectorizationService(pool, client),
                retriever=vector,
                embedding_client=client,
            )
            if args.prepare_only:
                report = runner.prepare_report(dataset, top_k=args.top_k)
            elif strategy == "vector":
                report = runner.evaluate_dataset(dataset, k=args.top_k)
            elif strategy == "answer":
                report = _compare_answers(dataset, runner, pool, client, vector, args.top_k, active, opened_rerankers)
            else:
                report = _compare_search(
                    dataset,
                    runner,
                    pool,
                    client,
                    vector,
                    args.top_k,
                    strategy,
                    active,
                    opened_rerankers,
                )
        _write_report(Path(args.output), report)
    except (PgvectorEvaluationError, EmbeddingConfigurationError, RerankConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except EmbeddingError:
        print("Embedding 调用失败", file=sys.stderr)
        return 2
    except RerankAPIError:
        print("Rerank 调用失败", file=sys.stderr)
        return 2
    except PsycopgError:
        print("PostgreSQL 执行失败", file=sys.stderr)
        return 2
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        for opened in opened_rerankers:
            close = getattr(opened, "close", None)
            if close is not None:
                close()
        if client is not None:
            client.close()
        pool.close()
    if args.cleanup:
        print(f"已清理评估文档 {len(report['deleted_document_ids'])} 篇，报告 {args.output}")
        return 0
    if args.prepare_only:
        print(f"评估数据已准备：{len(report['documents'])} 篇，报告 {args.output}")
        return 0
    if report.get("evaluation_mode") in {"hybrid-compare", "rerank-compare", "rag-answer"}:
        return _print_comparison(report, args.output)
    if report["failed_cases"]:
        print(f"评估完成，但有 {report['failed_cases']} 条样本失败", file=sys.stderr)
        return 1
    metrics = report["retrieval_metrics"] or {}
    print(
        f"pgvector 评估完成：{report['successful_cases']}/{report['total_cases']}，"
        f"Hit Rate@K={metrics.get('hit_rate')}，报告 {args.output}"
    )
    return 0


class _StoredModelName:
    """Lets keyword-only preparation recognize stored vectors without calling a model."""

    @property
    def model_name(self) -> str:
        from app.core.config import EMBEDDING_MODEL_NAME

        return EMBEDDING_MODEL_NAME

    def embed(self, _texts: object) -> list[list[float]]:
        from app.knowledge.embeddings import EmbeddingConfigurationError

        raise EmbeddingConfigurationError("关键词检索不调用 Embedding")

    def embed_query(self, _query: str) -> list[float]:
        from app.knowledge.embeddings import EmbeddingConfigurationError

        raise EmbeddingConfigurationError("关键词检索不调用 Embedding")

    def close(self) -> None:
        return None


def _compare_search(
    dataset,
    runner,
    pool,
    client,
    vector,
    k: int,
    strategy: str,
    settings: object,
    opened_rerankers: list[object],
) -> dict[str, object]:
    from app.evaluation.hybrid_eval import RERANK_COMPARE, STRATEGIES, compare_strategies
    from app.knowledge.hybrid import HybridRetriever
    from app.knowledge.keyword_retrieval import KeywordRetriever
    from app.knowledge.reranker import SiliconFlowReranker
    from app.knowledge.reranking import HybridRerankRetriever, RerankService

    if strategy == "all":
        selected = STRATEGIES
    elif strategy == "rerank":
        selected = RERANK_COMPARE
    else:
        selected = (strategy,)
    keyword = KeywordRetriever(pool)
    hybrid = HybridRetriever(vector, keyword, embedding_client=client)
    rerank_runner = None
    model_name = None
    if "hybrid_rerank" in selected:
        reranker = SiliconFlowReranker.from_settings(settings)
        opened_rerankers.append(reranker)
        model_name = reranker.model_name
        limit = getattr(settings, "reranker_top_n", 20)
        rerank_runner = HybridRerankRetriever(
            hybrid,
            RerankService(reranker, candidate_limit=limit),
            candidate_k=limit,
        )
    bindings = runner.prepare(dataset)
    return compare_strategies(
        dataset,
        bindings,
        vector_retriever=vector,
        keyword_retriever=keyword,
        hybrid_retriever=hybrid,
        k=k,
        embedding_client=client,
        strategies=selected,
        rerank_retriever=rerank_runner,
        reranker_model=model_name,
    )


def _compare_answers(dataset, runner, pool, client, vector, k: int, settings: object, opened: list[object]) -> dict[str, object]:
    from app.evaluation.answer_eval import compare_rag_answers
    from app.knowledge.hybrid import HybridRetriever
    from app.knowledge.keyword_retrieval import KeywordRetriever
    from app.knowledge.rag import RAGService
    from app.knowledge.reranker import SiliconFlowReranker
    from app.knowledge.reranking import HybridRerankRetriever, RerankService
    from app.knowledge.strategies import RetrievalStrategies
    from app.models.chat import ChatClient

    reranker = SiliconFlowReranker.from_settings(settings)
    opened.append(reranker)
    chat = ChatClient.from_settings(settings)
    opened.append(chat)
    keyword = KeywordRetriever(pool)
    hybrid = HybridRetriever(vector, keyword, embedding_client=client)
    limit = getattr(settings, "rag_retrieval_candidate_k", getattr(settings, "reranker_top_n", 20))
    policy = getattr(settings, "rag_reranker_failure_policy", "strict")
    rerank = HybridRerankRetriever(hybrid, RerankService(reranker, candidate_limit=limit), candidate_k=limit)
    strategies = RetrievalStrategies(
        vector=vector,
        hybrid=hybrid,
        hybrid_rerank=rerank,
        failure_policy=policy,
        candidate_k=limit,
    )
    service = RAGService(vector, chat, strategies=strategies, default_strategy="vector", default_top_k=k)
    bindings = runner.prepare(dataset)
    return compare_rag_answers(
        dataset,
        bindings,
        rag_service=service,
        k=k,
        chat_model=getattr(chat, "model_name", None),
        embedding_model=getattr(client, "model_name", None),
        reranker_model=getattr(reranker, "model_name", None),
        failure_policy=policy if isinstance(policy, str) else None,
        candidate_k=limit if isinstance(limit, int) else None,
    )


def _print_comparison(report: dict[str, object], output: str) -> int:
    failed = 0
    strategies = report["strategies"]
    if isinstance(strategies, dict):
        for name, payload in strategies.items():
            if not isinstance(payload, dict):
                continue
            failed += int(payload.get("failed_cases") or 0)
            answers = payload.get("answer_metrics") if isinstance(payload.get("answer_metrics"), dict) else None
            answer_text = ""
            if isinstance(answers, dict):
                answer_text = (
                    f" 拒答准确率={answers.get('refusal_accuracy')}"
                    f" 引用合法率={answers.get('citation_valid_rate')}"
                    f" 事实覆盖={answers.get('fact_keyword_coverage')}"
                )
            print(
                f"{name} Hit Rate@K={payload.get('hit_rate_at_k')} "
                f"Recall@K={payload.get('recall_at_k')} MRR@K={payload.get('mrr_at_k')}{answer_text}"
            )
    changes = report.get("rerank_changes")
    if isinstance(changes, dict):
        print(
            "重排序提前 "
            f"{changes.get('questions_promoted')} 题，"
            f"挤出 Top-K {changes.get('questions_dropped_from_top_k')} 题，"
            f"新进入 Top-K {changes.get('questions_entered_top_k')} 题"
        )
    focus = report.get("focus_case")
    if isinstance(focus, dict):
        hybrid_case = focus.get("hybrid")
        if isinstance(hybrid_case, dict):
            print(f"发票片段进入 Hybrid Top-K：{hybrid_case.get('contains_invoice')}")
        rerank_case = focus.get("hybrid_rerank")
        if isinstance(rerank_case, dict):
            print(f"发票片段进入 Rerank Top-K：{rerank_case.get('contains_invoice')}")
    print(f"检索策略比较完成，报告 {output}")
    if failed:
        print(f"评估完成，但有 {failed} 条策略样本失败", file=sys.stderr)
        return 1
    return 0


def _write_report(path: Path, report: EvaluationReport | dict[str, object]) -> None:
    if not path.parent.exists():
        raise OSError(f"输出目录不存在：{path.parent}")
    payload = report if isinstance(report, dict) else report_to_dict(report)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
