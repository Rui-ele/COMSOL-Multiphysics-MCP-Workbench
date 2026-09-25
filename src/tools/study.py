"""Start and monitor explicitly targeted asynchronous COMSOL studies."""

from mcp.server.fastmcp import FastMCP

from ..core.session import session_manager
from ..core.solver import async_solver


def register_study_tools(mcp: FastMCP) -> None:
    """Register a single solver entry and run-specific monitoring tools."""

    @mcp.tool()
    def study_solve(model_name: str, study_tag: str) -> dict:
        """Start the exact existing study in the background and return run_id.

        model_name and study_tag are actual COMSOL tags obtained by discovery or
        reading. Calls Java study(study_tag).run(); COMSOL executes that study's
        configured sequence. The response acknowledges startup; query the run
        with study_get_progress or study_wait to obtain completion or failure.
        """
        try:
            record = session_manager.get_model_record(model_name)
            if record is None or record.tag != model_name:
                raise ValueError(f"Registered model tag not found: {model_name}")
            java = record.model.java
            tags = [str(tag) for tag in java.study().tags()]
            if study_tag not in tags:
                return {"success": False, "error": f"Study tag not found: {study_tag}",
                        "model_tag": model_name, "candidate_study_tags": tags,
                        "write_attempted": False}
            return async_solver.start_solve(java.study(study_tag), model_name, study_tag)
        except Exception as exc:
            return {"success": False, "model_tag": model_name, "study_tag": study_tag,
                    "write_attempted": False, "error": str(exc),
                    "error_type": type(exc).__name__}

    @mcp.tool()
    def study_get_progress(run_id: str) -> dict:
        """Return this run's queued/running/completed/failed state and exact errors.

        Progress percentages are unavailable and returned as null. A completed
        state means study.run() returned; technical acceptance uses the task's
        result checks. Runs belong to this MCP process and remain identified
        after a later study starts.
        """
        try:
            return {"success": True, "run": async_solver.get_progress(run_id)}
        except Exception as exc:
            return {"success": False, "run_id": run_id, "error": str(exc)}

    @mcp.tool()
    def study_cancel(run_id: str) -> dict:
        """Request cancellation for a run and report actual cancellation support.

        This COMSOL connection currently has no confirmed per-run cancellation
        API. It returns cancellation_unsupported with submitted_to_comsol=false;
        the run keeps its real status. COMSOL Desktop can stop the computation.
        """
        try:
            return async_solver.cancel(run_id)
        except Exception as exc:
            return {"success": False, "run_id": run_id, "error": str(exc)}

    @mcp.tool()
    def study_wait(run_id: str, timeout: float = 30) -> dict:
        """Wait 0–60 seconds for this run; timeout leaves computation running.

        finished reports terminal state, and run.status distinguishes completed
        from failed. Reuse the same run_id for subsequent status/wait calls.
        """
        try:
            return async_solver.wait(run_id, timeout)
        except Exception as exc:
            return {"success": False, "run_id": run_id, "error": str(exc)}
