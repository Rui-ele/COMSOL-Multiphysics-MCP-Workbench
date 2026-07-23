"""Tests for COMSOL MCP command auditing and handoff reports."""

from datetime import datetime
from pathlib import Path

import pytest

import src.tools.report as report_module
from src.reporting import (
    AuditRecorder,
    AuditedFastMCP,
    should_audit_tool,
    summarize_value,
)
from src.tools.report import PlotRecommendation, create_simulation_report
from src.tools.session import ModelRecord


class FakeModel:
    class Java:
        def tag(self):
            return "model-report"

    java = Java()

    def name(self):
        return "2D_Coils_ACDC_N300_freq"

    def file(self):
        return None

    def version(self):
        return "6.4"

    def studies(self):
        return ["Study 1", "Study 4"]

    def solutions(self):
        return ["Study 1/Solution 1", "Study 4/Solution 4"]

    def datasets(self):
        return ["Study 1/Solution 1", "Study 4/Solution 4"]

    def plots(self):
        return ["Magnetic Flux Density", "B Field"]

    def problems(self):
        return []


class FakeSessionManager:
    def __init__(self):
        self.model = FakeModel()

    def get_model(self, name=None):
        if name not in (None, self.model.name()):
            return None
        return self.model

    def get_status(self):
        return {
            "connected": True,
            "version": "6.4",
            "session_mode": "shared-managed",
            "server_host": "localhost",
            "server_port": 2036,
            "desktop_attached": True,
            "current_model": self.model.name(),
            "current_model_tag": "model-report",
            "models_used_by_other_clients": ["model-report"],
        }

    def get_model_record(self, name=None):
        if name not in (None, self.model.name(), "model-report"):
            return None
        return ModelRecord(
            tag="model-report",
            name=self.model.name(),
            model=self.model,
            origin="external_attached",
            access_mode="observe",
            server_managed=False,
            attached_at=datetime.now().astimezone(),
            last_seen_at=datetime.now().astimezone(),
        )


def test_large_numeric_arrays_are_summarized():
    summary = summarize_value(list(range(100)))

    assert summary == {
        "summary": "numeric_array",
        "count": 100,
        "shape": [100],
        "min": 0.0,
        "max": 99.0,
        "mean": 49.5,
    }


def test_audit_recorder_filters_knowledge_and_advances_cursor(tmp_path):
    recorder = AuditRecorder(tmp_path)

    assert should_audit_tool("pdf_search") is False
    assert should_audit_tool("simulation_report_create") is False
    assert should_audit_tool("physics_get_available") is True

    assert recorder.record(
        "pdf_search", {"query": "coil"}, {"success": True}, duration_ms=1
    ) is None
    assert recorder.record(
        "simulation_report_create",
        {"title": "report"},
        {"success": True},
        duration_ms=1,
    ) is None
    recorder.record(
        "param_set",
        {"name": "freq", "value": "1[kHz]"},
        {"success": True},
        duration_ms=12.3456,
    )
    recorder.record(
        "study_solve",
        {"study_name": "Study 4"},
        {"success": False, "error": "solver failed"},
        duration_ms=20,
    )

    records = recorder.pending_records()
    assert [record["tool"] for record in records] == ["param_set", "study_solve"]
    assert records[0]["duration_ms"] == 12.346
    assert records[1]["success"] is False
    assert records[1]["error"] == "solver failed"
    assert recorder.journal_path.exists()

    assert recorder.advance_cursor() == 2
    assert recorder.pending_records() == []


@pytest.mark.asyncio
async def test_audited_fastmcp_records_success_and_exception(tmp_path):
    recorder = AuditRecorder(tmp_path)
    mcp = AuditedFastMCP("audit-test", recorder=recorder)

    @mcp.tool()
    def param_set(name: str, value: str) -> dict:
        return {"success": True, "name": name, "value": value}

    @mcp.tool()
    def study_solve() -> dict:
        raise RuntimeError("boom")

    await mcp.call_tool("param_set", {"name": "N", "value": "20"})
    with pytest.raises(Exception, match="boom"):
        await mcp.call_tool("study_solve", {})

    records = recorder.pending_records()
    assert records[0]["tool"] == "param_set"
    assert records[0]["success"] is True
    assert records[1]["tool"] == "study_solve"
    assert records[1]["success"] is False
    assert "boom" in records[1]["error"]


