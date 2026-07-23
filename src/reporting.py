"""Audit COMSOL-facing MCP calls for simulation handoff reports."""

from __future__ import annotations

import json
import functools
import inspect
import math
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from mcp.server.fastmcp import FastMCP

from .access_control import requires_write_access, tool_access_policy


KNOWLEDGE_TOOLS = {
    "docs_get",
    "docs_list",
    "physics_get_guide",
    "troubleshoot",
    "modeling_best_practices",
    "pdf_search",
    "pdf_search_status",
    "pdf_list_modules",
}


def should_audit_tool(name: str) -> bool:
    """Return whether a tool call belongs in the COMSOL command journal."""
    return name not in KNOWLEDGE_TOOLS and not name.startswith("simulation_report_")


def command_category(name: str) -> str:
    """Classify a COMSOL MCP tool for the Chinese report."""
    if name.startswith("comsol_"):
        return "会话"
    if name.startswith("results_export_"):
        return "导出"
    if name.startswith("results_"):
        return "结果读取"
    if name.startswith("study_") or name in {"solutions_list", "datasets_list"}:
        return "求解"
    if name.startswith(
        ("model_", "param_", "geometry_", "physics_", "multiphysics_", "mesh_")
    ):
        return "模型与设置"
    return "其他"


def _shape(value: Any) -> list[int]:
    shape = []
    current = value
    while isinstance(current, (list, tuple)):
        shape.append(len(current))
        if not current:
            break
        first = current[0]
        if any(
            isinstance(item, (list, tuple)) != isinstance(first, (list, tuple))
            for item in current
        ):
            break
        current = first
    return shape


def _flatten_numbers(value: Any, output: list[float]) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            output.append(number)
        return True
    if isinstance(value, (list, tuple)):
        return all(_flatten_numbers(item, output) for item in value)
    return False


def summarize_value(value: Any, *, max_items: int = 20, depth: int = 0) -> Any:
    """Convert arbitrary tool values into compact JSON-serializable data."""
    if depth > 8:
        return "<嵌套内容已省略>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag, "abs": abs(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:500] + "…"
    if hasattr(value, "tolist"):
        try:
            value = value.tolist()
        except Exception:
            return str(value)
    if isinstance(value, Mapping):
        summarized = {}
        for key, item in value.items():
            key_text = str(key)
            if any(secret in key_text.lower() for secret in ("password", "token", "secret")):
                summarized[key_text] = "<已隐藏>"
            else:
                summarized[key_text] = summarize_value(
                    item, max_items=max_items, depth=depth + 1
                )
        return summarized
    if isinstance(value, (list, tuple)):
        numbers: list[float] = []
        is_numeric = _flatten_numbers(value, numbers)
        if is_numeric and len(numbers) > max_items:
            return {
                "summary": "numeric_array",
                "count": len(numbers),
                "shape": _shape(value),
                "min": min(numbers) if numbers else None,
                "max": max(numbers) if numbers else None,
                "mean": sum(numbers) / len(numbers) if numbers else None,
            }
        if len(value) > max_items:
            return {
                "summary": "sequence",
                "count": len(value),
                "preview": [
                    summarize_value(item, max_items=max_items, depth=depth + 1)
                    for item in value[:10]
                ],
            }
        return [
            summarize_value(item, max_items=max_items, depth=depth + 1)
            for item in value
        ]
    if hasattr(value, "model_dump"):
        try:
            return summarize_value(
                value.model_dump(), max_items=max_items, depth=depth + 1
            )
        except Exception:
            pass
    return str(value)


