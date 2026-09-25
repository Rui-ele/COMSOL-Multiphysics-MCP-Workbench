"""Complete per-call facts and operation-journal regression checks."""

import json

import pytest

from src.core.reports import report_page
from src.core.reports import tool_report
from src.core.tool_runtime import AuditRecorder, AuditedFastMCP, _result_payload, journal_value


def test_journal_retains_full_values_and_redacts_credentials(tmp_path):
    recorder = AuditRecorder(tmp_path)
    values = list(range(1000))
    message = "source-error:" + "x" * 5000
    recorder.record(
        "results_evaluate", {"model_name": "model-a", "password": "private"},
        {"success": False, "real": values, "error": message}, duration_ms=12.3456,
    )
    record = json.loads(recorder.journal_path.read_text().strip())
    assert record["result"]["real"] == values
    assert record["result"]["error"] == message
    assert record["arguments"]["password"] == "<已隐藏>"
    assert record["duration_ms"] == 12.346
    assert record["success"] is False
    assert journal_value({"model_tag": "model-a"}) == {"model_tag": "model-a"}


@pytest.mark.asyncio
async def test_wrapper_records_success_and_exact_exception(tmp_path):
    recorder = AuditRecorder(tmp_path)
    mcp = AuditedFastMCP("operation-facts", recorder=recorder)

    @mcp.tool()
    def example_read(value: str) -> dict:
        return {"success": True, "value": value}

    @mcp.tool()
    def example_failure() -> dict:
        raise RuntimeError("COMSOL original failure")

    success = _result_payload(await mcp.call_tool("example_read", {"value": "42"}))
    failed = _result_payload(await mcp.call_tool("example_failure", {}))
    assert success["success"] is True
    assert success["value"] == "42"
    assert "report_markdown" in success
    assert failed["success"] is False
    assert failed["operation_invoked"] is True
    assert "COMSOL original failure" in failed["error"]
    records = [json.loads(line) for line in recorder.journal_path.read_text().splitlines()]
    assert [item["success"] for item in records] == [True, False]
    assert "COMSOL original failure" in records[1]["result"]["error"]


def test_paged_report_preserves_complete_facts_and_model_scope(monkeypatch):
    monkeypatch.setattr("src.core.reports.runtime_info", lambda: {"source_revision": "test"})
    values = list(range(10000))
    response = tool_report(
        "results_evaluate", {"model_name": "model-a", "evaluation_tag": "ev1"},
        {"success": True, "model_tag": "model-a", "real": values, "error_text": "e" * 20000},
    )
    pages = [response["report_markdown"]]
    delivery = response["report_delivery"]
    report_id = delivery["report_id"]
    while delivery["next_offset"] is not None:
        page = report_page(report_id, delivery["next_offset"])
        assert page["success"] is True
        assert page["report_delivery"]["report_id"] == report_id
        pages.append(page["report_markdown"])
        delivery = page["report_delivery"]
    content = "".join(pages)
    payload = json.loads(content.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert payload["real"] == values
    assert payload["error_text"] == "e" * 20000
    assert payload["model_tag"] == "model-a"
    assert payload["request"]["model_name"] == "model-a"


@pytest.mark.parametrize("kind", ["diagnostic", "api", "tool"])
def test_report_envelope_and_markdown_preserve_operation_facts(monkeypatch, kind):
    from src.core import reports

    monkeypatch.setattr(reports, "runtime_info", lambda: {"source_revision": "test"})
    request = {"model_name": "model-a", "method": "get", "args": ["L"]}
    if kind == "diagnostic":
        packet = reports.new_report_packet("diagnostic_parameters_read", request, success=True, status="collected")
        packet.update(model={"model_tag": "model-a"}, findings={"parameter": reports.fact("param.get(L)", "5[mm]")})
        response = reports.finish_diagnostic_packet(packet)
    elif kind == "api":
        packet = reports.new_report_packet("comsol_api_read", request, success=True, status="read")
        packet.update(model_tag="model-a", value="5[mm]")
        response = reports.finish_api_packet(packet)
    else:
        response = reports.tool_report("example_read", request, {"success": True, "model_tag": "model-a", "value": "5[mm]"})
    assert response["request"] == request
    assert type(response["report_version"]) is int
    assert response["report_version"] == reports.REPORT_VERSION
    assert response["model_tag"] == "model-a"
    assert response["model_reference"] == "model-a"
    assert isinstance(response["summary"], str)
    assert response["errors"] == []
    payload = json.loads(response["report_markdown"].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert payload["request"] == request
    assert payload["success"] is True
    assert "5[mm]" in response["report_markdown"]


def test_report_preserves_stale_model_error_with_label_metadata():
    response = tool_report("comsol_api_read", {"model_name": "model-a"}, {
        "success": False, "model": "Model label", "model_tag": "model-a",
        "error": "Model was removed", "error_code": "model_stale",
    })
    assert response["success"] is False
    assert response["model"] == "Model label"
    assert response["errors"] == ["Model was removed"]


def test_report_delivery_failure_keeps_completed_write_and_exact_readback(monkeypatch):
    from src.core import reports

    def unavailable(*args):
        raise RuntimeError("report cache unavailable")

    monkeypatch.setattr(reports, "paginate_report", unavailable)
    packet = reports.new_report_packet("comsol_api_write", {"model_name": "model-a"}, success=True, status="verified")
    packet.update(write_attempted=True, write_returned=True, after=[{"actual": "5[mm]", "passed": True}])
    result = reports.finish_api_packet(packet)
    assert result["success"] is True
    assert result["write_returned"] is True
    assert result["after"] == packet["after"]
    assert "report cache unavailable" in result["report_warning"]
    assert '"actual": "5[mm]"' in result["report_markdown"]
