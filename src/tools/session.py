"""Session management tools for COMSOL MCP Server."""

import atexit
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

import mph
from jpype import JClass
from mcp.server.fastmcp import FastMCP


logger = logging.getLogger(__name__)


ModelOrigin = Literal[
    "mcp_created",
    "mcp_loaded",
    "mcp_cloned",
    "external_attached",
]
ModelAccessMode = Literal["observe", "write"]


class AmbiguousModelError(ValueError):
    """Raised when a display name matches more than one registered model."""

    def __init__(self, reference: str, candidate_tags: list[str]):
        self.reference = reference
        self.candidate_tags = candidate_tags
        candidates = ", ".join(candidate_tags)
        super().__init__(
            f"Model name is ambiguous: {reference!r}. "
            f"Use one of these COMSOL model tags instead: {candidates}"
        )


class ModelAccessError(PermissionError):
    """Raised before a write is attempted against an observe-mode model."""

    def __init__(self, record: "ModelRecord"):
        self.record = record
        super().__init__(
            f"Model {record.name!r} ({record.tag}) is in observe mode; "
            "write access is required."
        )

    def to_result(self) -> dict:
        """Return the stable structured MCP denial payload."""
        record = self.record
        return {
            "success": False,
            "error": str(self),
            "error_code": "model_write_access_required",
            "model": record.name,
            "model_tag": record.tag,
            "access_mode": record.access_mode,
            "required_access_mode": "write",
            "hint": (
                "Call model_access_set("
                f"model_name={record.tag!r}, access_mode='write') before retrying."
            ),
        }


class StaleModelError(RuntimeError):
    """Raised when a registered model no longer exists on COMSOL Server."""

    def __init__(self, record: "ModelRecord"):
        self.record = record
        super().__init__(
            f"Model {record.name!r} ({record.tag}) is stale because it is no "
            "longer present on COMSOL Server."
        )

    def to_result(self) -> dict:
        record = self.record
        return {
            "success": False,
            "error": str(self),
            "error_code": "model_stale",
            "model": record.name,
            "model_tag": record.tag,
            "stale": True,
            "stale_reason": record.stale_reason,
            "hint": (
                "Run model_discover, then explicitly call model_attach with "
                "the current server tag. Use model_detach to discard this "
                "stale MCP registration."
            ),
        }


class ModelStateUnavailableError(RuntimeError):
    """Raised when Server state cannot be verified safely."""

    def __init__(self, reference: Optional[str], detail: str):
        self.reference = reference
        self.detail = detail
        super().__init__(
            "Could not verify the registered model against COMSOL Server state."
        )

    def to_result(self) -> dict:
        return {
            "success": False,
            "error": str(self),
            "error_code": "model_state_unavailable",
            "model_reference": self.reference,
            "detail": self.detail,
            "hint": (
                "Check the COMSOL Server connection. After reconnecting, run "
                "model_discover and explicitly attach the intended model."
            ),
        }


class ExternalModelLifecycleError(PermissionError):
    """Raised when save/remove is requested for an externally owned model."""

    def __init__(self, record: "ModelRecord", operation: str):
        self.record = record
        self.operation = operation
        super().__init__(
            f"Operation {operation!r} is disabled for externally attached "
            f"model {record.name!r} ({record.tag})."
        )

    def to_result(self) -> dict:
        record = self.record
        return {
            "success": False,
            "error": str(self),
            "error_code": "external_model_lifecycle_protected",
            "operation": self.operation,
            "model": record.name,
            "model_tag": record.tag,
            "origin": record.origin,
            "server_managed": record.server_managed,
            "hint": (
                "Use model_clone to create an MCP-managed copy before saving "
                "or removing it. Use model_detach to release the external model."
            ),
        }


