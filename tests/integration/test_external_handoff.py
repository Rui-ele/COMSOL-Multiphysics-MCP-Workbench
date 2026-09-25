"""Opt-in real COMSOL integration acceptance test."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name("handoff_runner.py")


@pytest.mark.integration
def test_real_two_client_external_model_handoff(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--timeout",
            "180",
            "--report-dir",
            str(tmp_path),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )

    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
    )
    summary = json.loads(completed.stdout)
    report = json.loads(
        Path(summary["json_report"]).read_text(encoding="utf-8")
    )

    assert summary["success"] is True
    expected_version = os.environ.get("COMSOL_MCP_COMSOL_VERSION", "6.4")
    assert report["comsol_version"].startswith(expected_version)
    assert report["server_cores"] == 1
    assert report["server_port"] != 2036
    assert report["model"]["file"] is None
    assert report["model"]["save_location_after_copy"] is None
    assert report["model"]["final_exists"] is False
    assert report["model_removed"] is True
    assert report["saved_file"]["size_bytes"] > 0
    assert report["values"] == {
        "initial": "1",
        "after_write": "42",
        "after_detach": "42",
        "after_mcp_disconnect": "42",
    }
    assert report["audit"]["required_events"] == 3
    assert "handoff_value" in report["handoff_report"]
    assert "42" in report["handoff_report"]
    assert report["cleanup"] == {
        "server_running_after_mcp_disconnect": True,
        "client_a_stopped": True,
        "server_stopped": True,
    }
