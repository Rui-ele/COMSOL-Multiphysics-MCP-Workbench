"""Access-mode enforcement tests for COMSOL-facing MCP tools."""

from __future__ import annotations

import inspect
from collections import defaultdict

import pytest

from src.access_control import (
    CONDITIONAL_MODEL_TOOLS,
    READ_ONLY_MODEL_TOOLS,
    tool_access_policy,
)
from src.reporting import AuditRecorder, AuditedFastMCP, _result_payload
from src.tools.geometry import register_geometry_tools
from src.tools.mesh import register_mesh_tools
from src.tools.model import register_model_tools
from src.tools.parameters import register_parameter_tools
from src.tools.physics import register_physics_tools
from src.tools.report import register_report_tools
from src.tools.results import register_results_tools
from src.tools.session import SessionManager
from src.tools.study import register_study_tools


class FakeJavaModel:
    def __init__(self, tag: str):
        self._tag = tag

    def tag(self):
        return self._tag


class FakeModel:
    def __init__(self, name: str, tag: str):
        self._name = name
        self.java = FakeJavaModel(tag)

    def name(self):
        return self._name


class FakeClient:
    def __init__(self):
        self.standalone = False
        self.port = 2036
        self.java = self
        self.server_tags = ["desktop-tag"]

    def tags(self):
        return self.server_tags


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kwargs):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


@pytest.fixture
def observe_manager():
    manager = SessionManager()
    manager._client = FakeClient()
    manager._server = None
    manager._server_managed = False
    manager._session_mode = "shared-external"
    manager._host = "localhost"
    manager._port = 2036
    manager._models.clear()
    manager._current_model_tag = None
    manager._busy_handler = None
    manager.add_model(
        FakeModel("Desktop Model", "desktop-tag"),
        origin="external_attached",
        access_mode="observe",
        server_managed=False,
    )
    yield manager
    manager._client = None
    manager._server = None
    manager._server_managed = False
    manager._session_mode = None
    manager._host = None
    manager._port = None
    manager._models.clear()
    manager._current_model_tag = None
    manager._busy_handler = None


def add_counted_tool(mcp, counters, tool_name):
    def body(model_name: str | None = None) -> dict:
        counters[tool_name] += 1
        return {
            "success": True,
            "tool": tool_name,
            "model_name": model_name,
        }

    mcp.add_tool(body, name=tool_name)


@pytest.mark.asyncio
async def test_observe_mode_blocks_every_write_category_before_body(
    tmp_path,
    observe_manager,
):
    recorder = AuditRecorder(tmp_path)
    mcp = AuditedFastMCP("access-write-categories", recorder=recorder)
    counters = defaultdict(int)
    write_tools = [
        "model_create_component",
        "param_set",
        "param_sweep_setup",
        "geometry_add_block",
        "geometry_get_boundaries",
        "physics_add",
        "physics_set_material",
        "physics_configure_boundary",
        "mesh_create",
        "study_create",
        "study_solve",
        "model_save",
        "model_save_version",
        "model_remove",
        "results_export_data",
        "results_export_image",
    ]
    for name in write_tools:
        add_counted_tool(mcp, counters, name)

    for name in write_tools:
        result = _result_payload(
            await mcp.call_tool(name, {"model_name": "desktop-tag"})
        )
        assert result["success"] is False
        assert result["error_code"] == "model_write_access_required"
        assert result["model"] == "Desktop Model"
        assert result["model_tag"] == "desktop-tag"
        assert result["access_mode"] == "observe"
        assert result["required_access_mode"] == "write"
        assert "model_access_set" in result["hint"]

    assert dict(counters) == {}


@pytest.mark.asyncio
async def test_observe_mode_allows_reads_reports_detach_and_clone(
    tmp_path,
    observe_manager,
):
    mcp = AuditedFastMCP(
        "access-read-tools",
        recorder=AuditRecorder(tmp_path),
    )
    counters = defaultdict(int)
    read_tools = [
        "model_detach",
        "model_inspect",
        "model_list_components",
        "model_set_current",
        "model_clone",
        "param_get",
        "param_list",
        "geometry_list",
        "geometry_list_features",
        "mesh_list",
        "mesh_info",
        "physics_list",
        "physics_list_features",
        "study_list",
        "solutions_list",
        "datasets_list",
        "results_evaluate",
        "results_global_evaluate",
        "results_inner_values",
        "results_outer_values",
        "results_exports_list",
        "results_plots_list",
        "simulation_report_create",
    ]
    for name in read_tools:
        add_counted_tool(mcp, counters, name)

    for name in read_tools:
        result = _result_payload(
            await mcp.call_tool(name, {"model_name": "desktop-tag"})
        )
        assert result["success"] is True

    assert counters == {name: 1 for name in read_tools}


