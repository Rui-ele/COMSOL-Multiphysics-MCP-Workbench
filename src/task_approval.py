"""Interactive local approval for prepared COMSOL model tasks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .task_ledger import TaskLedger


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="comsol-task-approve",
        description="Review and approve one prepared COMSOL model task locally.",
    )
    parser.add_argument("task_id", help="Prepared task ID")
    parser.add_argument(
        "--data-dir", type=Path, help="Task data directory (defaults to COMSOL_MCP_DATA_DIR)"
    )
    args = parser.parse_args(argv)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Approval requires an interactive terminal.", file=sys.stderr)
        return 2

    try:
        ledger = TaskLedger(args.data_dir)
        record = ledger.get(args.task_id)
        if record is None:
            raise ValueError("unknown task_id")
        if record["status"] != "pending":
            raise ValueError(f"task cannot be approved from {record['status']}")

        print(f"Task ID: {record['task_id']}")
        print(f"Fingerprint: {record['fingerprint']}")
        print("Preview:")
        print(json.dumps(record["preview"], ensure_ascii=False, indent=2, sort_keys=True))
        phrase = f"APPROVE {record['task_id']} {record['fingerprint'][:12]}"
        print("To approve, type this exact phrase:")
        print(phrase)
        try:
            typed = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nApproval cancelled.", file=sys.stderr)
            return 1
        if typed != phrase:
            print("Approval cancelled: phrase did not match.", file=sys.stderr)
            return 1

        approved = ledger.approve(record["task_id"], record["fingerprint"])
    except (OSError, ValueError) as exc:
        print(f"Approval failed: {exc}", file=sys.stderr)
        return 1

    print(f"Approved task {approved['task_id']} at {approved['approved_at']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
