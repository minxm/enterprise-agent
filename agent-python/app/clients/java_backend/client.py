"""Read-only HTTP client for Java internal contract lookup.

The caller supplies a ticket Java already bound to a user. This client never
sends tenant_id or actor_id, and it has no write methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

_SAFE = {
    400: ("invalid", "合同编号不合法"),
    401: ("unauthorized", "合同查询未授权"),
    403: ("forbidden", "无权查询合同"),
    404: ("not_found", "合同不存在"),
}


class JavaBackendError(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class ContractVersionRecord:
    version_no: int
    version_label: str
    change_summary: str
    filename: str
    created_at: str | None


@dataclass(frozen=True)
class ContractRecord:
    contract_no: str
    title: str
    contract_type: str
    counterparty_name: str
    amount: str
    currency: str
    sign_date: str | None
    effective_date: str | None
    expiry_date: str | None
    status: str
    status_label: str
    description: str | None
    current_version: ContractVersionRecord | None


class JavaBackendClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0, transport: httpx.BaseTransport | None = None) -> None:
        self._token = token
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def get_contract_by_number(self, contract_no: str, ticket: str) -> ContractRecord:
        try:
            response = self._client.get(
                "/internal/v1/contracts",
                params={"contractNo": contract_no},
                headers={
                    "X-Agent-Token": self._token,
                    "X-Actor-Ticket": ticket,
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise JavaBackendError("unavailable", "合同服务暂时不可用") from exc
        if response.status_code != 200:
            code, message = _SAFE.get(response.status_code, ("unavailable", "合同服务暂时不可用"))
            raise JavaBackendError(code, message)
        try:
            payload = response.json()
        except ValueError as exc:
            raise JavaBackendError("invalid_response", "合同数据无法解析") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise JavaBackendError("invalid_response", "合同数据无法解析")
        return _record(data)


def _record(data: dict[str, Any]) -> ContractRecord:
    try:
        version = _version(data.get("currentVersion"))
        return ContractRecord(
            contract_no=_text(data, "contractNo"),
            title=_text(data, "title"),
            contract_type=_text(data, "contractType"),
            counterparty_name=_text(data, "counterpartyName"),
            amount=_text(data, "amount"),
            currency=_text(data, "currency"),
            sign_date=_optional(data, "signDate"),
            effective_date=_optional(data, "effectiveDate"),
            expiry_date=_optional(data, "expiryDate"),
            status=_text(data, "status"),
            status_label=_text(data, "statusLabel"),
            description=_optional(data, "description"),
            current_version=version,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise JavaBackendError("invalid_response", "合同数据无法解析") from exc


def _version(value: object) -> ContractVersionRecord | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("currentVersion")
    version_no = value.get("versionNo")
    if isinstance(version_no, bool) or not isinstance(version_no, int):
        raise TypeError("versionNo")
    created = value.get("createdAt")
    return ContractVersionRecord(
        version_no=version_no,
        version_label=_text(value, "versionLabel"),
        change_summary=_text(value, "changeSummary"),
        filename=_text(value, "filename"),
        created_at=created if isinstance(created, str) and created else None,
    )


def _text(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(key)
    return value


def _optional(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(key)
    return value
