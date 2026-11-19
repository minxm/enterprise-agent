"""Read-only contract lookup. The number is checked before any HTTP call."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.clients.java_backend.client import ContractRecord, JavaBackendError
from app.tools.contract_context import current_actor_ticket

_CONTRACT_NO = re.compile(r"^[^\s\x00-\x1f&?#/\\%]{1,64}$")


@dataclass(frozen=True)
class ContractToolResult:
    status: str
    answer: str
    error_code: str | None = None


class ContractQueryTool:
    """Lookup one contract. This object has no create, update, or delete method."""

    def __init__(self, client: object) -> None:
        self._client = client

    def query(self, contract_no: object) -> ContractToolResult:
        number = validate_contract_no(contract_no)
        if isinstance(number, ContractToolResult):
            return number
        ticket = current_actor_ticket()
        if not ticket:
            return ContractToolResult("failed", "", "contract_unauthorized")
        try:
            record = self._client.get_contract_by_number(number, ticket)  # type: ignore[attr-defined]
        except JavaBackendError as exc:
            return _from_backend(exc)
        except Exception:
            return ContractToolResult("failed", "", "contract_unavailable")
        if not isinstance(record, ContractRecord):
            return ContractToolResult("failed", "", "contract_unavailable")
        return ContractToolResult("answered", format_contract_answer(record), None)


def validate_contract_no(value: object) -> str | ContractToolResult:
    if not isinstance(value, str):
        return ContractToolResult("answered", "合同编号不合法。请提供类似 HT-2026-001 的编号。", "invalid_contract_no")
    number = value.strip()
    if _CONTRACT_NO.fullmatch(number) is None:
        return ContractToolResult("answered", "合同编号不合法。请提供类似 HT-2026-001 的编号。", "invalid_contract_no")
    return number


def format_contract_answer(record: ContractRecord) -> str:
    """Render only fields present on the Java record."""
    lines = [
        f"合同编号：{record.contract_no}",
        f"标题：{record.title}",
        f"类型：{record.contract_type}",
        f"相对方：{record.counterparty_name}",
        f"金额：{record.amount} {record.currency}",
        f"签订日期：{_or_none(record.sign_date)}",
        f"生效日期：{_or_none(record.effective_date)}",
        f"到期日期：{_or_none(record.expiry_date)}",
        f"审批状态：{record.status_label}（{record.status}）",
        f"说明：{_or_none(record.description)}",
    ]
    version = record.current_version
    if version is None:
        lines.append("当前版本：无")
    else:
        lines.append(f"当前版本：{version.version_label}")
        lines.append(f"版本序号：{version.version_no}")
        lines.append(f"版本说明：{version.change_summary}")
        lines.append(f"版本文件：{version.filename}")
        if version.created_at:
            lines.append(f"版本时间：{version.created_at}")
    return "\n".join(lines)


def _from_backend(exc: JavaBackendError) -> ContractToolResult:
    if exc.code == "not_found":
        return ContractToolResult("answered", "未找到该合同。", "contract_not_found")
    if exc.code == "forbidden":
        return ContractToolResult("answered", "无权查询该合同。", "contract_forbidden")
    if exc.code == "invalid":
        return ContractToolResult("answered", "合同编号不合法。请提供类似 HT-2026-001 的编号。", "invalid_contract_no")
    if exc.code == "unauthorized":
        return ContractToolResult("failed", "", "contract_unauthorized")
    return ContractToolResult("failed", "", "contract_unavailable")


def _or_none(value: str | None) -> str:
    return value if value else "无"
