"""Operation serialization and availability of local run monitoring."""

import threading
from types import SimpleNamespace

import pytest

from src.core.execution import MODEL_OPERATION_LOCK
from src.core.tool_runtime import AuditRecorder, AuditedFastMCP, _result_payload


@pytest.mark.asyncio
async def test_busy_solver_rejects_model_calls_but_allows_run_status(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.core.solver.async_solver",
        SimpleNamespace(is_running=True, get_progress=lambda: {"run_id": "run-a", "status": "running"}),
    )
    mcp = AuditedFastMCP("busy-run", recorder=AuditRecorder(tmp_path))
    calls = []

    @mcp.tool()
    def example_operation() -> dict:
        calls.append("operation")
        return {"success": True}

    @mcp.tool()
    def study_get_progress(run_id: str) -> dict:
        calls.append(run_id)
        return {"success": True, "run": {"run_id": run_id, "status": "running"}}

    busy = _result_payload(await mcp.call_tool("example_operation", {}))
    progress = _result_payload(await mcp.call_tool("study_get_progress", {"run_id": "run-a"}))
    assert busy["success"] is False
    assert busy["status"] == "busy"
    assert busy["operation_invoked"] is False
    assert progress["run"]["run_id"] == "run-a"
    assert calls == ["run-a"]


@pytest.mark.asyncio
async def test_other_thread_holding_operation_lock_prevents_invocation(tmp_path):
    held, release = threading.Event(), threading.Event()
    calls = []

    def hold_lock():
        with MODEL_OPERATION_LOCK:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    try:
        assert held.wait(2)
        mcp = AuditedFastMCP("busy-lock", recorder=AuditRecorder(tmp_path))

        @mcp.tool()
        def example_operation() -> dict:
            calls.append("operation")
            return {"success": True}

        result = _result_payload(await mcp.call_tool("example_operation", {}))
        assert result["status"] == "busy"
        assert result["operation_invoked"] is False
        assert calls == []
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_call_reuses_one_live_model_and_releases_context(tmp_path, monkeypatch, fail):
    from src.core.execution import ACTIVE_MODEL
    from src.core.session import session_manager

    record = SimpleNamespace(tag="model-a")
    lookups = []

    def require_live(reference):
        lookups.append(reference)
        return record

    monkeypatch.setattr(session_manager, "_models", {record.tag: record})
    monkeypatch.setattr(session_manager, "require_model_live", require_live)
    monkeypatch.setattr(session_manager, "resolve_model_record", lambda *a: pytest.fail("Repeated model resolution"))
    mcp = AuditedFastMCP("resolved-model", recorder=AuditRecorder(tmp_path))

    @mcp.tool()
    def example_model_operation(model_name: str) -> dict:
        assert session_manager.get_model_record(model_name) is record
        assert session_manager.get_model_record(record.tag) is record
        if fail:
            raise RuntimeError("operation failed after resolution")
        return {"success": True, "value": 42}

    result = _result_payload(await mcp.call_tool("example_model_operation", {"model_name": "unique label"}))
    assert lookups == ["unique label"]
    assert result["success"] is not fail
    assert result["model_tag"] == record.tag
    assert ACTIVE_MODEL.get() is None
    # A completed or failed call must leave the shared lock usable by another thread.
    acquired = []
    def probe():
        locked = MODEL_OPERATION_LOCK.acquire(blocking=False)
        acquired.append(locked)
        if locked:
            MODEL_OPERATION_LOCK.release()
    worker = threading.Thread(target=probe)
    worker.start()
    worker.join(2)
    assert acquired == [True]
