"""Tests for stable tag-based COMSOL model registration."""

from datetime import datetime
from pathlib import Path

import pytest

import src.tools.model as model_module
from src.tools.model import register_model_tools
from src.core.session import AmbiguousModelError, SessionManager


class FakeModelJava:
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

    def save(self, file_path, save_copy=False):
        Path(file_path).write_bytes(b"fake MPH snapshot")
        if not save_copy:
            self._file_path = Path(file_path)

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
        return self.java._file_path

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

    def create(self, tag):
        java = FakeModelJava(tag, tag)
        self.server_models[tag] = java
        return java

    def load(self, tag, file_path):
        java = FakeModelJava(tag, Path(file_path).stem, Path(file_path))
        self.server_models[tag] = java
        return java

    def loadCopy(self, tag, file_path):
        java = FakeModelJava(tag, Path(file_path).stem)
        self.server_models[tag] = java
        return java

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
def manager(monkeypatch):
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
    monkeypatch.setattr(model_module.mph, "Model", FakeModel.from_java)
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
    manager.client.java.add_model(model)
    record = manager.add_model(model, origin="mcp_cloned")

    assert manager.current_model == "before"
    assert manager.current_model_tag == "stable-tag"
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
        "model_save",
        "model_list",
        "model_set_current",
        "model_clone",
        "model_remove",
    }

    model_path = tmp_path / "loaded_demo.mph"
    model_path.touch()

    loaded = model_tools["model_load"](str(model_path), "loaded1", set_current=False)
    created = model_tools["model_create"]("created1", label="created_demo", set_current=True)
    snapshot = tmp_path / "clone.mph"
    cloned = model_tools["model_clone"](
        created["model_tag"], new_tag="clone1", file_path=str(snapshot),
        label="cloned_demo", set_current=True,
    )
    listed = model_tools["model_list"]()

    assert loaded["success"] is True
    assert loaded["model"]["tag"] == "loaded1"
    assert created["success"] is True
    assert created["model"]["tag"] == "created1"
    assert cloned["success"] is True
    assert cloned["model_tag"] == "clone1"
    assert cloned["completed_stages"] == ["save_snapshot", "load_copy"]
    assert snapshot.is_file()
    assert manager.current_model == "cloned_demo"
    assert manager.current_model_tag == "clone1"

    by_tag = {item["tag"]: item for item in listed["models"]}
    assert by_tag["loaded1"]["origin"] == "mcp_loaded"
    assert by_tag["created1"]["origin"] == "mcp_created"
    assert by_tag["clone1"]["origin"] == "mcp_cloned"
    assert by_tag["clone1"]["server_managed"] is True
    assert by_tag["clone1"]["is_current"] is True
    assert "attached_at" in by_tag["clone1"]
    assert by_tag["loaded1"]["name"] == "loaded_demo"
    assert manager.get_model_record("loaded1").file_path == str(model_path)
    assert manager.get_model_record("clone1").file_path is None
    assert manager.get_model_record("created1").file_path is None


def test_attach_is_exact_and_idempotent(
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
    assert attached["model"]["origin"] == "external_attached"
    assert attached["model"]["server_managed"] is False
    assert attached["model"]["is_current"] is False
    assert manager.current_model_tag == "desktop-tag"
    assert manager.get_model("desktop-tag").name() == "desktop_model"
    assert manager.client.loaded == 0
    assert manager.client.created == 0

    assert repeated["success"] is True
    assert repeated["attached"] is False
    assert manager.get_model_record("desktop-tag").attached_at == first_attached_at
    assert manager.get_model_record("desktop-tag").origin == "external_attached"
    assert listed["models"][0]["tag"] == "desktop-tag"


def test_attach_failure_never_partially_registers_model(
    manager,
    model_tools,
):
    broken = FakeModel("broken", "broken-tag")
    manager.client.java.add_model(broken)
    manager.client.java.broken_tags.add("broken-tag")

    with pytest.raises(ValueError, match="not found"):
        model_tools["model_attach"]("missing-tag")
    with pytest.raises(RuntimeError, match="Could not access"):
        model_tools["model_attach"]("broken-tag")
    assert manager.models == {}
    assert manager.current_model_tag is None


def test_detach_preserves_server_models_of_either_origin(manager, model_tools):
    managed = FakeModel("managed", "managed-tag")
    external = FakeModel("external", "external-tag")
    manager.client.java.add_model(managed)
    manager.client.java.add_model(external)
    manager.add_model(managed, origin="mcp_created")
    model_tools["model_attach"]("external-tag", set_current=True)

    for tag in ("external-tag", "managed-tag"):
        detached = model_tools["model_detach"](tag)
        assert detached["success"] is True
        assert detached["model_tag"] == tag
        assert detached["server_model_preserved"] is True
        assert tag in manager.client.java.tags()
    assert manager.models == {}


def test_model_tools_preserve_ambiguity_and_remove_by_tag(manager, model_tools):
    for tag in ("model-a", "model-b"):
        model = FakeModel("duplicate", tag)
        manager.client.java.add_model(model)
        manager.add_model(model)

    with pytest.raises(AmbiguousModelError) as error:
        model_tools["model_set_current"]("duplicate")
    assert error.value.candidate_tags == ["model-a", "model-b"]
    selected = model_tools["model_set_current"]("model-b")
    removed = model_tools["model_remove"]("model-a")

    assert selected == {"success": True, "current_model": "duplicate", "model_tag": "model-b"}
    assert removed["success"] is True
    assert removed["model_tag"] == "model-a"
    assert removed["tag_present_after"] is False
    assert manager.current_model_tag == "model-b"


def test_registration_owns_explicit_current_marker_and_removal(manager):
    first = manager.add_model(FakeModel("first", "model-a"), set_current=False)
    assert manager.current_model_tag is None
    second = manager.add_model(FakeModel("second", "model-b"), set_current=True)
    assert manager.current_model_tag == second.tag
    manager.add_model(first.model, set_current=False)
    assert manager.current_model_tag == second.tag
    manager.add_model(first.model, set_current=True)
    assert manager.current_model_tag == first.tag
    assert manager.unregister_model(first.tag) is first
    assert first.tag not in manager.models
    assert manager.current_model_tag == second.tag
