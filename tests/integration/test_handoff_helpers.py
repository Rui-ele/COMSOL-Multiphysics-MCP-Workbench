"""Unit tests for the real external-handoff acceptance harness."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.handoff_runner import (
    AcceptanceFailure,
    assert_result,
    executable_path,
    find_acceptance_failure,
    exception_summary,
    safe_report_value,
    redacted_log_tail,
    tool_payload,
    write_acceptance_report,
)


def test_executable_path_preserves_virtualenv_symlink(tmp_path):
    target = tmp_path / "system-python"
    target.touch()
    link = tmp_path / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    assert executable_path(str(link)) == link.absolute()
    assert executable_path(str(link)) != target.resolve()


def test_tool_payload_reads_mcp_text_content():
    result = SimpleNamespace(
        structuredContent=None,
        content=[SimpleNamespace(text='{"success": true, "value": 42}')],
    )

    assert tool_payload(result) == {"success": True, "value": 42}


def test_assert_result_checks_stable_error_code():
    payload = {
        "success": False,
        "error_code": "model_stale",
    }

    assert_result(
        "stale_model",
        payload,
        success=False,
        error_code="model_stale",
    )
    with pytest.raises(AcceptanceFailure) as captured:
        assert_result(
            "stale_model",
            payload,
            success=False,
            error_code="unexpected",
        )

    assert captured.value.stage == "stale_model"


def test_find_acceptance_failure_unwraps_task_group_shape():
    failure = AcceptanceFailure("discover", "metadata mismatch")
    grouped = RuntimeError("task group")
    grouped.exceptions = (RuntimeError("noise"), failure)

    assert find_acceptance_failure(grouped) is failure


def test_exception_summary_flattens_nested_task_group_shape():
    inner = ValueError("server busy")
    outer = RuntimeError("task group")
    outer.exceptions = (inner,)

    assert exception_summary(outer) == [
        "RuntimeError: task group",
        "ValueError: server busy",
    ]


def test_acceptance_report_redacts_credentials_and_records_boundaries(tmp_path):
    report = {
        "success": True,
        "comsol_version": "6.4",
        "server_port": 30491,
        "model": {
            "name": "Temporary model",
            "tag": "model",
            "file": None,
        },
        "values": {
            "initial": "1",
            "after_stale_model": "1",
            "after_write": "42",
            "after_detach": "42",
            "after_mcp_disconnect": "42",
        },
        "saved_file": {"path": "/temporary/copy.mph", "size_bytes": 42},
        "model_removed": True,
        "cleanup": {
            "server_running_after_mcp_disconnect": True,
            "client_a_stopped": True,
            "server_stopped": True,
        },
        "duration_seconds": 12.5,
        "stages": [
            {
                "name": "真实链路",
                "success": True,
                "detail": "通过",
                "evidence": {"password": "do-not-persist"},
            }
        ],
        "api_token": "do-not-persist",
        "error": None,
    }

    json_path, markdown_path = write_acceptance_report(report, tmp_path)
    persisted = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert persisted["api_token"] == "<redacted>"
    assert (
        persisted["stages"][0]["evidence"]["password"]
        == "<redacted>"
    )
    assert "do-not-persist" not in json_path.read_text(encoding="utf-8")
    assert "未保存，仅存在于服务器内存" in markdown
    assert "Desktop 人工观察清单未在本自动化验收中执行" in markdown


def test_safe_report_value_preserves_non_secret_evidence():
    clean = safe_report_value(
        {
            "model_tag": "model",
            "secret_value": "hidden",
            "items": [{"value": 42}],
        }
    )

    assert clean == {
        "model_tag": "model",
        "secret_value": "<redacted>",
        "items": [{"value": 42}],
    }


def test_redacted_log_tail_drops_credential_lines_and_limits_output():
    lines = [f"line {index}" for index in range(30)]
    lines.append("password=must-not-persist")

    tail = redacted_log_tail(lines, limit=3)

    assert tail == [
        "line 28",
        "line 29",
        "<redacted credential-shaped log line>",
    ]
