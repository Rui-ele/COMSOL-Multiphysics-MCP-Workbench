"""Track asynchronous study runs without inventing COMSOL progress."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Optional
from uuid import uuid4

from .execution import MODEL_OPERATION_LOCK


class SolverStatus(Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SolverProgress:
    run_id: Optional[str] = None
    model_tag: Optional[str] = None
    study_tag: Optional[str] = None
    status: SolverStatus = SolverStatus.IDLE
    submitted_at: Optional[datetime] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    write_attempted: bool = False
    cancellation_attempts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "run_id": self.run_id,
            "model_tag": self.model_tag,
            "study_tag": self.study_tag,
            "status": self.status.value,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "elapsed_seconds": ((self.end_time or now) - self.start_time).total_seconds()
            if self.start_time else None,
            "progress": None,
            "progress_source": "COMSOL study.run() exposes no progress callback here.",
            "write_attempted": self.write_attempted,
            "error": self.error,
            "error_type": self.error_type,
            "cancellation_attempts": [dict(item) for item in self.cancellation_attempts],
            "completion_basis": "study.run() returned" if self.status == SolverStatus.COMPLETED else None,
        }


class AsyncSolver:
    """Keep run identity and factual state independently of the COMSOL lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, SolverProgress] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._current_run_id: Optional[str] = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return any(
                run.status in (SolverStatus.QUEUED, SolverStatus.RUNNING)
                for run in self._runs.values()
            )

    def start_solve(self, study, model_tag: str, study_tag: str) -> dict:
        """Reserve one run and invoke the exact study under the shared lock."""
        with self._lock:
            for run in self._runs.values():
                if run.status in (SolverStatus.QUEUED, SolverStatus.RUNNING):
                    return {
                        "success": False,
                        "error_code": "comsol_busy",
                        "error": "A study run is already active.",
                        "run": run.to_dict(),
                    }
            run_id = uuid4().hex
            run = SolverProgress(
                run_id=run_id,
                model_tag=model_tag,
                study_tag=study_tag,
                status=SolverStatus.QUEUED,
                submitted_at=datetime.now(timezone.utc),
            )
            self._runs[run_id] = run
            self._current_run_id = run_id

        def solve_thread():
            # All model tools share this lock. Status and cancellation records
            # use _lock only, so polling remains available throughout the run.
            with MODEL_OPERATION_LOCK:
                with self._lock:
                    run.status = SolverStatus.RUNNING
                    run.start_time = datetime.now(timezone.utc)
                    run.write_attempted = True
                try:
                    study.run()
                except Exception as exc:
                    with self._lock:
                        run.status = SolverStatus.FAILED
                        run.error = str(exc)
                        run.error_type = type(exc).__name__
                        run.end_time = datetime.now(timezone.utc)
                else:
                    with self._lock:
                        run.status = SolverStatus.COMPLETED
                        run.end_time = datetime.now(timezone.utc)

        thread = threading.Thread(target=solve_thread, name=f"comsol-{run_id}", daemon=True)
        with self._lock:
            self._threads[run_id] = thread
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                run.status = SolverStatus.FAILED
                run.error = str(exc)
                run.error_type = type(exc).__name__
                run.end_time = datetime.now(timezone.utc)
            return {"success": False, "error": str(exc), "run": self.get_progress(run_id)}
        return {"success": True, "accepted": True, "run": self.get_progress(run_id)}

    def get_progress(self, run_id: Optional[str] = None) -> dict:
        """Snapshot a specified run; the omitted ID is for the internal busy guard."""
        with self._lock:
            target = run_id if run_id is not None else self._current_run_id
            if target is None:
                return SolverProgress().to_dict()
            if target not in self._runs:
                raise ValueError(f"Unknown run_id: {target}")
            return self._runs[target].to_dict()

    def cancel(self, run_id: str) -> dict:
        """Report cancellation support without pretending a local flag stops COMSOL."""
        with self._lock:
            if run_id not in self._runs:
                raise ValueError(f"Unknown run_id: {run_id}")
            run = self._runs[run_id]
            if run.status not in (SolverStatus.QUEUED, SolverStatus.RUNNING):
                return {
                    "success": False,
                    "error_code": "run_not_active",
                    "error": "The requested run is no longer active.",
                    "run": run.to_dict(),
                }
            # The public COMSOL 6.3 ModelUtil / Study / SolverSequence APIs used
            # by this connection expose no confirmed per-run stop operation.
            # Do not disconnect, kill COMSOL, or substitute a Python flag.
            attempt = {
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "supported": False,
                "submitted_to_comsol": False,
                "confirmed_stopped": False,
            }
            run.cancellation_attempts.append(attempt)
            return {
                "success": False,
                "error_code": "cancellation_unsupported",
                "error": "This connection has no supported per-run cancellation API. Stop the computation in COMSOL Desktop and query this run again.",
                "cancellation": dict(attempt),
                "run": run.to_dict(),
            }

    def wait(self, run_id: str, timeout: float = 30) -> dict:
        """Wait at most 60 seconds for the specified run, retaining its identity."""
        if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds.")
        with self._lock:
            if run_id not in self._runs:
                raise ValueError(f"Unknown run_id: {run_id}")
            thread = self._threads.get(run_id)
        if thread is not None and thread.ident is not None:
            thread.join(timeout=timeout)
        progress = self.get_progress(run_id)
        finished = progress["status"] in ("completed", "failed")
        return {"success": True, "finished": finished, "timed_out": not finished, "run": progress}


async_solver = AsyncSolver()
