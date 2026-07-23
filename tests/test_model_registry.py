"""Tests for stable tag-based COMSOL model registration."""

from datetime import datetime
from pathlib import Path

import pytest

import src.tools.model as model_module
from src.tools.model import register_model_tools
from src.tools.session import AmbiguousModelError, SessionManager


class FakeModelJava:
    copy_count = 0

    def __init__(
        self,
        tag: str,
        label: str,
        file_path: Path | None = None,
    ):
        self._tag = tag
        self._label = label
        self._file_path = file_path

    def tag(self):
        return self._tag

    def label(self, value=None):
        if value is not None:
            self._label = value
        return self._label

    def createCopy(self):
        type(self).copy_count += 1
        return FakeModelJava(
            f"{self._tag}_copy{type(self).copy_count}",
            self._label,
            self._file_path,
        )

    def getFilePath(self):
        return str(self._file_path) if self._file_path else ""


class FakeModel:
    def __init__(
        self,
        name: str,
        tag: str,
        file_path: Path | None = None,
        *,
        java: FakeModelJava | None = None,
    ):
        self.java = java or FakeModelJava(tag, name, file_path)
        self._file_path = file_path if java is None else java._file_path

    @classmethod
    def from_java(cls, java: FakeModelJava):
        return cls(java.label(), java.tag(), java._file_path, java=java)

    def name(self):
        return self.java.label()

    def file(self):
        return self._file_path

    def version(self):
        return "6.4"


class FakeClientJava:
    def __init__(self):
        self.server_models = {}
        self.observed_tags = []
        self.broken_tags = set()

    def tags(self):
        return list(self.server_models)

    def model(self, tag):
        if tag in self.broken_tags:
            raise RuntimeError(f"cannot wrap {tag}")
        return self.server_models[tag]

    def add_model(self, model):
        self.server_models[model.java.tag()] = model.java

    def remove(self, tag):
        self.server_models.pop(tag, None)

    def modelsUsedByOtherClients(self):
        return self.observed_tags


class FakeClient:
    def __init__(self):
        self.version = "6.4"
        self.cores = 1
        self.standalone = False
        self.port = 2036
        self.java = FakeClientJava()
        self.created = 0
        self.loaded = 0
        self.removed = []
        self.remove_calls = 0

    def create(self, name=None):
        self.created += 1
        model = FakeModel(
            name or f"Model {self.created}",
            f"created{self.created}",
        )
        self.java.add_model(model)
        return model

    def load(self, path):
        self.loaded += 1
        model_path = Path(path)
        model = FakeModel(
            model_path.stem,
            f"loaded{self.loaded}",
            model_path,
        )
        self.java.add_model(model)
        return model

    def remove(self, model):
        self.remove_calls += 1
        self.removed.append(model.java.tag())
        self.java.remove(model.java.tag())

    def names(self):
        return [java.label() for java in self.java.server_models.values()]

    def disconnect(self):
        self.port = None


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


@pytest.fixture
def manager():
    sm = SessionManager()
    sm._client = FakeClient()
    sm._server = None
    sm._server_managed = False
    sm._session_mode = "shared-external"
    sm._host = "localhost"
    sm._port = 2036
    sm._models.clear()
    sm._current_model_tag = None
    sm._busy_handler = None
    FakeModelJava.copy_count = 0
    yield sm
    sm._client = None
    sm._server = None
    sm._server_managed = False
    sm._session_mode = None
    sm._host = None
    sm._port = None
    sm._models.clear()
    sm._current_model_tag = None
    sm._busy_handler = None


