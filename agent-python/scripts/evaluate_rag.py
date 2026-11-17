"""Evaluate RAG retrieval and answers.

The default path is offline: it ranks the evaluation file in memory and does
not call SiliconFlow. Pass --live to call the existing RAGService with the
configured SiliconFlow key. Live mode still does not write to PostgreSQL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.evaluation.baseline import build_offline_rag
from app.evaluation.dataset import EvaluationDatasetError, load_dataset
from app.evaluation.evaluator import EvaluationReport, RAGEvaluator, report_to_dict
from app.evaluation.metrics import EvaluationInputError


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else int(code)
    try:
        dataset = load_dataset(args.dataset)
        if args.live:
            from app.core.config import settings
            from app.evaluation.live import EvaluationConfigError, build_live_rag

            try:
                retriever, rag_service = build_live_rag(dataset.passages, settings)
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
        description="离线评估 RAG 检索和答案。默认不调用模型 API，也不连接生产知识库。",
    )
    parser.add_argument("--dataset", required=True, help="评估数据集 JSON 路径")
    parser.add_argument("--top-k", type=int, default=5, help="检索与指标使用的 K，范围 1 到 20")
    parser.add_argument("--output", required=True, help="JSON 报告输出路径")
    parser.add_argument(
        "--live",
        action="store_true",
        help="显式调用现有 RAGService 和 SiliconFlow。默认关闭。",
    )
    return parser


def _write_report(path: Path, report: EvaluationReport) -> None:
    if not path.parent.exists():
        raise OSError(f"输出目录不存在：{path.parent}")
    path.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
