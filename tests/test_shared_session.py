"""Tests for shared COMSOL client-server session management."""

import os
from pathlib import Path

import pytest

import src.core.session as session_module
import src.core.comsol_environment as environment_module
from src.core.session import SessionManager


class FakeJava:
    def __init__(self):
        self.observed_tags = []
        self.server_tags = ["model1"]

    def tags(self):
        return self.server_tags

    def modelsUsedByOtherClients(self):
        return self.observed_tags


class FakeClient:
    instances = []

    def __init__(self, cores=None, version=None, port=None, host="localhost"):
        self.version = version or "6.4"
        self.cores = cores or 1
        self.port = port
        self.host = host if port is not None else None
        self.standalone = port is None
        self.java = FakeJava()
        self._names = []
        self.clear_calls = 0
        self.disconnect_calls = 0
        self.connect_calls = []
        type(self).instances.append(self)

    def connect(self, port, host="localhost"):
        self.port = port
        self.host = host
        self.standalone = False
        self.connect_calls.append((host, port))

    def disconnect(self):
        self.disconnect_calls += 1
        self.port = None
        self.host = None

    def clear(self):
        self.clear_calls += 1

    def names(self):
        return self._names.copy()


class FakeServer:
    instances = []

    def __init__(self, cores=None, version=None, port=None, multi=None):
        self.cores = cores
        self.version = version or "6.4"
        self.port = port or 2036
        self.multi = multi
        self.stopped = False
        type(self).instances.append(self)

    def running(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


class FakeModelJava:
    def __init__(self, tag):
        self._tag = tag

    def tag(self):
        return self._tag


class FakeModel:
    def __init__(self, name="demo", tag="model1"):
        self._name = name
        self.java = FakeModelJava(tag)

    def name(self):
        return self._name

    def file(self):
        return Path(f"/tmp/{self._name}.mph")


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.delenv("COMSOL_MCP_COMSOL_ROOT", raising=False)
    monkeypatch.delenv("COMSOL_MCP_COMSOL_VERSION", raising=False)
    FakeClient.instances.clear()
    FakeServer.instances.clear()
    sm = SessionManager()
    sm._client = None
    sm._server = None
    sm._server_managed = False
    sm._session_mode = None
    sm._host = None
    sm._port = None
    sm._models.clear()
    sm._current_model_tag = None
    sm._busy_handler = None
    monkeypatch.setattr(sm, "_configure_busy_handler", lambda: None)
    monkeypatch.setattr(session_module.mph, "Client", FakeClient)
    monkeypatch.setattr(session_module.mph, "Server", FakeServer)
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


def test_start_creates_multi_client_server(manager):
    result = manager.start(cores=2, version="6.4")

    assert result["success"] is True
    assert result["standalone"] is False
    assert result["session_mode"] == "shared-managed"
    assert result["server_host"] == "localhost"
    assert result["server_port"] == 2036
    assert result["server_managed"] is True
    assert FakeServer.instances[0].multi == "on"
    assert result["desktop_connection"]["port"] == 2036


def test_start_reuses_session_without_clearing_models(manager):
    first = manager.start(version="6.4")
    model = FakeModel()
    manager.add_model(model)

    second = manager.start(version="6.4")

    assert first["success"] is True
    assert second["success"] is True
    assert "Reusing" in second["message"]
    assert manager.current_model == "demo"
    assert manager.current_model_tag == "model1"
    assert manager.client.clear_calls == 0
    assert len(FakeServer.instances) == 1


def test_explicit_occupied_port_is_rejected(manager, monkeypatch):
    monkeypatch.setattr(manager, "_is_port_available", lambda port: False)

    result = manager.start(version="6.4", port=2036)

    assert result["success"] is False
    assert "unavailable" in result["error"]
    assert FakeServer.instances == []


def test_status_detects_desktop_observer(manager):
    manager.start(version="6.4")
    model = FakeModel()
    manager.add_model(model)
    manager.client._names = ["demo"]
    manager.client.java.observed_tags = ["model1"]

    status = manager.get_status()

    assert status["desktop_attached"] is True
    assert status["models_used_by_other_clients"] == ["model1"]
    assert status["models"][0]["tag"] == "model1"
    assert status["models"][0]["origin"] == "mcp_loaded"
    assert status["models"][0]["server_managed"] is True
    assert status["models"][0]["is_current"] is True
    assert status["models"][0]["file"] == "/tmp/demo.mph"
    assert status["current_model_tag"] == "model1"


def test_disconnect_requires_force_while_desktop_is_attached(manager):
    manager.start(version="6.4")
    manager.add_model(FakeModel())
    manager.client.java.observed_tags = ["model1"]

    refused = manager.disconnect()

    assert refused["success"] is False
    assert manager.is_connected is True
    assert manager.server.running() is True

    closed = manager.disconnect(force=True)

    assert closed["success"] is True
    assert FakeServer.instances[0].stopped is True
    assert manager.is_connected is False
    assert manager.models == {}
    assert manager.current_model is None
    assert manager.current_model_tag is None


def test_external_server_is_never_stopped(manager):
    connected = manager.connect(port=3040, host="localhost")

    assert connected["success"] is True
    assert connected["session_mode"] == "shared-external"
    assert manager.server is None

    disconnected = manager.disconnect()

    assert disconnected["success"] is True
    assert "left running" in disconnected["message"]
    assert manager.client.disconnect_calls == 1


def test_connect_uses_configured_installation_before_creating_client(manager, tmp_path, monkeypatch):
    root = tmp_path / "custom COMSOL" / "Multiphysics"
    binary = root / "bin" / "win64"
    binary.mkdir(parents=True)
    (binary / "comsol.exe").touch()
    monkeypatch.setattr(environment_module, "platform_architecture", lambda: "win64")
    monkeypatch.setenv("COMSOL_MCP_COMSOL_ROOT", str(root))
    monkeypatch.setenv("COMSOL_MCP_COMSOL_VERSION", "6.3")
    monkeypatch.setenv("PATH", "existing-path")
    observed = []

    class ConfiguredClient(FakeClient):
        def __init__(self, **kwargs):
            observed.append((os.environ["PATH"], kwargs.get("version")))
            super().__init__(**kwargs)

    monkeypatch.setattr(session_module.mph, "Client", ConfiguredClient)
    result = manager.connect(port=2036)

    assert result["success"] is True
    assert observed == [(str(binary) + os.pathsep + "existing-path", "6.3")]
    assert result["version"] == "6.3"


@pytest.mark.parametrize("explicit_version, expected", [(None, "6.3"), ("6.4", "6.4")])
def test_start_selects_version_from_environment_or_argument(manager, monkeypatch, explicit_version, expected):
    monkeypatch.setenv("COMSOL_MCP_COMSOL_VERSION", "6.3")

    result = manager.start(version=explicit_version)

    assert result["success"] is True
    assert FakeServer.instances[0].version == expected
    assert FakeClient.instances[0].version == expected


def test_bad_installation_path_returns_actionable_error_before_client_start(manager, tmp_path, monkeypatch):
    root = tmp_path / "missing-comsol"
    monkeypatch.setenv("COMSOL_MCP_COMSOL_ROOT", str(root))

    result = manager.connect(port=2036)

    assert result["success"] is False
    assert result["stage"] == "configure_environment"
    assert "COMSOL_MCP_COMSOL_ROOT" in result["error"]
    assert str(root) in result["error"]
    assert FakeClient.instances == []


def test_no_installation_override_preserves_path(manager, monkeypatch):
    monkeypatch.setenv("PATH", "existing-path")

    result = manager.connect(port=2036)

    assert result["success"] is True
    assert os.environ["PATH"] == "existing-path"


def test_client_connection_failure_stops_managed_server(manager, monkeypatch):
    class FailingClient:
        def __init__(self, **kwargs):
            raise RuntimeError("connection failed")

    monkeypatch.setattr(session_module.mph, "Client", FailingClient)

    result = manager.start(version="6.4")

    assert result["success"] is False
    assert "connection failed" in result["error"]
    assert FakeServer.instances[0].stopped is True
    assert manager.server is None


def test_disconnected_thin_client_can_reconnect_to_new_server(manager):
    first = manager.start(version="6.4")
    first_client = manager.client
    manager.disconnect()

    second = manager.start(version="6.4")

    assert first["success"] is True
    assert second["success"] is True
    assert manager.client is first_client
    assert first_client.connect_calls == [("localhost", 2036)]
    assert len(FakeServer.instances) == 2