@dataclass
class ModelRecord:
    """Stable identity and lifecycle metadata for a registered COMSOL model."""

    tag: str
    name: str
    model: mph.Model
    origin: ModelOrigin
    access_mode: ModelAccessMode
    server_managed: bool
    attached_at: datetime
    stale: bool = False
    stale_reason: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    file_path: Optional[str] = None
    comsol_version: Optional[str] = None

    def metadata(self, *, is_current: bool = False) -> dict:
        """Return JSON-serializable registry metadata."""
        return {
            "tag": self.tag,
            "name": self.name,
            "origin": self.origin,
            "access_mode": self.access_mode,
            "server_managed": self.server_managed,
            "attached_at": self.attached_at.isoformat(),
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "last_seen_at": (
                self.last_seen_at.isoformat() if self.last_seen_at else None
            ),
            "is_current": is_current,
        }


class SessionManager:
    """Singleton manager for shared COMSOL client-server sessions."""

    _instance: Optional["SessionManager"] = None
    BUSY_TIMEOUT_MS = 120_000

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._client: Optional[mph.Client] = None
        self._server: Optional[mph.Server] = None
        self._server_managed = False
        self._session_mode: Optional[str] = None
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._models: dict[str, ModelRecord] = {}
        self._current_model_tag: Optional[str] = None
        self._busy_handler = None
        self._initialized = True

    @property
    def client(self) -> Optional[mph.Client]:
        return self._client

    @property
    def server(self) -> Optional[mph.Server]:
        return self._server

    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        if getattr(self._client, "standalone", False):
            return True
        return getattr(self._client, "port", None) is not None

    @property
    def current_model(self) -> Optional[str]:
        record = self.get_model_record()
        return record.name if record is not None else None

    @property
    def models(self) -> dict[str, mph.Model]:
        return {tag: record.model for tag, record in self._models.items()}

    @property
    def model_records(self) -> dict[str, ModelRecord]:
        for record in self._models.values():
            self._refresh_record_name(record)
        return self._models.copy()

    @property
    def current_model_tag(self) -> Optional[str]:
        return self._current_model_tag

    @property
    def session_mode(self) -> Optional[str]:
        return self._session_mode

    @staticmethod
    def _is_port_available(port: int) -> bool:
        if port < 1 or port > 65535:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def _configure_busy_handler(self) -> None:
        """Wait briefly instead of failing when the Desktop is refreshing."""
        if self._client is None:
            return
        try:
            handler_class = JClass("com.comsol.model.util.ServerBusyHandler")
            self._busy_handler = handler_class(self.BUSY_TIMEOUT_MS)
            self._client.java.setServerBusyHandler(self._busy_handler)
        except Exception as exc:
            logger.warning("Could not configure COMSOL server busy handler: %s", exc)

    def _desktop_connection_info(self) -> Optional[dict]:
        if self._session_mode not in {"shared-managed", "shared-external"}:
            return None
        return {
            "host": self._host or "localhost",
            "port": self._port,
            "connect_menu": "File > COMSOL Multiphysics Server > Connect to Server",
            "import_menu": "File > COMSOL Multiphysics Server > Import Application from Server",
            "usage": "Connect the existing COMSOL Desktop as a read-only observer.",
        }

    def desktop_import_hint(self, model_name: str) -> Optional[dict]:
        """Return instructions for importing a server-side model in Desktop."""
        connection = self._desktop_connection_info()
        if connection is None:
            return None
        return {
            **connection,
            "model": model_name,
            "message": (
                "In COMSOL Desktop, connect to the server above, then use "
                "Import Application from Server and select this model."
            ),
        }

    def _observer_state(self) -> tuple[list[str], Optional[str]]:
        """Return model tags used by other clients and any check error."""
        if not self.is_connected or self._client is None:
            return [], None
        try:
            tags = self._client.java.modelsUsedByOtherClients()
            return [str(tag) for tag in tags], None
        except Exception as exc:
            return [], str(exc)

    def server_model_tags(self) -> list[str]:
        """Return exact tags for all models currently held by COMSOL Server."""
        if not self.is_connected or self._client is None:
            raise RuntimeError("No active COMSOL session.")
        try:
            return [str(tag) for tag in self._client.java.tags()]
        except Exception as exc:
            raise RuntimeError(
                f"Could not enumerate COMSOL Server models: {exc}"
            ) from exc

    def _wrap_server_model(self, tag: str) -> mph.Model:
        """Wrap one server-side Java model after its tag has been verified."""
        if self._client is None:
            raise RuntimeError("No active COMSOL client.")
        try:
            model = mph.Model(self._client.java.model(tag))
        except Exception as exc:
            raise RuntimeError(
                f"Could not access COMSOL Server model with tag {tag!r}."
            ) from exc
        actual_tag = self._model_tag(model)
        if actual_tag != tag:
            raise RuntimeError(
                f"COMSOL returned model tag {actual_tag!r} for requested tag {tag!r}."
            )
        return model

    def get_server_model_by_tag(self, tag: str) -> Optional[mph.Model]:
        """Return one server model by exact tag without registering it."""
        if tag not in self.server_model_tags():
            return None
        return self._wrap_server_model(tag)

    def discover_server_models(self) -> dict:
        """Inspect server models without changing the MCP registry."""
        tags = self.server_model_tags()
        self._synchronize_registry_from_tags(tags)
        observed_tags, observer_error = self._observer_state()
        models = []

        for tag in tags:
            record = self._models.get(tag)
            entry = {
                "tag": tag,
                "registered": record is not None,
                "stale": False,
                "stale_reason": None,
                "used_by_other_clients": (
                    tag in observed_tags if observer_error is None else None
                ),
            }
            if record is not None:
                entry.update(
                    record.metadata(is_current=tag == self._current_model_tag)
                )
                entry["registered"] = True
                entry["file"] = record.file_path
                entry["comsol_version"] = record.comsol_version
            else:
                entry["is_current"] = False
                try:
                    model = self._wrap_server_model(tag)
                    entry["name"] = self._model_name(model)
                    entry["file"] = self._model_file_path(model)
                    entry["comsol_version"] = self._model_version(model)
                except Exception as exc:
                    entry["name"] = None
                    entry["file"] = None
                    entry["comsol_version"] = None
                    entry["inspection_error"] = str(exc)
            models.append(entry)

        for tag, record in self._models.items():
            if tag in tags:
                continue
            entry = record.metadata(is_current=tag == self._current_model_tag)
            entry.update(
                {
                    "registered": True,
                    "used_by_other_clients": (
                        tag in observed_tags if observer_error is None else None
                    ),
                    "file": record.file_path,
                    "comsol_version": record.comsol_version,
                }
            )
            models.append(entry)

        registered_count = sum(
            1 for item in models if item.get("registered")
        )
        result = {
            "success": True,
            "models": models,
            "count": len(models),
            "server_model_count": len(tags),
            "registered_count": registered_count,
            "unregistered_count": sum(
                1
                for item in models
                if not item.get("registered") and not item.get("stale")
            ),
            "stale_count": sum(1 for item in models if item.get("stale")),
            "current_model": self.current_model,
            "current_model_tag": self._current_model_tag,
        }
        if observer_error:
            result["observer_check_error"] = observer_error
        return result

    def attach_server_model(
        self,
        tag: str,
        *,
        set_current: bool = True,
    ) -> tuple[ModelRecord, bool]:
        """Register an exact server tag as an external, observe-only model."""
        tags = self.server_model_tags()
        self._synchronize_registry_from_tags(tags)
        if tag not in tags:
            raise ValueError(f"COMSOL Server model tag not found: {tag}")

        existing = self._models.get(tag)
        if existing is not None:
            if existing.stale:
                model = self._wrap_server_model(tag)
                now = datetime.now(timezone.utc)
                existing.model = model
                existing.name = self._model_name(model)
                existing.origin = "external_attached"
                existing.access_mode = "observe"
                existing.server_managed = False
                existing.attached_at = now
                existing.last_seen_at = now
                existing.stale = False
                existing.stale_reason = None
                existing.file_path = self._model_file_path(model)
                existing.comsol_version = self._model_version(model)
                if set_current:
                    self._current_model_tag = tag
                return existing, True
            self._refresh_record_name(existing)
            if set_current:
                self._current_model_tag = tag
            return existing, False

        model = self._wrap_server_model(tag)
        previous_current_tag = self._current_model_tag
        record = self.add_model(
            model,
            origin="external_attached",
            access_mode="observe",
            server_managed=False,
        )
        if set_current:
            self._current_model_tag = tag
        else:
            self._current_model_tag = previous_current_tag
        return record, True

    def unregister_external_model(
        self, reference: str
    ) -> Optional[ModelRecord]:
        """Forget an attached external model without removing it from Server."""
        record = self.resolve_model_record(reference)
        if record is None:
            return None
        if record.origin != "external_attached" or record.server_managed:
            raise ValueError(
                "Only externally attached models can be detached. "
                "Use model_remove for MCP-created, loaded, or cloned models."
            )
        del self._models[record.tag]
        if self._current_model_tag == record.tag:
            self._current_model_tag = next(iter(self._models), None)
        return record

    def _session_summary(self, message: Optional[str] = None) -> dict:
        if self._client is None or not self.is_connected:
            return {"success": False, "error": "No active COMSOL session."}
        result = {
            "success": True,
            "version": self._client.version,
            "cores": self._client.cores,
            "standalone": self._client.standalone,
            "session_mode": self._session_mode,
            "server_host": self._host,
            "server_port": self._port,
            "server_managed": self._server_managed,
            "desktop_connection": self._desktop_connection_info(),
        }
        if message:
            result["message"] = message
        return result

    def start(
        self,
        cores: Optional[int] = None,
        version: Optional[str] = None,
        products: Optional[list[str]] = None,
        port: Optional[int] = None,
    ) -> dict:
        """Start a local multi-client COMSOL server and connect to it."""
        if port is not None and not self._is_port_available(port):
            return {
                "success": False,
                "error": f"Requested COMSOL server port is unavailable: {port}",
            }

        if self.is_connected:
            return self._session_summary("Reusing existing shared COMSOL session.")

        if self._client is not None and version:
            client_version = str(self._client.version)
            if client_version != str(version):
                return {
                    "success": False,
                    "error": (
                        f"The MCP process already initialized COMSOL {client_version}. "
                        f"Restart Codex before switching to COMSOL {version}."
                    ),
                }

        server = None
        try:
            server = mph.Server(
                cores=cores,
                version=version,
                port=port,
                multi="on",
            )

            if self._client is None:
                self._client = mph.Client(
                    version=version,
                    port=server.port,
                    host="localhost",
                )
            else:
                self._client.connect(server.port, "localhost")

            self._server = server
            self._server_managed = True
            self._session_mode = "shared-managed"
            self._host = "localhost"
            self._port = server.port
            self._configure_busy_handler()

            result = self._session_summary()
            if products:
                result["products_requested"] = products
            return result
        except Exception as exc:
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    pass
            self._server = None
            self._server_managed = False
            self._session_mode = None
            self._host = None
            self._port = None
            return {"success": False, "error": str(exc)}

    def connect(self, port: int, host: str = "localhost") -> dict:
        """Connect to a COMSOL server managed outside this MCP process."""
        if self.is_connected:
            if self._host == host and self._port == port:
                return self._session_summary("Already connected to this COMSOL server.")
            return {
                "success": False,
                "error": "COMSOL session already running. Disconnect first.",
            }
        if self._server is not None:
            return {
                "success": False,
                "error": "A managed COMSOL server is already running.",
            }

        try:
            if self._client is None:
                self._client = mph.Client(port=port, host=host)
            else:
                self._client.connect(port, host)
            self._server_managed = False
            self._session_mode = "shared-external"
            self._host = host
            self._port = port
            self._configure_busy_handler()
            return self._session_summary()
        except Exception as exc:
            self._session_mode = None
            self._host = None
            self._port = None
            return {"success": False, "error": str(exc)}

    def disconnect(self, force: bool = False) -> dict:
        """Disconnect the client and stop a managed server when safe."""
        if not self.is_connected and self._server is None:
            self._models.clear()
            self._current_model_tag = None
            return {"success": True, "message": "No active session."}

        managed = self._server_managed
        observed_tags, observer_error = self._observer_state()
        if managed and not force and observer_error:
            return {
                "success": False,
                "error": "Could not verify whether another COMSOL client is attached.",
                "observer_check_error": observer_error,
                "hint": "Retry, or call comsol_disconnect(force=true) to close anyway.",
            }
        if managed and not force and observed_tags:
            return {
                "success": False,
                "error": "Another COMSOL client is observing server-side models.",
                "models_used_by_other_clients": observed_tags,
                "hint": (
                    "Disconnect COMSOL Desktop first, or call "
                    "comsol_disconnect(force=true) to close all connections."
                ),
            }

        errors = []
        if self.is_connected and self._client is not None:
            try:
                if self._client.standalone:
                    self._client.clear()
                else:
                    self._client.disconnect()
            except Exception as exc:
                errors.append(f"client disconnect: {exc}")

        if managed and self._server is not None:
            try:
                self._server.stop()
            except Exception as exc:
                errors.append(f"server stop: {exc}")

        self._server = None
        self._server_managed = False
        self._session_mode = None
        self._host = None
        self._port = None
        self._models.clear()
        self._current_model_tag = None
        self._busy_handler = None

        if errors:
            return {
                "success": False,
                "error": "; ".join(errors),
                "message": "COMSOL session cleanup completed with errors.",
            }
        if managed:
            return {
                "success": True,
                "message": "Disconnected from COMSOL and stopped the managed server.",
            }
        return {
            "success": True,
            "message": "Disconnected from external COMSOL server; server left running.",
        }

    def shutdown(self) -> None:
        """Best-effort process-exit cleanup for a managed server."""
        try:
            self.disconnect(force=True)
        except Exception:
            pass

    def get_status(self) -> dict:
        """Get current session, server, model, and Desktop observer status."""
        if not self.is_connected or self._client is None:
            return {
                "connected": False,
                "session_mode": None,
                "message": "No active COMSOL session.",
            }

        registry_sync_error = None
        try:
            self.synchronize_model_registry()
        except Exception as exc:
            registry_sync_error = str(exc)

        observed_tags, observer_error = self._observer_state()
        model_list = []
        registered_names = set()
        records = (
            self.model_records
            if registry_sync_error is None
            else self._models.copy()
        )
        for tag, record in records.items():
            registered_names.add(record.name)
            model_info = record.metadata(is_current=tag == self._current_model_tag)
            model_info["registered"] = True
            model_info["used_by_other_clients"] = (
                tag in observed_tags if observer_error is None else None
            )
            model_info["desktop_attached"] = model_info["used_by_other_clients"]
            model_info["file"] = record.file_path
            model_info["comsol_version"] = record.comsol_version
            model_list.append(model_info)

        # Preserve the previous visibility of client-side names without silently
        # registering server models that this MCP does not own yet.
        try:
            client_names = self._client.names()
        except Exception as exc:
            client_names = []
            if registry_sync_error is None:
                registry_sync_error = str(exc)
        for name in client_names:
            if str(name) not in registered_names:
                model_list.append({"name": str(name), "registered": False})

        current_tag = self._current_model_tag
        current_record = self._models.get(current_tag) if current_tag else None
        current_name = current_record.name if current_record else None

        result = {
            "connected": True,
            "version": self._client.version,
            "cores": self._client.cores,
            "standalone": self._client.standalone,
            "session_mode": self._session_mode,
            "server_host": self._host,
            "server_port": self._port,
            "server_managed": self._server_managed,
            "server_running": (
                self._server.running() if self._server is not None else None
            ),
            "models": model_list,
            "current_model": current_name,
            "current_model_tag": current_tag,
            "models_used_by_other_clients": observed_tags,
            "desktop_attached": bool(current_tag and current_tag in observed_tags),
            "desktop_connection": self._desktop_connection_info(),
            "registry_state_available": registry_sync_error is None,
        }
        if registry_sync_error:
            result["registry_sync_error"] = registry_sync_error
        if observer_error:
            result["observer_check_error"] = observer_error
        return result

    @staticmethod
    def _model_tag(model: mph.Model) -> str:
        """Read and validate the stable COMSOL model tag."""
        try:
            tag = str(model.java.tag())
        except Exception as exc:
            raise ValueError("Could not read the COMSOL model tag.") from exc
        if not tag:
            raise ValueError("COMSOL returned an empty model tag.")
        return tag

    @staticmethod
    def _model_name(model: mph.Model) -> str:
        """Read the mutable display name without using it as identity."""
        try:
            name = str(model.name())
        except Exception as exc:
            raise ValueError("Could not read the COMSOL model name.") from exc
        if not name:
            raise ValueError("COMSOL returned an empty model name.")
        return name

    @staticmethod
    def _model_file_path(model: mph.Model) -> Optional[str]:
        """Return the actual model path, preserving unsaved models as null."""
        try:
            raw_path = str(model.java.getFilePath())
            return raw_path or None
        except Exception:
            try:
                file_path = model.file()
                return str(file_path) if file_path else None
            except Exception:
                return None

    @staticmethod
    def _model_version(model: mph.Model) -> Optional[str]:
        try:
            return str(model.version())
        except Exception:
            return None

    def _refresh_record_name(self, record: ModelRecord) -> None:
        """Refresh a mutable model label while preserving tag identity."""
        if record.stale:
            return
        try:
            record.name = self._model_name(record.model)
        except ValueError:
            logger.warning("Could not refresh model name for tag %s", record.tag)

    def add_model(
        self,
        model: mph.Model,
        *,
        origin: ModelOrigin = "mcp_loaded",
        access_mode: ModelAccessMode = "write",
        server_managed: bool = True,
    ) -> ModelRecord:
        """Register a model by stable COMSOL tag."""
        tag = self._model_tag(model)
        name = self._model_name(model)
        existing = self._models.get(tag)
        if existing is not None:
            existing.model = model
            existing.name = name
            existing.file_path = self._model_file_path(model)
            existing.comsol_version = self._model_version(model)
            if existing.stale:
                now = datetime.now(timezone.utc)
                existing.origin = origin
                existing.access_mode = access_mode
                existing.server_managed = server_managed
                existing.attached_at = now
                existing.last_seen_at = now
                existing.stale = False
                existing.stale_reason = None
            return existing

        now = datetime.now(timezone.utc)
        record = ModelRecord(
            tag=tag,
            name=name,
            model=model,
            origin=origin,
            access_mode=access_mode,
            server_managed=server_managed,
            attached_at=now,
            last_seen_at=now,
            file_path=self._model_file_path(model),
            comsol_version=self._model_version(model),
        )
        self._models[tag] = record
        if self._current_model_tag is None:
            self._current_model_tag = tag
        return record

    def resolve_model_record(
        self, reference: Optional[str] = None
    ) -> Optional[ModelRecord]:
        """Resolve current, exact-tag, or unique-name model references."""
        if reference is None:
            if self._current_model_tag is None:
                return None
            record = self._models.get(self._current_model_tag)
            if record is not None:
                self._refresh_record_name(record)
            return record

        if reference in self._models:
            record = self._models[reference]
            self._refresh_record_name(record)
            return record

        matches = []
        for record in self._models.values():
            self._refresh_record_name(record)
            if record.name == reference:
                matches.append(record)
        if len(matches) > 1:
            raise AmbiguousModelError(
                reference,
                [record.tag for record in matches],
            )
        return matches[0] if matches else None

    def get_model_record(
        self, reference: Optional[str] = None
    ) -> Optional[ModelRecord]:
        """Get registry metadata by current model, tag, or unique name."""
        return self.resolve_model_record(reference)

    def get_model(self, name: Optional[str] = None) -> Optional[mph.Model]:
        """Get a model by current model, exact tag, or unique display name."""
        record = self.resolve_model_record(name)
        return record.model if record is not None else None

    def set_current_model(self, name: str) -> bool:
        """Set the current model by exact tag or unique display name."""
        record = self.resolve_model_record(name)
        if record is None:
            return False
        self._current_model_tag = record.tag
        return True

    def set_model_access(
        self,
        reference: Optional[str],
        access_mode: str,
    ) -> Optional[tuple[ModelRecord, ModelAccessMode]]:
        """Set registry access metadata without changing the COMSOL model."""
        if access_mode not in {"observe", "write"}:
            raise ValueError("access_mode must be either 'observe' or 'write'.")
        record = self.resolve_model_record(reference)
        if record is None:
            return None
        previous_mode = record.access_mode
        record.access_mode = access_mode
        return record, previous_mode

    def _synchronize_registry_from_tags(self, tags: list[str]) -> None:
        """Update lifecycle state only after Server enumeration succeeds."""
        live_tags = set(tags)
        now = datetime.now(timezone.utc)
        for tag, record in self._models.items():
            if tag not in live_tags:
                if not record.stale:
                    record.stale = True
                    record.stale_reason = "model_missing_from_server"
                continue

            # A stale handle stays stale even if its tag later reappears. The
            # new server object must be explicitly wrapped by model_attach.
            if record.stale:
                continue
            record.last_seen_at = now
            record.stale_reason = None
            self._refresh_record_name(record)
            record.file_path = self._model_file_path(record.model)
            record.comsol_version = self._model_version(record.model)

    def synchronize_model_registry(self) -> list[str]:
        """Refresh names/liveness without guessing after enumeration errors."""
        tags = self.server_model_tags()
        self._synchronize_registry_from_tags(tags)
        return tags

    def require_model_live(
        self,
        reference: Optional[str] = None,
    ) -> Optional[ModelRecord]:
        """Resolve a model only after verifying its current Server lifecycle."""
        try:
            self.synchronize_model_registry()
        except Exception as exc:
            raise ModelStateUnavailableError(reference, str(exc)) from exc
        record = self.resolve_model_record(reference)
        if record is not None and record.stale:
            raise StaleModelError(record)
        return record

    def require_write_access(
        self,
        reference: Optional[str] = None,
    ) -> Optional[ModelRecord]:
        """Resolve a model and reject observe-mode writes before tool execution."""
        record = self.resolve_model_record(reference)
        if record is None:
            return None
        if record.access_mode != "write":
            raise ModelAccessError(record)
        return record

    def require_lifecycle_permission(
        self,
        operation: str,
        reference: Optional[str] = None,
    ) -> Optional[ModelRecord]:
        """Protect externally owned models from save and remove operations."""
        record = self.resolve_model_record(reference)
        if record is None:
            return None
        if operation in {"model_save", "model_save_version", "model_remove"} and (
            record.origin == "external_attached" or not record.server_managed
        ):
            raise ExternalModelLifecycleError(record, operation)
        return record

    def remove_model(self, name: str) -> bool:
        """Remove a model by exact tag or unique name."""
        record = self.resolve_model_record(name)
        if record is None or not self.is_connected or self._client is None:
            return False
        try:
            self._client.remove(record.model)
            del self._models[record.tag]
            if self._current_model_tag == record.tag:
                self._current_model_tag = next(iter(self._models), None)
            return True
        except Exception:
            return False


