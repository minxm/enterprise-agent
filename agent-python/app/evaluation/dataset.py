"""Load an isolated RAG evaluation dataset.

Passage and case ids live only in this file. Loading it does not read or write
the production knowledge tables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


class EvaluationDatasetError(ValueError):
    """The evaluation dataset is missing a field or has the wrong type."""


@dataclass(frozen=True)
class EvalPassage:
    chunk_id: str
    document_id: str
    content: str
    title: str


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    question: str
    expected_chunk_ids: tuple[str, ...]
    expected_facts: tuple[str, ...]
    expected_answer: str | None
    should_refuse: bool
    description: str


@dataclass(frozen=True)
class EvaluationDataset:
    dataset_name: str
    passages: tuple[EvalPassage, ...]
    cases: tuple[EvalCase, ...]

    def passage_map(self) -> dict[str, EvalPassage]:
        return {passage.chunk_id: passage for passage in self.passages}


_ROOT_FIELDS = frozenset({"dataset_name", "passages", "cases"})
_PASSAGE_FIELDS = frozenset({"chunk_id", "document_id", "content", "title"})
_CASE_FIELDS = frozenset(
    {
        "case_id",
        "question",
        "expected_chunk_ids",
        "expected_facts",
        "expected_answer",
        "should_refuse",
        "description",
    }
)


def load_dataset(path: Path | str) -> EvaluationDataset:
    source = Path(path)
    if not source.is_file():
        raise EvaluationDatasetError(f"评估数据集不存在：{source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvaluationDatasetError("评估数据集不是合法 JSON") from exc
    return parse_dataset(payload)


def parse_dataset(payload: object) -> EvaluationDataset:
    if not isinstance(payload, dict):
        raise EvaluationDatasetError("评估数据集必须是 JSON 对象")
    _require_exact_fields(payload, _ROOT_FIELDS, "数据集")
    name = _require_text(payload.get("dataset_name"), "dataset_name")
    passages_raw = payload.get("passages")
    cases_raw = payload.get("cases")
    if not isinstance(passages_raw, list) or not passages_raw:
        raise EvaluationDatasetError("passages 必须是非空数组")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise EvaluationDatasetError("cases 必须是非空数组")
    passages = tuple(_parse_passage(item, index) for index, item in enumerate(passages_raw))
    known = {passage.chunk_id for passage in passages}
    if len(known) != len(passages):
        raise EvaluationDatasetError("passages 中的 chunk_id 不能重复")
    cases = tuple(_parse_case(item, index, known) for index, item in enumerate(cases_raw))
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise EvaluationDatasetError("cases 中的 case_id 不能重复")
    return EvaluationDataset(dataset_name=name, passages=passages, cases=cases)


def _parse_passage(item: object, index: int) -> EvalPassage:
    label = f"passages[{index}]"
    if not isinstance(item, dict):
        raise EvaluationDatasetError(f"{label} 必须是对象")
    _require_exact_fields(item, _PASSAGE_FIELDS, label)
    return EvalPassage(
        chunk_id=_require_uuid(item.get("chunk_id"), f"{label}.chunk_id"),
        document_id=_require_uuid(item.get("document_id"), f"{label}.document_id"),
        content=_require_text(item.get("content"), f"{label}.content"),
        title=_require_text(item.get("title"), f"{label}.title"),
    )


def _parse_case(item: object, index: int, known_chunks: set[str]) -> EvalCase:
    label = f"cases[{index}]"
    if not isinstance(item, dict):
        raise EvaluationDatasetError(f"{label} 必须是对象")
    _require_exact_fields(item, _CASE_FIELDS, label)
    case_id = _require_text(item.get("case_id"), f"{label}.case_id")
    label = case_id
    question = _require_text(item.get("question"), f"{label}.question")
    expected_chunk_ids = _require_id_list(item.get("expected_chunk_ids"), f"{label}.expected_chunk_ids")
    expected_facts = _require_text_list(item.get("expected_facts"), f"{label}.expected_facts")
    expected_answer = _require_optional_text(item.get("expected_answer"), f"{label}.expected_answer")
    should_refuse = _require_bool(item.get("should_refuse"), f"{label}.should_refuse")
    description = _require_text(item.get("description"), f"{label}.description")
    unknown = [chunk_id for chunk_id in expected_chunk_ids if chunk_id not in known_chunks]
    if unknown:
        raise EvaluationDatasetError(f"{label}.expected_chunk_ids 引用了数据集中不存在的片段：{unknown[0]}")
    if not should_refuse and not expected_chunk_ids:
        raise EvaluationDatasetError(f"{label} 需要回答时 expected_chunk_ids 不能为空")
    if not should_refuse and not expected_facts and expected_answer is None:
        raise EvaluationDatasetError(f"{label} 需要 expected_facts 或 expected_answer")
    return EvalCase(
        case_id=case_id,
        question=question,
        expected_chunk_ids=tuple(expected_chunk_ids),
        expected_facts=tuple(expected_facts),
        expected_answer=expected_answer,
        should_refuse=should_refuse,
        description=description,
    )


def _require_exact_fields(payload: dict[str, object], expected: frozenset[str], label: str) -> None:
    missing = sorted(expected - payload.keys())
    extra = sorted(payload.keys() - expected)
    if missing:
        raise EvaluationDatasetError(f"{label} 缺少字段：{missing[0]}")
    if extra:
        raise EvaluationDatasetError(f"{label} 包含未知字段：{extra[0]}")


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationDatasetError(f"{label} 必须是非空字符串")
    return value.strip()


def _require_optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, label)


def _require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise EvaluationDatasetError(f"{label} 必须是布尔值")
    return value


def _require_uuid(value: object, label: str) -> str:
    text = _require_text(value, label)
    try:
        parsed = UUID(text)
    except ValueError:
        raise EvaluationDatasetError(f"{label} 必须是 UUID") from None
    return str(parsed)


def _require_id_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise EvaluationDatasetError(f"{label} 必须是数组")
    parsed = [_require_uuid(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(parsed) != len(set(parsed)):
        raise EvaluationDatasetError(f"{label} 不能包含重复片段")
    return parsed


def _require_text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise EvaluationDatasetError(f"{label} 必须是数组")
    return [_require_text(item, f"{label}[{index}]") for index, item in enumerate(value)]