def _result_payload(result: Any) -> Any:
    """Extract structured data from FastMCP's converted tool result."""
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
    """Persist a compact JSONL journal and expose report-sized slices."""

    def __init__(self, data_dir: Path | str | None = None):
        base = data_dir or os.environ.get("COMSOL_MCP_DATA_DIR", ".comsol-mcp-data")
        self.data_dir = Path(base).expanduser().resolve()
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        self.session_id = f"{timestamp}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.journal_path = self.data_dir / "audit" / f"{self.session_id}.jsonl"
        self._records: list[dict[str, Any]] = []
        self._cursor = 0
        self._lock = threading.Lock()

    def record(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: Any,
        *,
        duration_ms: float,
        exception: BaseException | None = None,
    ) -> dict[str, Any] | None:
        payload = _result_payload(result)
        report_failure = (
            name.startswith("simulation_report_")
            and (
                exception is not None
                or (
                    isinstance(payload, Mapping)
                    and payload.get("success") is False
                )
            )
        )
        if not should_audit_tool(name) and not report_failure:
            return None

        summarized_result = summarize_value(payload)
        success = exception is None
        error = str(exception) if exception is not None else None
        if isinstance(payload, Mapping):
            if "success" in payload:
                success = bool(payload["success"])
            if payload.get("error"):
                error = str(payload["error"])

        record = {
            "sequence": 0,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool": name,
            "category": command_category(name),
            "arguments": summarize_value(dict(arguments)),
            "success": success,
            "error": error,
            "duration_ms": round(duration_ms, 3),
            "result": summarized_result,
        }
        with self._lock:
            record["sequence"] = len(self._records) + 1
            self._records.append(record)
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def pending_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._records[self._cursor :]]

    def advance_cursor(self) -> int:
        with self._lock:
            count = len(self._records) - self._cursor
            self._cursor = len(self._records)
            return count

    @property
    def cursor(self) -> int:
        return self._cursor


audit_recorder = AuditRecorder()


class AuditedFastMCP(FastMCP):
    """FastMCP server with model access enforcement and command journaling."""

    def __init__(self, *args, recorder: AuditRecorder | None = None, **kwargs):
        self.audit_recorder = recorder or audit_recorder
        super().__init__(*args, **kwargs)

    def add_tool(
        self,
        fn,
        name=None,
        title=None,
        description=None,
        annotations=None,
        icons=None,
        meta=None,
        structured_output=None,
    ) -> None:
        """Wrap guarded tools before FastMCP builds their public schema."""
        tool_name = name or fn.__name__
        parameter_names = tuple(inspect.signature(fn).parameters)
        policy = tool_access_policy(tool_name, parameter_names)

        if policy != "none":
            original = fn

            def denied_or_none(args, kwargs):
                bound = inspect.signature(original).bind_partial(*args, **kwargs)
                arguments = dict(bound.arguments)
                # Imported lazily to keep reporting independent from COMSOL
                # session initialization and to make test replacement reliable.
                from .tools.session import (
                    AmbiguousModelError,
                    ExternalModelLifecycleError,
                    ModelAccessError,
                    ModelStateUnavailableError,
                    StaleModelError,
                    session_manager,
                )

                try:
                    reference = arguments.get("model_name")
                    # Stale external registrations must remain detachable so
                    # users can clean local MCP state without a live handle.
                    if tool_name != "model_detach":
                        session_manager.require_model_live(reference)
                    if requires_write_access(
                        tool_name,
                        arguments,
                        parameter_names,
                    ):
                        session_manager.require_write_access(reference)
                    session_manager.require_lifecycle_permission(
                        tool_name,
                        reference,
                    )
                except (
                    ExternalModelLifecycleError,
                    ModelAccessError,
                    ModelStateUnavailableError,
                    StaleModelError,
                ) as exc:
                    return exc.to_result()
                except AmbiguousModelError as exc:
                    return {
                        "success": False,
                        "error": str(exc),
                        "candidate_tags": exc.candidate_tags,
                    }
                return None

            if inspect.iscoroutinefunction(original):

                @functools.wraps(original)
                async def guarded(*args, **kwargs):
                    denial = denied_or_none(args, kwargs)
                    if denial is not None:
                        return denial
                    return await original(*args, **kwargs)

            else:

                @functools.wraps(original)
                def guarded(*args, **kwargs):
                    denial = denied_or_none(args, kwargs)
                    if denial is not None:
                        return denial
                    return original(*args, **kwargs)

            fn = guarded

        super().add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        started = time.perf_counter()
        try:
            result = await super().call_tool(name, arguments)
        except BaseException as exc:
            self.audit_recorder.record(
                name,
                arguments,
                None,
                duration_ms=(time.perf_counter() - started) * 1000,
                exception=exc,
            )
            raise
        self.audit_recorder.record(
            name,
            arguments,
            result,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result