def _record_demo_commands(recorder):
    recorder.record(
        "comsol_start",
        {"cores": 1, "version": "6.4"},
        {"success": True, "server_port": 2036},
        duration_ms=100,
    )
    recorder.record(
        "model_load",
        {"file_path": "/models/2D_Coils_ACDC_N300_freq.mph"},
        {"success": True, "model": "2D_Coils_ACDC_N300_freq"},
        duration_ms=200,
    )
    recorder.record(
        "param_set",
        {"name": "freq", "value": "1[kHz]"},
        {"success": True},
        duration_ms=10,
    )
    recorder.record(
        "study_solve",
        {"study_name": "Study 4"},
        {"success": True, "study": "Study 4"},
        duration_ms=1500,
    )
    recorder.record(
        "results_evaluate",
        {
            "expression": "mf.normB",
            "unit": "T",
            "dataset": "Study 4/Solution 4",
        },
        {
            "success": True,
            "expression": "mf.normB",
            "unit": "T",
            "dataset": "Study 4/Solution 4",
            "value": [index / 1000 for index in range(100)],
        },
        duration_ms=50,
    )


def test_report_contains_handoff_details_and_advances_cursor(tmp_path, monkeypatch):
    recorder = AuditRecorder(tmp_path / "data")
    _record_demo_commands(recorder)
    monkeypatch.setattr(report_module, "session_manager", FakeSessionManager())

    result = create_simulation_report(
        title="2D 线圈 Study 4 仿真报告",
        summary="完成 1 kHz 频域磁场求解。",
        recommended_plots=[
            PlotRecommendation(
                plot_name="B Field",
                dataset_name="Study 4/Solution 4",
                reason="查看磁通密度空间分布",
                view_state="freq = 1000 Hz",
            )
        ],
        conclusions=["磁通密度结果已完成数值检查。"],
        recorder=recorder,
        report_dir=tmp_path / "simulation_reports",
    )

    assert result["success"] is True
    assert result["command_count"] == 5
    assert result["server_port"] == 2036
    assert result["model_tag"] == "model-report"
    assert result["origin"] == "external_attached"
    assert result["access_mode"] == "observe"
    assert result["used_by_other_clients"] is True
    assert result["stale"] is False
    assert result["saved_to_disk"] is False
    assert result["plot_checks"][0]["plot_exists"] is True
    assert result["plot_checks"][0]["dataset_exists"] is True
    assert recorder.pending_records() == []

    report_path = Path(result["report_path"])
    content = report_path.read_text(encoding="utf-8")
    assert "Study 4/Solution 4" in content
    assert "Results → B Field" in content
    assert "mf.normB" in content
    assert '"summary":"numeric_array"' in content
    assert "未调用模型保存工具" in content
    assert "模型 tag：`model-report`" in content
    assert "模型来源：`external_attached`" in content
    assert "MCP 访问模式：`observe`" in content
    assert "其他客户端正在使用：`是`" in content
    assert "服务器内存模型（未保存）" in content
    assert "共享服务器：`localhost:2036`" in content
    assert "`study_solve`" in content
    assert "1500.0 ms" in content

    recorder.record(
        "comsol_status", {}, {"connected": True}, duration_ms=1
    )
    second = create_simulation_report(
        title="第二次报告",
        summary="只记录新命令。",
        recommended_plots=[],
        recorder=recorder,
        report_dir=tmp_path / "simulation_reports",
    )
    assert second["command_count"] == 1
    assert second["report_path"] != result["report_path"]
    assert "未提供推荐结果图。" in second["warnings"]


def test_report_warns_for_missing_plot_and_dataset(tmp_path, monkeypatch):
    recorder = AuditRecorder(tmp_path / "data")
    recorder.record("comsol_status", {}, {"connected": True}, duration_ms=1)
    monkeypatch.setattr(report_module, "session_manager", FakeSessionManager())

    result = create_simulation_report(
        title="检查缺失结果图",
        summary="验证报告警告。",
        recommended_plots=[
            PlotRecommendation(
                plot_name="Missing Plot",
                dataset_name="Missing Dataset",
                reason="测试",
            )
        ],
        recorder=recorder,
        report_dir=tmp_path / "reports",
    )

    assert result["success"] is True
    assert result["plot_checks"][0]["plot_exists"] is False
    assert result["plot_checks"][0]["dataset_exists"] is False
    assert any("Missing Plot" in warning for warning in result["warnings"])
    assert any("Missing Dataset" in warning for warning in result["warnings"])
