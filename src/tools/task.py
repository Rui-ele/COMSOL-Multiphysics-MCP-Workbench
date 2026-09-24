"""Narrow, reviewable parameter-task workflow exposed through COMSOL MCP."""

from __future__ import annotations

import threading
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from ..task_ledger import TaskLedger
from ..task_protocol import ParameterTask, task_fingerprint
from .session import session_manager


_execution_lock = threading.Lock()


def _parse_task(raw: dict[str, Any]) -> tuple[ParameterTask | None, dict | None]:
    try:
        return ParameterTask.model_validate(raw), None
    except ValidationError as exc:
        return None, {
            "success": False,
            "status": "rejected",
            "error_code": "invalid_task",
            "errors": exc.errors(
                include_url=False,
                include_input=False,
                include_context=False,
            ),
        }


def _model_for_task(task: ParameterTask, model_name: str) -> tuple[Any | None, dict | None]:
    if model_name != task.model_tag:
        return None, {
            "success": False,
            "task_id": task.task_id,
            "status": "rejected",
            "error_code": "model_tag_mismatch",
            "errors": ["model_name must be the exact model_tag in the task."],
        }
    record = session_manager.get_model_record(model_name)
    if record is None or record.tag != model_name:
        return None, {
            "success": False,
            "task_id": task.task_id,
            "status": "rejected",
            "error_code": "model_not_registered",
            "errors": ["Discover and attach the exact model tag first."],
        }
    if session_manager.connection_token is None:
        return None, {
            "success": False,
            "task_id": task.task_id,
            "status": "rejected",
            "error_code": "session_identity_unavailable",
            "errors": ["Reconnect to COMSOL Server and prepare a new task."],
        }
    return record, None


def _model_identity(record: Any) -> str:
    """Bind a preview to this connection and registered model handle."""
    return (
        f"{session_manager.connection_token}:"
        f"{record.attached_at.isoformat()}:{id(record.model)}"
    )


def _read_expression(model: Any, parameter: str) -> str:
    expression = model.parameter(parameter)
    if not isinstance(expression, str):
        raise ValueError("COMSOL did not return a parameter expression string.")
    return expression


def _result_base(task: ParameterTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "protocol_version": task.protocol_version,
        "goal": task.goal,
        "model_tag": task.model_tag,
        "target": {"parameter": task.target.parameter},
        "read_state_before": None,
        "performed_operations": [],
        "read_state_after": None,
        "verification": {
            "passed": False,
            "expected_expression": task.expected_result.parameter_expression,
            "actual_expression": None,
        },
        "errors": [],
        "requires_expert_review": True,
    }


