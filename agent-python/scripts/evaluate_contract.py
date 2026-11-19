"""Evaluate contract-query routing and fixture-backed answers.

The default run does not call Java, PostgreSQL, or a chat model. Pass --live
only when CONTRACT_EVAL_JAVA_URL and CONTRACT_EVAL_BEARER are already set.
Those values are not printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.evaluation.contract_dataset import ContractDatasetError, load_contract_dataset
from app.evaluation.contract_evaluator import ContractEvaluator, report_to_dict
from app.evaluation.contract_live import LiveConfig, not_executed, run_live


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评估合同查询 Agent。默认使用 Fixture，不连接数据库。")
    parser.add_argument("--dataset", default="evaluation_data/contract_query_eval.json")
    parser.add_argument("--output", default="reports/contract-query-day35.json")
    parser.add_argument("--live-output", default="", help="真实 Java 评估的独立 JSON。不并入离线分数。")
    parser.add_argument("--live", action="store_true", help="额外通过 Java 问答接口验证已有真实合同。默认关闭。")
    args = parser.parse_args(argv)
    try:
        dataset = load_contract_dataset(args.dataset)
        report = report_to_dict(ContractEvaluator().evaluate(dataset))
        live = _live(dataset) if args.live else not_executed(
            "未传入 --live。离线结果不能当作真实数据库评估。"
        )
        if args.live_output:
            live_path = Path(args.live_output)
            live_path.parent.mkdir(parents=True, exist_ok=True)
            live_path.write_text(json.dumps(live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["live_database"] = _live_pointer(live, args.live_output)
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ContractDatasetError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    metrics = report["metrics"]
    print(
        "离线合同评估："
        f"{report['passed_cases']}/{report['total_cases']}，"
        f"意图={_accuracy(metrics, 'intent_accuracy')}，"
        f"编号={_accuracy(metrics, 'contract_number_extraction_accuracy')}，"
        f"工具调用={_accuracy(metrics, 'tool_call_accuracy')}，"
        f"参数={_accuracy(metrics, 'tool_argument_accuracy')}，"
        f"字段={_accuracy(metrics, 'field_accuracy')}，"
        f"报告 {args.output}"
    )
    if isinstance(live, dict) and live.get("status") == "not_executed":
        print(f"真实 Java 评估未执行：{live.get('reason')}", file=sys.stderr)
    if isinstance(live, dict) and live.get("status") == "executed":
        print(
            "真实 Java 评估："
            f"{live.get('passed_cases')}/{live.get('executed_cases')} 执行通过，"
            f"跳过 {live.get('skipped_cases')}"
        )
    if report["execution_failures"] or report["check_failures"]:
        print(f"离线评估有 {len(report['failures'])} 条样本未通过", file=sys.stderr)
        return 1
    if isinstance(live, dict) and live.get("status") == "executed" and live.get("failed_cases"):
        print(f"真实 Java 评估有 {live['failed_cases']} 条未通过", file=sys.stderr)
        return 1
    return 0


def _live(dataset: object) -> dict[str, object]:
    base_url = os.environ.get("CONTRACT_EVAL_JAVA_URL", "")
    bearer = os.environ.get("CONTRACT_EVAL_BEARER", "")
    if not base_url.strip() or not bearer.strip():
        return not_executed("缺少 CONTRACT_EVAL_JAVA_URL 或 CONTRACT_EVAL_BEARER，未连接 Java。")
    try:
        return run_live(
            dataset,
            LiveConfig(
                base_url=base_url,
                bearer=bearer,
                no_permission_bearer=os.environ.get("CONTRACT_EVAL_BEARER_NO_PERMISSION", ""),
                other_tenant_bearer=os.environ.get("CONTRACT_EVAL_BEARER_OTHER_TENANT", ""),
            ),
        )
    except Exception:
        return not_executed("Java 问答接口调用失败")


def _live_pointer(live: dict[str, object], live_output: str) -> dict[str, object]:
    """Keep live totals out of the offline score.

    A separate live file holds the Java results. The offline document only
    records whether that run happened.
    """
    if live.get("status") == "executed":
        return {
            "status": "separate_report",
            "reason": "真实 Java 评估单独成文，不并入离线分子、分母或通过数。",
            "report": live_output,
            "executed_cases": live.get("executed_cases"),
            "passed_cases": live.get("passed_cases"),
            "failed_cases": live.get("failed_cases"),
            "skipped_cases": live.get("skipped_cases"),
        }
    return {
        "status": live.get("status"),
        "reason": live.get("reason"),
        "ground_truth": live.get("ground_truth"),
    }


def _accuracy(metrics: object, name: str) -> object:
    if not isinstance(metrics, dict):
        return None
    item = metrics.get(name)
    if not isinstance(item, dict):
        return None
    return item.get("accuracy")


if __name__ == "__main__":
    raise SystemExit(main())
