"""Offline memory evaluation. It does not call a hosted model."""

from __future__ import annotations

import json
from pathlib import Path

from app.evaluation.memory_evaluator import MemoryEvaluator, write_guard_holds


def test_offline_memory_evaluation_separates_control_from_model_compliance(tmp_path: Path):
    report = MemoryEvaluator().evaluate()
    assert report.mode == "offline-fixture"
    assert report.model_compliance == "not_executed"
    assert report.passed_cases == report.total_cases
    assert report.failures == ()
    assert write_guard_holds()
    for name, item in report.dimensions.items():
        assert item["passed"] == item["total"], name

    payload = {
        "mode": report.mode,
        "model_compliance": report.model_compliance,
        "total_cases": report.total_cases,
        "passed_cases": report.passed_cases,
        "dimensions": report.dimensions,
        "failures": list(report.failures),
        "cases": [
            {
                "case_id": item.case_id,
                "description": item.description,
                "passed": item.passed,
                "recall_pass": item.recall_pass,
                "permission_pass": item.permission_pass,
                "expression_pass": item.expression_pass,
                "authority_pass": item.authority_pass,
                "escalation_pass": item.escalation_pass,
                "acceptance_pass": item.acceptance_pass,
            }
            for item in report.per_case_results
        ],
    }
    destination = Path(__file__).resolve().parents[2] / "reports" / "day41-memory-offline.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror = tmp_path / "day41-memory-offline.json"
    mirror.write_text(destination.read_text(encoding="utf-8"), encoding="utf-8")
    assert json.loads(mirror.read_text(encoding="utf-8"))["passed_cases"] == report.total_cases