@pytest.fixture
def model_tools(manager, monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr(model_module, "session_manager", manager)
    monkeypatch.setattr(model_module.mph, "Model", FakeModel.from_java)
    register_model_tools(registry)
    return registry.tools


def test_duplicate_names_are_registered_and_require_tag(manager):
    first = FakeModel("duplicate", "model-a")
    second = FakeModel("duplicate", "model-b")

    first_record = manager.add_model(first, origin="mcp_created")
    second_record = manager.add_model(second, origin="mcp_loaded")

    assert len(manager.models) == 2
    assert set(manager.models) == {"model-a", "model-b"}
    assert manager.get_model("model-a") is first
    assert manager.get_model("model-b") is second
    assert first_record.origin == "mcp_created"
    assert second_record.origin == "mcp_loaded"

    with pytest.raises(AmbiguousModelError) as error:
        manager.get_model("duplicate")

    assert error.value.candidate_tags == ["model-a", "model-b"]
    assert "model-a" in str(error.value)
    assert "model-b" in str(error.value)


def test_exact_tag_wins_over_a_matching_display_name(manager):
    name_match = FakeModel("model-b", "model-a")
    tag_match = FakeModel("other", "model-b")
    manager.add_model(name_match)
    manager.add_model(tag_match)

    assert manager.get_model("model-b") is tag_match


def test_current_identity_survives_rename_and_metadata_is_serializable(manager):
    model = FakeModel("before", "stable-tag")
    record = manager.add_model(model, origin="mcp_cloned")

    assert manager.current_model == "before"
    assert manager.current_model_tag == "stable-tag"
    assert record.access_mode == "write"
    assert record.server_managed is True
    assert record.attached_at.tzinfo is not None
    datetime.fromisoformat(record.metadata()["attached_at"])

    model.java.label("after")

    assert manager.current_model == "after"
    assert manager.current_model_tag == "stable-tag"
    assert manager.get_model("after") is model
    assert manager.get_model("before") is None
    assert manager.set_current_model("after") is True

    status = manager.get_status()
    assert status["models"][0]["name"] == "after"
    assert status["models"][0]["tag"] == "stable-tag"
    assert status["current_model"] == "after"
    assert status["current_model_tag"] == "stable-tag"


def test_set_current_and_remove_use_tags_without_harming_same_name(manager):
    first = FakeModel("duplicate", "model-a")
    second = FakeModel("duplicate", "model-b")
    manager.add_model(first)
    manager.add_model(second)

    assert manager.set_current_model("model-b") is True
    assert manager.current_model_tag == "model-b"
    assert manager.remove_model("model-a") is True

    assert set(manager.models) == {"model-b"}
    assert manager.get_model("duplicate") is second
    assert manager.client.removed == ["model-a"]
    assert manager.remove_model("duplicate") is True
    assert manager.models == {}
    assert manager.client.removed == ["model-a", "model-b"]


def test_disconnect_clears_stale_registry_even_without_live_connection(manager):
    manager.add_model(FakeModel("stale", "stale-tag"))
    manager._client = None

    result = manager.disconnect()

    assert result["success"] is True
    assert manager.models == {}
    assert manager.current_model_tag is None


def test_discovery_uses_server_tags_without_mutating_registry(manager):
    registered = FakeModel("duplicate", "server-a")
    unregistered = FakeModel(
        "duplicate",
        "server-b",
        Path("/desktop/opened_model.mph"),
    )
    manager.client.java.add_model(registered)
    manager.client.java.add_model(unregistered)
    manager.client.java.observed_tags = ["server-b"]
    manager.add_model(registered, origin="mcp_loaded")
    original_current = manager.current_model_tag

    result = manager.discover_server_models()

    assert result["success"] is True
    assert result["count"] == 2
    assert result["registered_count"] == 1
    assert result["unregistered_count"] == 1
    assert manager.current_model_tag == original_current
    assert set(manager.models) == {"server-a"}

    by_tag = {item["tag"]: item for item in result["models"]}
    assert by_tag["server-a"]["name"] == "duplicate"
    assert by_tag["server-a"]["registered"] is True
    assert by_tag["server-a"]["origin"] == "mcp_loaded"
    assert by_tag["server-b"]["name"] == "duplicate"
    assert by_tag["server-b"]["file"] == "/desktop/opened_model.mph"
    assert by_tag["server-b"]["registered"] is False
    assert by_tag["server-b"]["used_by_other_clients"] is True


def test_discovery_keeps_per_model_wrapping_errors_local(manager):
    healthy = FakeModel("healthy", "healthy-tag")
    broken = FakeModel("broken", "broken-tag")
    manager.client.java.add_model(healthy)
    manager.client.java.add_model(broken)
    manager.client.java.broken_tags.add("broken-tag")

    result = manager.discover_server_models()

    assert result["success"] is True
    assert result["count"] == 2
    by_tag = {item["tag"]: item for item in result["models"]}
    assert by_tag["healthy-tag"]["name"] == "healthy"
    assert by_tag["healthy-tag"]["file"] is None
    assert by_tag["broken-tag"]["name"] is None
    assert "inspection_error" in by_tag["broken-tag"]
    assert manager.models == {}


def test_model_tools_record_origins_and_expose_registry_metadata(
    manager,
    model_tools,
    tmp_path,
):
    assert set(model_tools) == {
        "model_load",
        "model_create",
        "model_discover",
        "model_attach",
        "model_detach",
        "model_access_set",
        "model_create_component",
        "model_list_components",
        "model_save",
        "model_save_version",
        "model_list",
        "model_set_current",
        "model_clone",
        "model_remove",
        "model_inspect",
    }

    model_path = tmp_path / "loaded_demo.mph"
    model_path.touch()

    loaded = model_tools["model_load"](str(model_path), set_current=False)
    created = model_tools["model_create"]("created_demo", set_current=True)
    cloned = model_tools["model_clone"](
        created["model_tag"],
        new_name="cloned_demo",
        set_current=True,
    )
    listed = model_tools["model_list"]()

    assert loaded["success"] is True
    assert loaded["model"]["tag"] == "loaded1"
    assert created["success"] is True
    assert created["model"]["tag"] == "created1"
    assert cloned["success"] is True
    assert cloned["model_tag"] == "created1_copy1"
    assert listed["current_model"] == "cloned_demo"
    assert listed["current_model_tag"] == "created1_copy1"

    by_tag = {item["tag"]: item for item in listed["models"]}
    assert by_tag["loaded1"]["origin"] == "mcp_loaded"
    assert by_tag["created1"]["origin"] == "mcp_created"
    assert by_tag["created1_copy1"]["origin"] == "mcp_cloned"
    assert by_tag["created1_copy1"]["access_mode"] == "write"
    assert by_tag["created1_copy1"]["server_managed"] is True
    assert by_tag["created1_copy1"]["is_current"] is True
    assert "attached_at" in by_tag["created1_copy1"]
    assert by_tag["loaded1"]["name"] == "loaded_demo"
    assert by_tag["loaded1"]["file"] == model_path
    assert by_tag["loaded1"]["comsol_version"] == "6.4"


def test_attach_is_exact_observe_only_and_idempotent(
    manager,
    model_tools,
):
    external = FakeModel("desktop_model", "desktop-tag")
    manager.client.java.add_model(external)

    discovered = model_tools["model_discover"]()
    attached = model_tools["model_attach"]("desktop-tag", set_current=False)
    first_record = manager.get_model_record("desktop-tag")
    first_attached_at = first_record.attached_at
    assert manager.current_model_tag is None
    repeated = model_tools["model_attach"]("desktop-tag", set_current=True)
    listed = model_tools["model_list"]()

    assert discovered["models"][0]["registered"] is False
    assert attached["success"] is True
    assert attached["attached"] is True
    assert attached["already_registered"] is False
    assert attached["model"]["origin"] == "external_attached"
    assert attached["model"]["access_mode"] == "observe"
    assert attached["model"]["server_managed"] is False
    assert attached["model"]["is_current"] is False
    assert manager.current_model_tag == "desktop-tag"
    assert manager.get_model("desktop-tag").name() == "desktop_model"
    assert manager.client.loaded == 0
    assert manager.client.created == 0

    assert repeated["success"] is True
    assert repeated["attached"] is False
    assert repeated["already_registered"] is True
    assert manager.get_model_record("desktop-tag").attached_at == first_attached_at
    assert manager.get_model_record("desktop-tag").origin == "external_attached"
    assert listed["models"][0]["tag"] == "desktop-tag"


def test_model_access_set_switches_by_tag_and_is_idempotent(
    manager,
    model_tools,
):
    external = FakeModel("desktop_model", "desktop-tag")
    manager.client.java.add_model(external)
    model_tools["model_attach"]("desktop-tag")

    unchanged = model_tools["model_access_set"]("desktop-tag", "observe")
    elevated = model_tools["model_access_set"]("desktop-tag", "write")
    lowered = model_tools["model_access_set"]("desktop-tag", "observe")

    assert unchanged["success"] is True
    assert unchanged["previous_access_mode"] == "observe"
    assert unchanged["access_mode"] == "observe"
    assert unchanged["changed"] is False
    assert elevated["success"] is True
    assert elevated["model"] == "desktop_model"
    assert elevated["model_tag"] == "desktop-tag"
    assert elevated["previous_access_mode"] == "observe"
    assert elevated["access_mode"] == "write"
    assert elevated["changed"] is True
    assert lowered["previous_access_mode"] == "write"
    assert lowered["access_mode"] == "observe"
    assert lowered["changed"] is True
    assert manager.get_model_record("desktop-tag").access_mode == "observe"


def test_model_access_set_rejects_invalid_and_ambiguous_references(
    manager,
    model_tools,
):
    manager.add_model(FakeModel("duplicate", "model-a"), access_mode="observe")
    manager.add_model(FakeModel("duplicate", "model-b"), access_mode="observe")

    invalid = model_tools["model_access_set"]("model-a", "admin")
    ambiguous = model_tools["model_access_set"]("duplicate", "write")
    missing = model_tools["model_access_set"]("missing", "write")

    assert invalid["success"] is False
    assert "observe" in invalid["error"]
    assert manager.get_model_record("model-a").access_mode == "observe"
    assert ambiguous["success"] is False
    assert ambiguous["candidate_tags"] == ["model-a", "model-b"]
    assert missing == {"success": False, "error": "Model not found: missing"}


def test_attach_failure_never_partially_registers_model(
    manager,
    model_tools,
):
    broken = FakeModel("broken", "broken-tag")
    manager.client.java.add_model(broken)
    manager.client.java.broken_tags.add("broken-tag")

    missing = model_tools["model_attach"]("missing-tag")
    wrapping_failed = model_tools["model_attach"]("broken-tag")

    assert missing["success"] is False
    assert "not found" in missing["error"]
    assert wrapping_failed["success"] is False
    assert "Could not access" in wrapping_failed["error"]
    assert manager.models == {}
    assert manager.current_model_tag is None


def test_detach_preserves_server_and_refuses_managed_models(
    manager,
    model_tools,
):
    managed = FakeModel("managed", "managed-tag")
    external = FakeModel("external", "external-tag")
    manager.client.java.add_model(managed)
    manager.client.java.add_model(external)
    manager.add_model(managed, origin="mcp_created")
    model_tools["model_attach"]("external-tag", set_current=True)

    detached = model_tools["model_detach"]("external")
    refused = model_tools["model_detach"]("managed-tag")

    assert detached["success"] is True
    assert detached["detached_tag"] == "external-tag"
    assert detached["server_model_preserved"] is True
    assert "external-tag" in manager.client.java.tags()
    assert manager.client.remove_calls == 0
    assert manager.current_model_tag == "managed-tag"
    assert refused["success"] is False
    assert "model_remove" in refused["error"]
    assert "managed-tag" in manager.models


def test_model_tools_return_safe_ambiguity_and_remove_by_tag(
    manager,
    model_tools,
):
    manager.add_model(FakeModel("duplicate", "model-a"))
    manager.add_model(FakeModel("duplicate", "model-b"))

    ambiguous = model_tools["model_set_current"]("duplicate")
    selected = model_tools["model_set_current"]("model-b")
    removed = model_tools["model_remove"]("model-a")

    assert ambiguous["success"] is False
    assert ambiguous["candidate_tags"] == ["model-a", "model-b"]
    assert selected == {
        "success": True,
        "current_model": "duplicate",
        "current_model_tag": "model-b",
    }
    assert removed["success"] is True
    assert removed["removed_tag"] == "model-a"
    assert manager.current_model_tag == "model-b"
