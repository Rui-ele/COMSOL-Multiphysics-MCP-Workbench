"""External-model lifecycle and stale-handle protection tests."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.tools.model as model_module
import src.core.session as session_module
from src.core.tool_runtime import AuditRecorder, AuditedFastMCP, _result_payload
from src.tools.model import register_model_tools
from src.core.session import SessionManager


class LifecycleJavaModel:
    def __init__(
        self,
        tag: str,
        label: str,
        file_path: Path | None = None,
    ):
        self._tag = tag
        self._label = label
        self._file_path = file_path
        self.get_file_calls = 0
        self.label_calls = 0

    def tag(self):
        return self._tag

    def label(self, value=None):
        self.label_calls += 1
        if value is not None:
            self._label = value
        return self._label

    def getFilePath(self):
        self.get_file_calls += 1
        return str(self._file_path) if self._file_path else ""


class LifecycleModel:
    def __init__(self, java: LifecycleJavaModel):
        self.java = java

    def name(self):
        return self.java.label()

    def file(self):
        return self.java._file_path

    def version(self):
        return "6.4"


class LifecycleJavaClient:
    def __init__(self):
        self.server_models: dict[str, LifecycleJavaModel] = {}
        self.observed_tags: list[str] = []
        self.fail_enumeration = False

    def tags(self):
        if self.fail_enumeration:
            raise RuntimeError("server unavailable")
        return list(self.server_models)

    def model(self, tag):
        return self.server_models[tag]

    def modelsUsedByOtherClients(self):
        return self.observed_tags


class LifecycleClient:
    def __init__(self):
        self.version = "6.4"
        self.cores = 1
        self.standalone = False
        self.port = 2036
        self.host = "localhost"
        self.java = LifecycleJavaClient()
        self.disconnect_calls = 0
        self.connect_calls = []
        self.remove_calls = 0

    def names(self):
        if self.java.fail_enumeration:
            raise RuntimeError("server unavailable")
        return [model.label() for model in self.java.server_models.values()]

    def disconnect(self):
        self.disconnect_calls += 1
        self.port = None
        self.host = None

    def connect(self, port, host="localhost"):
        self.port = port
        self.host = host
        self.standalone = False
        self.connect_calls.append((host, port))

    def remove(self, _model):
        self.remove_calls += 1


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


@pytest.fixture
def lifecycle_manager(monkeypatch):
    manager = SessionManager()
    manager._client = LifecycleClient()
    manager._server = None
    manager._server_managed = False
    manager._session_mode = "shared-external"
    manager._host = "localhost"
    manager._port = 2036
    manager._models.clear()
    manager._current_model_tag = None
    manager._busy_handler = None
    monkeypatch.setattr(
        session_module.mph,
        "Model",
        lambda java: LifecycleModel(java),
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


def add_server_model(
    manager: SessionManager,
    *,
    tag: str = "desktop-tag",
    name: str = "Desktop Model",
    file_path: Path | None = None,
):
    java = LifecycleJavaModel(tag, name, file_path)
    manager.client.java.server_models[tag] = java
    return java


def attach_external(manager: SessionManager):
    java = add_server_model(manager)
    record, attached = manager.attach_server_model("desktop-tag")
    assert attached is True
    return java, record


def add_counted_tool(mcp, counters, tool_name):
    def body(model_name: str | None = None) -> dict:
        counters[tool_name] += 1
        return {"success": True, "model_name": model_name}

    mcp.add_tool(body, name=tool_name)


def test_status_refreshes_rename_and_marks_missing_model_stale(
    lifecycle_manager,
):
    java, record = attach_external(lifecycle_manager)
    lifecycle_manager.client.java.observed_tags = ["desktop-tag"]
    java.label("Renamed in Desktop")

    live = lifecycle_manager.get_status()

    assert live["current_model"] == "Renamed in Desktop"
    assert live["current_model_tag"] == "desktop-tag"
    assert live["desktop_attached"] is True
    assert live["models"][0]["stale"] is False
    assert live["models"][0]["last_seen_at"] is not None

    lifecycle_manager.client.java.observed_tags = []
    file_calls_before_delete = java.get_file_calls
    del lifecycle_manager.client.java.server_models["desktop-tag"]
    stale = lifecycle_manager.get_status()
    discovered = lifecycle_manager.discover_server_models()

    assert stale["models"][0]["stale"] is True
    assert stale["models"][0]["stale_reason"] == "model_missing_from_server"
    assert stale["desktop_attached"] is False
    assert record.stale is True
    assert discovered["server_model_count"] == 0
    assert discovered["stale_count"] == 1
    assert discovered["models"][0]["tag"] == "desktop-tag"
    assert discovered["models"][0]["stale"] is True
    assert java.get_file_calls == file_calls_before_delete


def test_model_list_reports_live_and_stale_lifecycle_fields(
    lifecycle_manager,
    monkeypatch,
):
    java, _record = attach_external(lifecycle_manager)
    lifecycle_manager.client.java.observed_tags = ["desktop-tag"]
    java.label("Renamed for List")
    registry = ToolRegistry()
    monkeypatch.setattr(model_module, "session_manager", lifecycle_manager)
    register_model_tools(registry)

    live = registry.tools["model_list"]()
    del lifecycle_manager.client.java.server_models["desktop-tag"]
    lifecycle_manager.client.java.observed_tags = []
    stale = registry.tools["model_list"]()

    assert live["models"][0]["name"] == "Renamed for List"
    assert live["models"][0]["stale"] is False
    assert stale["models"][0]["stale"] is True
    assert stale["models"][0]["stale_reason"] == "model_missing_from_server"


def test_status_uses_cached_model_identity_while_solver_is_running(lifecycle_manager, monkeypatch):
    java, record = attach_external(lifecycle_manager)
    reads_before = (java.label_calls, java.get_file_calls)
    monkeypatch.setattr(
        "src.core.solver.async_solver",
        SimpleNamespace(is_running=True, get_progress=lambda: {"run_id": "active-run", "status": "running"}),
    )
    monkeypatch.setattr(lifecycle_manager.client.java, "tags", lambda: pytest.fail("busy status queried COMSOL"))

    status = lifecycle_manager.get_status()

    assert status["connected"] is True
    assert status["models"][0]["tag"] == record.tag
    assert "cache" in status["model_metadata_source"]
    assert status["operation"]["run_id"] == "active-run"
    assert (java.label_calls, java.get_file_calls) == reads_before


@pytest.mark.asyncio
async def test_stale_model_blocks_reads_writes_solve_and_report_before_body(
    tmp_path,
    lifecycle_manager,
):
    attach_external(lifecycle_manager)
    del lifecycle_manager.client.java.server_models["desktop-tag"]
    recorder = AuditRecorder(tmp_path)
    mcp = AuditedFastMCP("stale-guard", recorder=recorder)
    counters = defaultdict(int)
    guarded = [
        "comsol_api_read",
        "comsol_api_write",
        "study_solve",
        "results_evaluate",
    ]
    for name in guarded + ["model_detach"]:
        add_counted_tool(mcp, counters, name)

    for name in guarded:
        result = _result_payload(
            await mcp.call_tool(name, {"model_name": "desktop-tag"})
        )
        assert result["error_code"] == "model_stale"
        assert result["model_tag"] == "desktop-tag"
        assert result["stale"] is True

    detached = _result_payload(
        await mcp.call_tool("model_detach", {"model_name": "desktop-tag"})
    )
    assert detached["success"] is True
    assert {name: counters[name] for name in guarded} == {
        name: 0 for name in guarded
    }
    assert counters["model_detach"] == 1
    records = [json.loads(line) for line in recorder.journal_path.read_text().splitlines()]
    assert [record["success"] for record in records] == [
        False,
        False,
        False,
        False,
        True,
    ]
    assert all(
        record["result"]["error_code"] == "model_stale"
        for record in records[:-1]
    )


@pytest.mark.asyncio
async def test_stale_tag_is_sticky_until_explicit_attach(
    tmp_path,
    lifecycle_manager,
):
    _java, record = attach_external(lifecycle_manager)
    old_model = record.model
    del lifecycle_manager.client.java.server_models["desktop-tag"]
    lifecycle_manager.synchronize_model_registry()
    replacement_java = add_server_model(
        lifecycle_manager,
        name="Replacement Model",
    )

    mcp = AuditedFastMCP(
        "stale-reattach",
        recorder=AuditRecorder(tmp_path),
    )
    counters = defaultdict(int)
    add_counted_tool(mcp, counters, "comsol_api_read")
    still_stale = _result_payload(
        await mcp.call_tool(
            "comsol_api_read",
            {"model_name": "desktop-tag"},
        )
    )

    assert still_stale["error_code"] == "model_stale"
    assert counters["comsol_api_read"] == 0
    reattached, attached = lifecycle_manager.attach_server_model(
        "desktop-tag"
    )
    assert attached is True
    assert reattached.model is not old_model
    assert reattached.model.java is replacement_java
    assert reattached.name == "Replacement Model"
    assert reattached.origin == "external_attached"
    assert reattached.server_managed is False
    assert reattached.stale is False


@pytest.mark.asyncio
async def test_enumeration_failure_does_not_mark_models_stale(
    tmp_path,
    lifecycle_manager,
):
    _java, record = attach_external(lifecycle_manager)
    label_calls_before_failure = _java.label_calls
    lifecycle_manager.client.java.fail_enumeration = True
    recorder = AuditRecorder(tmp_path)
    mcp = AuditedFastMCP("state-unavailable", recorder=recorder)
    counters = defaultdict(int)
    add_counted_tool(mcp, counters, "comsol_api_read")

    result = _result_payload(
        await mcp.call_tool(
            "comsol_api_read",
            {"model_name": "desktop-tag"},
        )
    )
    status = lifecycle_manager.get_status()

    assert result["error_code"] == "model_state_unavailable"
    assert "server unavailable" in result["detail"]
    assert counters["comsol_api_read"] == 0
    assert record.stale is False
    assert _java.label_calls == label_calls_before_failure
    assert status["registry_state_available"] is False
    assert "server unavailable" in status["registry_sync_error"]
    records = [json.loads(line) for line in recorder.journal_path.read_text().splitlines()]
    assert records[0]["success"] is False


def test_external_disconnect_clears_registry_and_requires_reattach(
    lifecycle_manager,
):
    attach_external(lifecycle_manager)
    client = lifecycle_manager.client
    client.java.observed_tags = ["desktop-tag"]

    disconnected = lifecycle_manager.disconnect()

    assert disconnected["success"] is True
    assert "left running" in disconnected["message"]
    assert client.disconnect_calls == 1
    assert lifecycle_manager.models == {}
    assert "desktop-tag" in client.java.server_models
    assert client.java.observed_tags == ["desktop-tag"]

    reconnected = lifecycle_manager.connect(3040, "localhost")
    discovered = lifecycle_manager.discover_server_models()

    assert reconnected["success"] is True
    assert client.connect_calls == [("localhost", 3040)]
    assert lifecycle_manager.get_model("desktop-tag") is None
    assert discovered["models"][0]["registered"] is False
    record, attached = lifecycle_manager.attach_server_model("desktop-tag")
    assert attached is True
    assert record.origin == "external_attached"