session_manager = SessionManager()
atexit.register(session_manager.shutdown)


def register_session_tools(mcp: FastMCP) -> None:
    """Register session management tools."""

    @mcp.tool()
    def comsol_start(
        cores: Optional[int] = None,
        version: Optional[str] = None,
        products: Optional[list[str]] = None,
        port: Optional[int] = None,
    ) -> dict:
        """
        Start a local multi-client COMSOL server and connect MCP to it.

        The returned host and port can be used by COMSOL Desktop to observe
        the same server-side model. If port is omitted, COMSOL starts with its
        default port 2036 or the next available port.

        Args:
            cores: Number of processor cores to use.
            version: COMSOL version, for example "6.4".
            products: Optional list of requested COMSOL products.
            port: Optional fixed server port.
        """
        return session_manager.start(
            cores=cores,
            version=version,
            products=products,
            port=port,
        )

    @mcp.tool()
    def comsol_connect(port: int, host: str = "localhost") -> dict:
        """Connect MCP to a COMSOL server managed outside this process."""
        return session_manager.connect(port=port, host=host)

    @mcp.tool()
    def comsol_disconnect(force: bool = False) -> dict:
        """
        Disconnect from COMSOL and stop an MCP-managed server.

        For an MCP-managed server this refuses to close while COMSOL Desktop
        is observing a server-side model. External servers are never stopped;
        MCP only disconnects and clears its local registry.
        """
        return session_manager.disconnect(force=force)

    @mcp.tool()
    def comsol_status() -> dict:
        """Get session, server, model, and Desktop observer status."""
        return session_manager.get_status()