@pytest.mark.asyncio
async def test_param_description_is_guarded_only_when_text_is_supplied(
    tmp_path,
    observe_manager,
):
    mcp = AuditedFastMCP(
        "access-conditional",
        recorder=AuditRecorder(tmp_path),
    )
    calls = []

    def param_description(
        name: str,
        text: str | None = None,
        model_name: str | None = None,
    ) -> dict:
        calls.append((name, text, model_name))
        return {"success": True, "description": text or "existing"}

    mcp.add_tool(param_description)

    read = _result_payload(
        await mcp.call_tool(
            "param_description",
            {"name": "freq", "model_name": "desktop-tag"},
        )
    )
    explicit_none = _result_payload(
        await mcp.call_tool(
            "param_description",
            {"name": "freq", "text": None, "model_name": "desktop-tag"},
        )
    )
    write = _result_payload(
        await mcp.call_tool(
            "param_description",
            {
                "name": "freq",
                "text": "frequency",
                "model_name": "desktop-tag",
            },
        )
    )

    assert read["success"] is True
    assert explicit_none["success"] is True
    assert write["error_code"] == "model_write_access_required"
    assert calls == [
        ("freq", None, "desktop-tag"),
        ("freq", None, "desktop-tag"),
    ]


@pytest.mark.asyncio
async def test_guard_resolves_current_tag_unique_name_and_preserves_not_found(
    tmp_path,
    observe_manager,
):
    mcp = AuditedFastMCP(
        "access-resolution",
        recorder=AuditRecorder(tmp_path),
    )
    calls = []

    def param_set(model_name: str | None = None) -> dict:
        calls.append(model_name)
        return {
            "success": False,
            "error": f"Model not found: {model_name or 'no current model'}",
        }

    mcp.add_tool(param_set)

    for arguments in (
        {},
        {"model_name": "desktop-tag"},
        {"model_name": "Desktop Model"},
    ):
        denied = _result_payload(await mcp.call_tool("param_set", arguments))
        assert denied["error_code"] == "model_write_access_required"

    observe_manager._models.clear()
    observe_manager._current_model_tag = None
    missing = _result_payload(
        await mcp.call_tool("param_set", {"model_name": "missing"})
    )

    assert missing == {
        "success": False,
        "error": "Model not found: missing",
    }
    assert calls == ["missing"]


@pytest.mark.asyncio
async def test_observe_write_observe_cycle_and_denial_audit(
    tmp_path,
    observe_manager,
):
    recorder = AuditRecorder(tmp_path)
    mcp = AuditedFastMCP("access-cycle", recorder=recorder)
    calls = []

    def param_set(model_name: str | None = None) -> dict:
        calls.append(model_name)
        return {"success": True}

    mcp.add_tool(param_set)

    first = _result_payload(await mcp.call_tool("param_set", {}))
    observe_manager.set_model_access("desktop-tag", "write")
    allowed = _result_payload(await mcp.call_tool("param_set", {}))
    observe_manager.set_model_access("Desktop Model", "observe")
    last = _result_payload(await mcp.call_tool("param_set", {}))

    assert first["error_code"] == "model_write_access_required"
    assert allowed == {"success": True}
    assert last["error_code"] == "model_write_access_required"
    assert calls == [None]

    records = recorder.pending_records()
    assert [record["success"] for record in records] == [False, True, False]
    assert records[0]["error"] == first["error"]
    assert records[0]["result"]["error_code"] == "model_write_access_required"
    assert records[2]["result"]["model_tag"] == "desktop-tag"


def test_policy_covers_all_current_model_tools_and_defaults_new_tools_to_write():
    registry = ToolRegistry()
    for register in (
        register_model_tools,
        register_parameter_tools,
        register_geometry_tools,
        register_physics_tools,
        register_mesh_tools,
        register_study_tools,
        register_results_tools,
        register_report_tools,
    ):
        register(registry)

    tools_with_model = {
        name: function
        for name, function in registry.tools.items()
        if "model_name" in inspect.signature(function).parameters
    }
    assert READ_ONLY_MODEL_TOOLS <= tools_with_model.keys()
    assert CONDITIONAL_MODEL_TOOLS <= tools_with_model.keys()

    for name, function in tools_with_model.items():
        policy = tool_access_policy(
            name,
            inspect.signature(function).parameters,
        )
        assert policy != "none"
        if name in READ_ONLY_MODEL_TOOLS:
            assert policy == "read"
        elif name in CONDITIONAL_MODEL_TOOLS:
            assert policy == "conditional"
        else:
            assert policy == "write"

    assert tool_access_policy("future_model_mutation", {"model_name"}) == "write"
    assert tool_access_policy("unrelated_tool", {"value"}) == "none"
    for name in {"study_get_progress", "study_cancel", "study_wait"}:
        parameters = inspect.signature(registry.tools[name]).parameters
        assert "model_name" not in parameters
        assert tool_access_policy(name, parameters) == "none"