def register_task_tools(mcp: FastMCP, ledger: TaskLedger | None = None) -> None:
    """Register the first task-protocol PoC without broad agent-side writes."""
    ledger = ledger or TaskLedger()

    @mcp.tool()
    def task_parameter_preview(model_name: str, task: dict[str, Any]) -> dict:
        """Read an existing parameter and prepare a narrow task for expert review.

        model_name must be an exact COMSOL tag. This tool never changes a model.
        Approval is performed through a separate trusted operator workflow;
        this MCP server cannot approve its own pending tasks.
        """
        parsed, error = _parse_task(task)
        if error is not None:
            return error
        assert parsed is not None
        record, error = _model_for_task(parsed, model_name)
        if error is not None:
            return error
        try:
            actual = _read_expression(record.model, parsed.target.parameter)
        except Exception as exc:
            return {
                "success": False,
                "task_id": parsed.task_id,
                "status": "rejected",
                "error_code": "parameter_read_failed",
                "errors": [str(exc)],
            }

        preview = {
            "task_id": parsed.task_id,
            "goal": parsed.goal,
            "model_tag": parsed.model_tag,
            "local_model_identity": _model_identity(record),
            "observed_state": {
                "parameter": parsed.target.parameter,
                "parameter_expression": actual,
            },
            "requested_operation": {
                "tool": "param_set",
                "parameter": parsed.target.parameter,
                "new_expression": parsed.target.new_expression,
            },
            "allowed_operations": parsed.allowed_operations,
            "expected_result": parsed.expected_result.model_dump(),
            "precondition_match": actual == parsed.target.expected_current_expression,
        }
        if not preview["precondition_match"]:
            return {
                "success": False,
                "status": "rejected",
                "error_code": "precondition_mismatch",
                "preview": preview,
                "errors": ["Actual parameter expression differs from the task precondition."],
            }

        fingerprint = task_fingerprint(parsed)
        try:
            prepared = ledger.prepare(parsed.task_id, fingerprint, preview)
            if prepared["status"] != "pending":
                raise ValueError(
                    "task_id has already been claimed or completed; create a new task."
                )
        except ValueError as exc:
            return {
                "success": False,
                "task_id": parsed.task_id,
                "status": "rejected",
                "error_code": "task_id_conflict",
                "errors": [str(exc)],
            }
        return {
            "success": True,
            "status": "pending",
            "preview": preview,
            "task_fingerprint": fingerprint,
            "requires_expert_approval": True,
        }

    @mcp.tool()
    def task_parameter_execute(
        model_name: str,
        task: dict[str, Any],
    ) -> dict:
        """Execute one expert-confirmed parameter change and read back COMSOL.

        The task must already have been approved outside MCP, and the model
        must be in explicit write mode. This tool never enables write access,
        builds geometry or mesh, solves, or saves.
        """
        parsed, error = _parse_task(task)
        if error is not None:
            return error
        assert parsed is not None
        record, error = _model_for_task(parsed, model_name)
        if error is not None:
            return error

        result = _result_base(parsed)
        fingerprint = task_fingerprint(parsed)
        with _execution_lock:
            try:
                prepared = ledger.claim(parsed.task_id, fingerprint)
            except ValueError as exc:
                result.update(
                    success=False,
                    status="rejected",
                    error_code="approval_or_state_invalid",
                )
                result["errors"].append(str(exc))
                return result

            if prepared["preview"].get("local_model_identity") != _model_identity(record):
                result["errors"].append(
                    "COMSOL connection or attached model changed after preview; "
                    "prepare a new task with a new task_id."
                )
                result.update(success=False, status="rejected", error_code="model_identity_changed")
                ledger.finish(parsed.task_id, "rejected", result)
                return result

            try:
                before = _read_expression(record.model, parsed.target.parameter)
            except Exception as exc:
                result["errors"].append(f"Read before mutation failed: {exc}")
                result.update(success=False, status="failed", error_code="parameter_read_failed")
                ledger.finish(parsed.task_id, "failed", result)
                return result

            result["read_state_before"] = {
                "parameter": parsed.target.parameter,
                "parameter_expression": before,
            }
            preview_value = prepared["preview"]["observed_state"]["parameter_expression"]
            if (
                before != parsed.target.expected_current_expression
                or before != preview_value
            ):
                result["errors"].append(
                    "Parameter changed after preview or differs from the approved precondition."
                )
                result.update(success=False, status="rejected", error_code="precondition_mismatch")
                ledger.finish(parsed.task_id, "rejected", result)
                return result

            operation = {
                "tool": "param_set",
                "parameter": parsed.target.parameter,
                "requested_expression": parsed.target.new_expression,
                "status": "attempted",
            }
            result["performed_operations"].append(operation)
            try:
                record.model.parameter(parsed.target.parameter, parsed.target.new_expression)
                operation["status"] = "returned_without_error"
            except Exception as exc:
                operation["status"] = "error_unknown_effect"
                result["errors"].append(f"Parameter write raised an error: {exc}")

            try:
                after = _read_expression(record.model, parsed.target.parameter)
                result["read_state_after"] = {
                    "parameter": parsed.target.parameter,
                    "parameter_expression": after,
                }
                result["verification"]["actual_expression"] = after
            except Exception as exc:
                result["errors"].append(f"Readback failed: {exc}")

            verified = (
                operation["status"] == "returned_without_error"
                and result["verification"]["actual_expression"]
                == parsed.expected_result.parameter_expression
            )
            result["verification"]["passed"] = verified
            result["status"] = "verified" if verified else "needs_review"
            result["success"] = verified
            if not verified:
                result["error_code"] = "verification_failed_or_effect_unknown"
            ledger.finish(parsed.task_id, result["status"], result)
            return result

    @mcp.tool()
    def task_parameter_status(task_id: str) -> dict:
        """Query a locally persisted parameter-task preview or final result."""
        record = ledger.get(task_id)
        if record is None:
            return {
                "success": False,
                "error_code": "task_not_found",
                "task_id": task_id,
            }
        return {
            "success": True,
            "task_id": task_id,
            "status": record["status"],
            "preview": record["preview"],
            "result": record["result"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "approved_at": record["approved_at"],
            "requires_manual_reconciliation": record["status"] == "running",
        }
