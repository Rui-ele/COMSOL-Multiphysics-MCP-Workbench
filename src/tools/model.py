"""Explicit model identity and file operations for COMSOL Server."""

from pathlib import Path
from typing import Literal

import mph
from mcp.server.fastmcp import FastMCP

from ..core.session import session_manager


def _record(model_name: str):
    record = session_manager.get_model_record(model_name)
    if record is None:
        raise ValueError(f"Model not registered: {model_name}")
    return record


def _new_tag(tag: str) -> None:
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("model_tag must be a nonempty exact tag.")
    if tag in session_manager.server_model_tags():
        raise ValueError(f"Model tag already exists: {tag}")


def _output_path(file_path: str, suffix: str, overwrite: bool) -> Path:
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        raise ValueError("Use an absolute file path on the MCP client machine.")
    if path.suffix.lower() != suffix:
        raise ValueError(f"Output path must end with {suffix}.")
    if not path.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {path.parent}")
    if path.exists() and not overwrite:
        raise ValueError(f"Output already exists: {path}; specify overwrite=true to replace it.")
    if path.exists() and not path.is_file():
        raise ValueError(f"Output is not a regular file: {path}")
    return path


def _file_state(path: Path) -> dict:
    try:
        stat = path.stat()
        return {"path": str(path), "exists": True, "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns, "source": "MCP client filesystem"}
    except FileNotFoundError:
        return {"path": str(path), "exists": False, "source": "MCP client filesystem"}
    except OSError as exc:
        return {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}


def _register(java_model, origin: str, set_current: bool):
    return session_manager.add_model(
        mph.Model(java_model), origin=origin, set_current=set_current,
    )


def register_model_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def model_discover() -> dict:
        """List Server model tags, labels, files and registration state."""
        return session_manager.discover_server_models()

    @mcp.tool()
    def model_attach(model_tag: str, set_current: bool = False) -> dict:
        """Register an existing Server model by its discovered exact tag."""
        record, created = session_manager.attach_server_model(model_tag, set_current=set_current)
        return {"success": True, "model_tag": record.tag, "attached": created,
                "model": record.metadata(is_current=record.tag == session_manager.current_model_tag)}

    @mcp.tool()
    def model_detach(model_name: str) -> dict:
        """Forget a model's MCP registration while keeping it on COMSOL Server."""
        record = session_manager.unregister_model(model_name)
        if record is None:
            return {"success": False, "error": f"Model not registered: {model_name}"}
        return {"success": True, "model_tag": record.tag, "server_model_preserved": True}

    @mcp.tool()
    def model_list() -> dict:
        """List registered model identities and their current Server liveness."""
        session_manager.synchronize_model_registry()
        return {"success": True, "models": [
            record.metadata(is_current=tag == session_manager.current_model_tag)
            for tag, record in session_manager.model_records.items()
        ]}

    @mcp.tool()
    def model_set_current(model_name: str) -> dict:
        """Set the current model marker; operation tools still receive a model explicitly."""
        record = _record(model_name)
        session_manager.set_current_model(record.tag)
        return {"success": True, "model_tag": record.tag, "current_model": record.name}

    @mcp.tool()
    def model_load(file_path: str, model_tag: str, as_copy: bool = False,
                   set_current: bool = False) -> dict:
        """Load an absolute client-side .mph path under an unused Server tag.

        as_copy uses ModelUtil.loadCopy: the loaded model has no associated save
        location. Return its actual model tag and registration metadata.
        """
        _new_tag(model_tag)
        path = Path(file_path).expanduser()
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".mph":
            raise ValueError("file_path must identify an existing absolute .mph file.")
        result = {"success": False, "write_attempted": False, "model_tag": model_tag,
                  "file": str(path), "as_copy": as_copy, "stage": "load"}
        try:
            client = session_manager.client
            result["write_attempted"] = True
            loader = client.java.loadCopy if as_copy else client.java.load
            record = _register(loader(model_tag, str(path)), "mcp_loaded", set_current)
            result.update(success=True, status="loaded", stage="complete",
                          model=record.metadata(is_current=set_current))
        except Exception as exc:
            result.update(status="needs_review", error=f"{type(exc).__name__}: {exc}")
            try:
                result["server_tag_present"] = model_tag in session_manager.server_model_tags()
            except Exception as read_error:
                result["state_error"] = str(read_error)
        return result

    @mcp.tool()
    def model_create(model_tag: str, label: str | None = None, set_current: bool = False) -> dict:
        """Create one empty model under an unused exact tag, then register it."""
        _new_tag(model_tag)
        result = {"success": False, "model_tag": model_tag, "write_attempted": True, "stage": "create"}
        try:
            java = session_manager.client.java.create(model_tag)
            record = _register(java, "mcp_created", set_current)
            result["stage"] = "label"
            if label is not None:
                java.label(label)
            record.name = str(java.label())
            result.update(success=True, status="created", stage="complete",
                          model=record.metadata(is_current=set_current))
        except Exception as exc:
            result.update(status="needs_review", error=f"{type(exc).__name__}: {exc}")
            try:
                result["server_tag_present"] = model_tag in session_manager.server_model_tags()
            except Exception as read_error:
                result["state_error"] = str(read_error)
        return result

    @mcp.tool()
    def model_save(model_name: str, file_path: str, format: Literal["mph", "java", "m", "vba"] = "mph",
                   save_copy: bool = False, overwrite: bool = False) -> dict:
        """Save the specified model to an explicit absolute client-side path.

        format selects the required suffix. For mph, save_copy=true retains the
        original model save location; false associates it with file_path.
        overwrite=true explicitly permits replacing an existing output.
        Returns the API outcome and actual file metadata; file existence alone
        does not establish the physical validity of a simulation.
        """
        record = _record(model_name)
        path = _output_path(file_path, "." + format, overwrite)
        if save_copy and format != "mph":
            raise ValueError("save_copy applies to mph files.")
        result = {"success": False, "model_tag": record.tag, "write_attempted": False,
                  "write_returned": False, "file_before": _file_state(path)}
        try:
            result["write_attempted"] = True
            if format == "mph":
                record.model.java.save(str(path), save_copy)
            else:
                record.model.java.save(str(path), format)
            result["write_returned"] = True
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["file_after"] = _file_state(path)
        result["file_verified"] = (
            result["file_after"].get("size_bytes", 0) > 0
            and result["file_before"] != result["file_after"]
        )
        result["success"] = result["write_returned"] and result["file_verified"]
        result["status"] = "saved" if result["success"] else "needs_review"
        if result["write_returned"] and not result["file_verified"]:
            result["error"] = "The save call returned, but a new or updated nonempty output file could not be verified."
        try:
            record.file_path = str(record.model.java.getFilePath()) or None
            result["model_save_location"] = record.file_path
        except Exception as exc:
            result["save_location_error"] = f"{type(exc).__name__}: {exc}"
        return result

    @mcp.tool()
    def model_clone(model_name: str, new_tag: str, file_path: str,
                    label: str | None = None, set_current: bool = False,
                    overwrite: bool = False) -> dict:
        """Copy current model state through an explicitly named .mph snapshot.

        Saves file_path with saveCopy=true, then loads that snapshot with
        ModelUtil.loadCopy under new_tag. The source keeps its save location;
        the snapshot remains at the requested path. Returns each completed stage.
        """
        record = _record(model_name)
        _new_tag(new_tag)
        path = _output_path(file_path, ".mph", overwrite)
        result = {"success": False, "source_model_tag": record.tag, "clone_tag": new_tag,
                  "write_attempted": False, "completed_stages": [], "stage": "save_snapshot"}
        try:
            result["write_attempted"] = True
            record.model.java.save(str(path), True)
            result["completed_stages"].append("save_snapshot")
            result["stage"] = "load_copy"
            java = session_manager.client.java.loadCopy(new_tag, str(path))
            result["completed_stages"].append("load_copy")
            clone = _register(java, "mcp_cloned", set_current)
            result["stage"] = "label"
            if label is not None:
                java.label(label)
            clone.name = str(java.label())
            result.update(success=True, status="cloned", stage="complete",
                          model_tag=clone.tag, model=clone.metadata(is_current=set_current))
        except Exception as exc:
            result.update(status="needs_review", error=f"{type(exc).__name__}: {exc}")
            try:
                result["clone_tag_present"] = new_tag in session_manager.server_model_tags()
            except Exception as read_error:
                result["state_error"] = str(read_error)
        result["snapshot"] = _file_state(path)
        return result

    @mcp.tool()
    def model_remove(model_name: str) -> dict:
        """Remove the explicitly selected model from Server and verify tag absence."""
        record = _record(model_name)
        result = {"success": False, "model_tag": record.tag,
                  "write_attempted": True, "write_returned": False}
        try:
            session_manager.client.java.remove(record.tag)
            result["write_returned"] = True
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            result["tag_present_after"] = record.tag in session_manager.server_model_tags()
            if not result["tag_present_after"]:
                session_manager.unregister_model(record.tag)
            result["success"] = result["write_returned"] and not result["tag_present_after"]
        except Exception as exc:
            result["readback_error"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "removed" if result["success"] else "needs_review"
        return result
