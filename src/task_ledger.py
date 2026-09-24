"""Persistent records for model tasks requiring out-of-band approval."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


_FINAL_STATUSES = frozenset({"verified", "needs_review", "failed", "rejected"})
_SCHEMA = """
CREATE TABLE {table} (
    task_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'running', 'verified',
                   'needs_review', 'failed', 'rejected')
    ),
    preview_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    started_at TEXT,
    finished_at TEXT
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _required_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _json_object(value: dict[str, Any], name: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a dictionary")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class TaskLedger:
    """Record task intent, separate approval, execution, and outcome.

    A task can be claimed only after approval, and only once. A persisted
    ``running`` task requires
    explicit review after a restart; constructing a new ledger never replays it.
    Each operation uses its own SQLite connection so instances can be shared
    across threads and processes.
    """

    def __init__(self, data_dir: Path | str | None = None):
        base = data_dir if data_dir is not None else os.environ.get(
            "COMSOL_MCP_DATA_DIR", ".comsol-mcp-data"
        )
        self.data_dir = Path(base).expanduser().resolve()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        self.db_path = self.data_dir / "tasks.sqlite3"

        # SQLite creates its own files, but precreating the database prevents a
        # permissive process umask from exposing newly stored task details.
        try:
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)

        with self._write() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if not columns:
                connection.execute(_SCHEMA.format(table="tasks"))
            elif "confirmation_code" in columns:
                # The earlier draft exposed its confirmation code to the same
                # caller. Migrate its records without granting approval.
                connection.execute("ALTER TABLE tasks RENAME TO tasks_legacy")
                connection.execute(_SCHEMA.format(table="tasks"))
                connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, fingerprint, status, preview_json, result_json,
                        created_at, updated_at, approved_at, started_at, finished_at
                    )
                    SELECT task_id, fingerprint, status, preview_json, result_json,
                           created_at, updated_at, NULL, started_at, finished_at
                    FROM tasks_legacy
                    """
                )
                connection.execute("DROP TABLE tasks_legacy")
            elif "approved_at" not in columns:
                raise ValueError("tasks.sqlite3 has an unsupported schema")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "fingerprint": row["fingerprint"],
            "status": row["status"],
            "preview": json.loads(row["preview_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "approved_at": row["approved_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _find(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    def prepare(
        self,
        task_id: str,
        fingerprint: str,
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a pending task, or return its existing immutable identity."""
        _required_text(task_id, "task_id")
        _required_text(fingerprint, "fingerprint")
        with self._write() as connection:
            row = self._find(connection, task_id)
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("task_id already belongs to a different task")
                if json.loads(row["preview_json"]) != preview:
                    raise ValueError(
                        "task preview belongs to an earlier model connection; use a new task_id"
                    )
                return self._record(row)

            preview_json = _json_object(preview, "preview")
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO tasks (task_id, fingerprint, status, preview_json,
                                   created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?, ?)
                """,
                (task_id, fingerprint, preview_json, timestamp, timestamp),
            )
            return self._record(self._find(connection, task_id))

    def approve(self, task_id: str, fingerprint: str) -> dict[str, Any]:
        """Atomically approve the exact pending task reviewed by the operator."""
        _required_text(task_id, "task_id")
        _required_text(fingerprint, "fingerprint")
        with self._write() as connection:
            row = self._find(connection, task_id)
            if row is None:
                raise ValueError("unknown task_id")
            if row["fingerprint"] != fingerprint:
                raise ValueError("task fingerprint does not match")
            if row["status"] != "pending":
                raise ValueError(f"task cannot be approved from {row['status']}")

            timestamp = _now()
            connection.execute(
                """
                UPDATE tasks SET status = 'approved', approved_at = ?, updated_at = ?
                WHERE task_id = ? AND status = 'pending'
                """,
                (timestamp, timestamp, task_id),
            )
            return self._record(self._find(connection, task_id))

    def claim(self, task_id: str, fingerprint: str) -> dict[str, Any]:
        """Atomically claim an approved task exactly once."""
        _required_text(task_id, "task_id")
        _required_text(fingerprint, "fingerprint")
        with self._write() as connection:
            row = self._find(connection, task_id)
            if row is None:
                raise ValueError("unknown task_id")
            if row["fingerprint"] != fingerprint:
                raise ValueError("task fingerprint does not match")
            if row["status"] != "approved":
                raise ValueError(f"task cannot be claimed from {row['status']}")

            timestamp = _now()
            connection.execute(
                """
                UPDATE tasks SET status = 'running', started_at = ?, updated_at = ?
                WHERE task_id = ? AND status = 'approved'
                """,
                (timestamp, timestamp, task_id),
            )
            return self._record(self._find(connection, task_id))

    def finish(self, task_id: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
        """Store a terminal outcome; never overwrite an earlier outcome."""
        _required_text(task_id, "task_id")
        if status not in _FINAL_STATUSES:
            raise ValueError("status must be verified, needs_review, failed, or rejected")
        result_json = _json_object(result, "result")
        with self._write() as connection:
            row = self._find(connection, task_id)
            if row is None:
                raise ValueError("unknown task_id")
            if row["status"] in _FINAL_STATUSES:
                if row["status"] == status and row["result_json"] == result_json:
                    return self._record(row)
                raise ValueError("task already has a terminal outcome")
            if row["status"] != "running":
                raise ValueError("task must be running before it can finish")

            timestamp = _now()
            connection.execute(
                """
                UPDATE tasks SET status = ?, result_json = ?, finished_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (status, result_json, timestamp, timestamp, task_id),
            )
            return self._record(self._find(connection, task_id))

    def get(self, task_id: str) -> dict[str, Any] | None:
        """Read one task, including its last persisted status."""
        _required_text(task_id, "task_id")
        with closing(self._connect()) as connection:
            row = self._find(connection, task_id)
            return self._record(row) if row is not None else None
