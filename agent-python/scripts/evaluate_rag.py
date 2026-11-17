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
        help="显式把评估语料写入专用租户，并用 PostgreSQL + pgvector 检索。不调用 Chat Model。",
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
    return parser


def _mode_error(args: argparse.Namespace) -> str | None:
    if args.live and args.live_pgvector:
        return "不能同时使用 --live 和 --live-pgvector"
    if args.prepare_only and args.cleanup:
        return "不能同时准备数据和清理数据"
    if (args.prepare_only or args.cleanup) and not args.live_pgvector:
        return "准备或清理评估数据必须同时指定 --live-pgvector"
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
    from app.knowledge.ingest import KnowledgeIngestionService
    from app.knowledge.retrieval import VectorRetriever
    from app.knowledge.vectorization import VectorizationService
    from app.persistence.postgres import build_agent_conninfo

    active = loaded_settings if settings is None else settings
    try:
        require_pgvector_config(active, need_embedding=not args.cleanup)
    except PgvectorConfigError as exc:
        print(str(exc), file=sys.stderr)
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
    try:
        catalog = PostgresCatalog(pool)
        if args.cleanup:
            deleted = cleanup_dataset(catalog, dataset)
            report = cleanup_report(dataset, deleted)
        else:
            client = TimedEmbeddingClient(EmbeddingClient.from_settings(active))
            runner = PgvectorEvaluation(
                catalog=catalog,
                ingestion=KnowledgeIngestionService(pool),
                vectorization=VectorizationService(pool, client),
                retriever=VectorRetriever(pool, client),
                embedding_client=client,
            )
            if args.prepare_only:
                report = runner.prepare_report(dataset, top_k=args.top_k)
            else:
                report = runner.evaluate_dataset(dataset, k=args.top_k)
        _write_report(Path(args.output), report)
    except (PgvectorEvaluationError, EmbeddingConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except EmbeddingError:
        print("Embedding 调用失败", file=sys.stderr)
        return 2
    except PsycopgError:
        print("PostgreSQL 执行失败", file=sys.stderr)
        return 2
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()
        pool.close()
    if args.cleanup:
        print(f"已清理评估文档 {len(report['deleted_document_ids'])} 篇，报告 {args.output}")
        return 0
    if args.prepare_only:
        print(f"评估数据已准备：{len(report['documents'])} 篇，报告 {args.output}")
        return 0
    if report["failed_cases"]:
        print(f"评估完成，但有 {report['failed_cases']} 条样本失败", file=sys.stderr)
        return 1
    metrics = report["retrieval_metrics"] or {}
    print(
        f"pgvector 评估完成：{report['successful_cases']}/{report['total_cases']}，"
        f"Hit Rate@K={metrics.get('hit_rate')}，报告 {args.output}"
    )
    return 0


def _write_report(path: Path, report: EvaluationReport | dict[str, object]) -> None:
    if not path.parent.exists():
        raise OSError(f"输出目录不存在：{path.parent}")
    payload = report if isinstance(report, dict) else report_to_dict(report)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
