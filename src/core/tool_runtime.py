"""Single-operation guards, complete tool reports, and an operation journal."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from .execution import ACTIVE_MODEL, MODEL_OPERATION_LOCK
from .reports import serialize_value, tool_report

logger = logging.getLogger(__name__)
LOCAL_TOOLS = {
    "diagnostic_report_page", "study_get_progress", "study_cancel", "study_wait", "comsol_status",
}


def journal_value(value: Any) -> Any:
    """Retain full values in the journal, redacting explicit credential fields."""
    if isinstance(value, Mapping):
        return {
            str(key): "<已隐藏>" if str(key).lower() in {
                "password", "api_key", "access_token", "secret_key"
            } else journal_value(item)
            for key, item in value.items()
        }
    if hasattr(value, "model_dump"):
        return journal_value(value.model_dump())
    if isinstance(value, (list, tuple)):
        return [journal_value(item) for item in value]
    return serialize_value(value)


def _result_payload(result: Any) -> Any:
    """Extract structured data from FastMCP's converted tool result."""
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], Mapping):
        return dict(result[1])
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, Mapping):
        return dict(structured)
    if isinstance(result, Mapping):
        return dict(result)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        blocks = []
        for block in result:
            if hasattr(block, "text"):
                text = block.text
                try:
                    blocks.append(json.loads(text))
                except (TypeError, json.JSONDecodeError):
                    blocks.append(text)
            elif hasattr(block, "model_dump"):
                blocks.append(block.model_dump())
            else:
                blocks.append(str(block))
        return blocks[0] if len(blocks) == 1 else blocks
    return result


class AuditRecorder:
    """Append actual tool deliveries to a session journal."""

    def __init__(self, data_dir: Path | str | None = None):
        base = data_dir or os.environ.get("COMSOL_MCP_DATA_DIR", ".comsol-mcp-data")
        self.data_dir = Path(base).expanduser().resolve()
        self.session_id = uuid.uuid4().hex
        self.journal_path = self.data_dir / "audit" / f"{self.session_id}.jsonl"
        self._sequence = 0
        self._lock = threading.Lock()

    def record(self, name, arguments, result, *, duration_ms, exception=None):
        payload = _result_payload(result)
        success = exception is None
        if isinstance(payload, Mapping) and "success" in payload:
            success = bool(payload["success"])
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "tool": name, "arguments": journal_value(arguments),
            "success": success, "exception": str(exception) if exception else None,
            "duration_ms": round(duration_ms, 3), "result": journal_value(payload),
        }
        with self._lock:
            self._sequence += 1
            record["sequence"] = self._sequence
            self.journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.journal_path, flags, 0o600)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        return record


audit_recorder = AuditRecorder()


class AuditedFastMCP(FastMCP):
    """Serialize COMSOL calls and attach facts; the Agent controls task steps."""

    def __init__(self, *args, recorder: AuditRecorder | None = None, **kwargs):
        self.audit_recorder = recorder or audit_recorder
        super().__init__(*args, **kwargs)

    def add_tool(self, fn, name=None, title=None, description=None, annotations=None,
                 icons=None, meta=None, structured_output=None) -> None:
        tool_name = name or fn.__name__
        original = fn
        signature = inspect.signature(original)

        @functools.wraps(original)
        def guarded(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            model_token = ACTIVE_MODEL.set(None)
            acquired = False
            invoked = False
            identity = None
            try:
                if tool_name not in LOCAL_TOOLS:
                    from .solver import async_solver
                    if async_solver.is_running or not MODEL_OPERATION_LOCK.acquire(blocking=False):
                        return tool_report(tool_name, arguments, {
                            "success": False, "status": "busy", "operation_invoked": False,
                            "error": "A COMSOL operation is still running.",
                            "active_operation": async_solver.get_progress(),
                        })
                    acquired = True
                    # Check once more after taking the lock to cover a concurrent start.
                    if async_solver.is_running:
                        return tool_report(tool_name, arguments, {
                            "success": False, "status": "busy", "operation_invoked": False,
                            "active_operation": async_solver.get_progress(),
                        })
                    from .session import session_manager
                    if "model_name" in arguments and tool_name != "model_detach":
                        identity = session_manager.require_model_live(arguments["model_name"])
                        if identity is None:
                            raise ValueError(f"Model not registered: {arguments['model_name']}")
                        ACTIVE_MODEL.set((session_manager, arguments["model_name"], identity))
                invoked = True
                result = original(*args, **kwargs)
                if not isinstance(result, dict):
                    result = {"success": True, "value": result}
                if identity is not None:
                    result.setdefault("model_tag", identity.tag)
                return tool_report(tool_name, arguments, result)
            except Exception as exc:
                result = exc.to_result() if hasattr(exc, "to_result") else {
                    "success": False, "error": f"{type(exc).__name__}: {exc}",
                }
                if hasattr(exc, "candidate_tags"):
                    result["candidate_tags"] = exc.candidate_tags
                result.update(operation_invoked=invoked,
                              status="error" if invoked else "rejected")
                if identity is not None:
                    result["model_tag"] = identity.tag
                return tool_report(tool_name, arguments, result)
            finally:
                ACTIVE_MODEL.reset(model_token)
                if acquired:
                    MODEL_OPERATION_LOCK.release()

        super().add_tool(guarded, name=name, title=title, description=description,
                         annotations=annotations, icons=icons, meta=meta,
                         structured_output=structured_output)

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        started = time.perf_counter()
        try:
            result = await super().call_tool(name, arguments)
        except BaseException as exc:
            try:
                self.audit_recorder.record(name, arguments, None,
                    duration_ms=(time.perf_counter() - started) * 1000, exception=exc)
            except Exception:
                logger.exception("Could not record failed tool call %s", name)
            raise
        try:
            self.audit_recorder.record(name, arguments, result,
                duration_ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:
            logger.exception("Could not record tool call %s", name)
            warning = f"Local journal write failed; the tool outcome below remains unchanged: {exc}"
            payload = _result_payload(result)
            if isinstance(payload, dict):
                payload["audit_warning"] = warning
                result = ([TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))],
                          payload)
            elif isinstance(result, list):
                result.append(TextContent(type="text", text=warning))
        return result
