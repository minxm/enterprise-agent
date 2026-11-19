import inspect

import httpx
import pytest

from app.clients.java_backend.client import ContractRecord, ContractVersionRecord, JavaBackendClient, JavaBackendError
from app.tools import contract_query as tool_module
from app.tools.contract_context import bind_actor_ticket, reset_actor_ticket
from app.tools.contract_query import ContractQueryTool, format_contract_answer, validate_contract_no

TOKEN = "gateway-test-token"
TICKET = "ticket-value-not-a-tenant-id-0123456789"
NUMBER = "HT-2026-001"


def test_contract_number_rejects_blank_unsafe_and_overlong_values():
    assert validate_contract_no(NUMBER) == NUMBER
    assert validate_contract_no(" CT-001 ") == "CT-001"
    for value in ("", "   ", "HT 2026", "HT/2026", "a&b", "id?x=1", "a\nb", "x" * 65, None, 1):
        result = validate_contract_no(value)
        assert result.status == "answered"
        assert result.error_code == "invalid_contract_no"
        assert "HT-2026-001" in result.answer


def test_client_is_read_only_and_does_not_touch_the_business_schema():
    source = inspect.getsource(tool_module) + inspect.getsource(JavaBackendClient)
    assert "psycopg" not in source
    assert "business." not in source
    assert ".post(" not in source
    assert ".put(" not in source
    assert ".delete(" not in source
    assert ".patch(" not in source
    assert not hasattr(JavaBackendClient, "create_contract")
    assert not hasattr(ContractQueryTool, "update")
    assert not hasattr(ContractQueryTool, "delete")


def test_client_sends_ticket_and_number_without_tenant_or_actor():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["token"] = request.headers.get("X-Agent-Token")
        seen["ticket"] = request.headers.get("X-Actor-Ticket")
        return httpx.Response(200, json=_payload())

    client = JavaBackendClient("http://java.internal", TOKEN, transport=httpx.MockTransport(handler))
    record = client.get_contract_by_number(NUMBER, TICKET)

    assert seen == {
        "method": "GET",
        "path": "/internal/v1/contracts",
        "params": {"contractNo": NUMBER},
        "token": TOKEN,
        "ticket": TICKET,
    }
    assert "tenant" not in seen["params"]
    assert record.status == "pending_review"
    assert record.status_label == "待审核"
    assert record.current_version is not None
    assert record.current_version.version_label == "V1"
    client.close()


def test_client_maps_missing_forbidden_and_unavailable_without_leaking_bodies():
    def sequence(codes):
        values = iter(codes)

        def handler(request: httpx.Request) -> httpx.Response:
            code = next(values)
            if code == 0:
                raise httpx.ConnectError("postgres://agent:password@db/app sk-live-secret")
            return httpx.Response(code, json={"message": "sk-live-secret", "objectKey": "secret-object-key"})

        return handler

    client = JavaBackendClient("http://java.internal", TOKEN, transport=httpx.MockTransport(sequence([404, 403, 401, 0])))
    missing = _error(client)
    forbidden = _error(client)
    unauthorized = _error(client)
    down = _error(client)

    assert (missing.code, missing.safe_message) == ("not_found", "合同不存在")
    assert (forbidden.code, forbidden.safe_message) == ("forbidden", "无权查询合同")
    assert unauthorized.code == "unauthorized"
    assert down.code == "unavailable"
    assert "sk-live-secret" not in down.safe_message
    assert "password" not in down.safe_message
    assert "secret-object-key" not in missing.safe_message
    client.close()


def test_tool_formats_only_returned_fields_and_does_not_log_credentials(caplog):
    client = JavaBackendClient(
        "http://java.internal",
        TOKEN,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_payload())),
    )
    tool = ContractQueryTool(client)
    token = bind_actor_ticket(TICKET)
    try:
        result = tool.query(f" {NUMBER} ")
    finally:
        reset_actor_ticket(token)

    assert result.status == "answered"
    assert "待审核（pending_review）" in result.answer
    assert "当前版本：V1" in result.answer
    assert "年度采购合同" in result.answer
    assert "已审核" not in result.answer
    assert "已驳回" not in result.answer
    assert "已通过" not in result.answer
    assert TOKEN not in caplog.text
    assert TICKET not in caplog.text
    assert "objectKey" not in result.answer
    client.close()


