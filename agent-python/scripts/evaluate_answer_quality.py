"""Score answer quality without changing retrieval or the production answer path.

The default run is deterministic and does not call a model. --judge sends the
small calibration set to the configured judge. It does not call the answer model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.evaluation.answer_quality import load_calibration, rescore_answers, score_calibration
from app.evaluation.dataset import EvaluationDatasetError, load_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评估 RAG 答案质量。默认不调用 Judge。")
    parser.add_argument("--dataset", default="evaluation_data/enterprise_contract_eval.json")
    parser.add_argument("--calibration", default="evaluation_data/answer_quality_calibration.json")
    parser.add_argument("--answers", default="reports/rag-day24.json")
    parser.add_argument("--output", default="reports/answer-quality-day25.json")
    parser.add_argument("--judge", action="store_true", help="对人工校准集调用 Judge。默认关闭。")
    args = parser.parse_args(argv)
    try:
        dataset = load_dataset(args.dataset)
        calibration = load_calibration(args.calibration)
    except (EvaluationDatasetError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    saved = _saved_answers(args.answers)
    judge = None
    judge_model = None
    if args.judge:
        judge, judge_model, error = _build_judge()
        if error is not None:
            print(error, file=sys.stderr)
            return 2
    report = {
        "evaluation_mode": "answer-quality-v2",
        "dataset_name": dataset.dataset_name,
        "judge_enabled": judge is not None,
        "judge_model": judge_model,
        "faithfulness_is_judge_only": True,
        "saved_answers": None if saved is None else {
            name: {
                "previous_answer_metrics": payload.get("answer_metrics"),
                "previous_hit_rate_at_k": payload.get("hit_rate_at_k"),
                "previous_recall_at_k": payload.get("recall_at_k"),
                "previous_mrr_at_k": payload.get("mrr_at_k"),
                "quality": rescore_answers(dataset, rows),
            }
            for name, (payload, rows) in saved.items()
        },
        "calibration": score_calibration(calibration, judge=judge),
    }
    try:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        close = getattr(judge, "close", None)
        if close is not None:
            close()
    calibration_report = report["calibration"]
    print(
        "答案质量评估完成："
        f"确定性一致率={calibration_report.get('deterministic_agreement')} "
        f"Judge 状态={calibration_report.get('judge_status')} "
        f"报告 {args.output}"
    )
    if judge is not None and calibration_report.get("judge_evaluation_success_rate") != 1:
        return 1
    return 0


def _saved_answers(path: str) -> dict[str, tuple[dict[str, object], list[dict[str, object]]]] | None:
    source = Path(path)
    if not source.is_file():
        return None
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    strategies = payload.get("strategies")
    if not isinstance(strategies, dict):
        return None
    grouped: dict[str, tuple[dict[str, object], list[dict[str, object]]]] = {}
    for name, strategy in strategies.items():
        if not isinstance(strategy, dict):
            continue
        cases = strategy.get("per_case_results")
        if not isinstance(cases, list):
            continue
        grouped[str(name)] = (strategy, [item for item in cases if isinstance(item, dict)])
    return grouped


def _build_judge() -> tuple[object | None, str | None, str | None]:
    from app.core.config import settings
    from app.evaluation.judge_client import JudgeClient, JudgeConfigurationError

    try:
        client = JudgeClient(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            model=settings.rag_eval_judge_model,
            timeout_seconds=settings.rag_eval_judge_timeout_seconds,
        )
    except JudgeConfigurationError as exc:
        return None, None, str(exc)
    return client, client.model_name, None


if __name__ == "__main__":
    raise SystemExit(main())