def test_tool_reports_not_found_forbidden_and_downtime_safely():
    responses = iter([
        httpx.Response(404, json={"message": "合同不存在"}),
        httpx.Response(403, json={"message": "无权查询合同"}),
        httpx.Response(500, text="postgres://agent:password sk-live"),
    ])
    client = JavaBackendClient("http://java.internal", TOKEN, transport=httpx.MockTransport(lambda request: next(responses)))
    tool = ContractQueryTool(client)
    token = bind_actor_ticket(TICKET)
    try:
        missing = tool.query(NUMBER)
        forbidden = tool.query(NUMBER)
        down = tool.query(NUMBER)
    finally:
        reset_actor_ticket(token)

    assert missing.status == "answered"
    assert missing.answer == "未找到该合同。"
    assert "年度采购" not in missing.answer
    assert forbidden.answer == "无权查询该合同。"
    assert down.status == "failed"
    assert down.error_code == "contract_unavailable"
    assert down.answer == ""
    assert "sk-live" not in str(down)
    unauthenticated = tool.query(NUMBER)
    assert unauthenticated.status == "failed"
    assert unauthenticated.error_code == "contract_unauthorized"
    client.close()


def _error(client: JavaBackendClient) -> JavaBackendError:
    with pytest.raises(JavaBackendError) as caught:
        client.get_contract_by_number(NUMBER, TICKET)
    return caught.value


def _payload() -> dict[str, object]:
    return {
        "data": {
            "contractNo": NUMBER,
            "title": "年度采购合同",
            "contractType": "采购",
            "counterpartyName": "南方公司",
            "amount": "1.00",
            "currency": "CNY",
            "signDate": None,
            "effectiveDate": None,
            "expiryDate": None,
            "status": "pending_review",
            "statusLabel": "待审核",
            "description": None,
            "currentVersion": {
                "versionNo": 1,
                "versionLabel": "V1",
                "changeSummary": "首版",
                "filename": "采购合同.pdf",
                "createdAt": "2026-11-19T02:00:00Z",
            },
            "objectKey": "secret-object-key-should-not-leak",
        }
    }


def test_invalid_ticket_response_does_not_echo_the_ticket_or_interrupt_other_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=TICKET + " sk-live-secret")

    client = JavaBackendClient("http://java.internal", TOKEN, transport=httpx.MockTransport(handler))
    tool = ContractQueryTool(client)
    missing = tool.query(NUMBER)
    token = bind_actor_ticket(TICKET)
    try:
        rejected = tool.query(NUMBER)
    finally:
        reset_actor_ticket(token)

    assert missing.error_code == "contract_unauthorized"
    assert rejected.status == "failed"
    assert rejected.error_code == "contract_unauthorized"
    assert rejected.answer == ""
    assert TICKET not in rejected.answer
    assert "sk-live-secret" not in rejected.answer
    client.close()


def test_format_uses_the_record_and_marks_a_missing_version():
    record = ContractRecord(
        NUMBER, "年度采购合同", "采购", "南方公司", "1.00", "CNY",
        None, None, None, "draft", "草稿", None, None,
    )
    text = format_contract_answer(record)
    assert "审批状态：草稿（draft）" in text
    assert "当前版本：无" in text
    version = ContractVersionRecord(2, "V2", "修订", "修订.pdf", None)
    revised = ContractRecord(
        NUMBER, "年度采购合同", "采购", "南方公司", "1.00", "CNY",
        "2026-01-01", None, None, "approved", "已审核", "说明文字", version,
    )
    rendered = format_contract_answer(revised)
    assert "已审核（approved）" in rendered
    assert "当前版本：V2" in rendered
    assert "说明：说明文字" in rendered
