"""Exercise the shipped checker over real stdio, without COMSOL or a license."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts.check_connection import ProbeFailure, build_environment, check_status, supervise, write_report


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_connection.py"


def test_checkout(tmp_path, status_body):
    repo = tmp_path / "checkout with spaces"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "server.py").write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('connection-check-fixture')\n"
        "@mcp.tool()\n"
        "def comsol_status() -> dict:\n"
        + "\n".join("    " + line for line in status_body.splitlines())
        + "\nif __name__ == '__main__':\n    mcp.run()\n",
        encoding="utf-8",
    )
    return repo


# This helper constructs a fixture, not a pytest test.
test_checkout.__test__ = False


def run_check(tmp_path, repo, timeout=10):
    out = tmp_path / "reports"
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--python", sys.executable,
         "--mode", "mcp", "--report-dir", str(out), "--timeout", str(timeout)],
        capture_output=True, text=True, timeout=35,
    )
    report_path = next(out.glob("*/report.json"))
    return process, json.loads(report_path.read_text(encoding="utf-8")), report_path.parent


def tool_data(response):
    return response.get("structuredContent") or json.loads(response["content"][0]["text"])


def test_real_sdk_uses_requested_checkout_and_accepts_disconnected_status(tmp_path):
    repo = test_checkout(tmp_path, "return {'connected': False, 'marker': 'old-checkout'}")
    process, report, directory = run_check(tmp_path, repo)
    assert process.returncode == 0, (process.stdout, process.stderr, report)
    assert report["success"] is True
    events = {e["stage"]: e for e in report["events"] if e["status"] == "passed"}
    assert events["environment"]["response"]["source_file"] == str(repo / "src" / "__init__.py")
    assert "initialize" in events and "tools_list" in events
    assert tool_data(events["status_before_connect"]["response"])["marker"] == "old-checkout"
    assert "mcp_shutdown" in events
    assert (directory / "report.md").is_file()


def test_business_failure_preserves_response_and_is_not_a_protocol_pass(tmp_path):
    repo = test_checkout(tmp_path, "return {'success': False, 'error': 'fixture failure'}")
    process, report, directory = run_check(tmp_path, repo)
    assert process.returncode == 1
    failure = report["first_failure"]
    assert failure["stage"] == "status_before_connect"
    assert failure["response_received"] is True
    assert tool_data(failure["response"])["error"] == "fixture failure"
    assert any(e["stage"] == "mcp_shutdown" and e["status"] == "passed" for e in report["events"])
    assert "fixture failure" in (directory / "report.md").read_text(encoding="utf-8")


def test_status_error_cannot_pass_merely_because_it_is_disconnected():
    with pytest.raises(ProbeFailure, match="fixture failure"):
        check_status({"structuredContent": {"success": False, "connected": False,
                                             "error": "fixture failure"}}, False)


def test_synchronous_server_hang_returns_bounded_failure(tmp_path):
    repo = test_checkout(tmp_path, "import time\ntime.sleep(60)\nreturn {'connected': False}")
    started = time.monotonic()
    process, report, _ = run_check(tmp_path, repo, timeout=3)
    assert time.monotonic() - started < 25
    assert process.returncode == 1
    assert report["first_failure"]["stage"] == "status_before_connect"


def test_supervisor_stops_a_worker_that_cannot_write_progress(tmp_path):
    config = {"repo": str(tmp_path), "events": str(tmp_path / "events.jsonl"),
              "timeout": 0.1, "connect_timeout": 0.1}
    started = time.monotonic()
    report = supervise([sys.executable, "-c", "import time; time.sleep(60)"],
                       config=config, env=dict(os.environ), directory=tmp_path)
    assert time.monotonic() - started < 15
    assert report["success"] is False
    assert report["supervisor_failure"]["stage"] == "worker_startup"
    assert report["forced_cleanup"]["worker_exit_code"] is not None


def test_worker_crash_still_identifies_a_failed_stage(tmp_path):
    config = {"repo": str(tmp_path), "events": str(tmp_path / "events.jsonl"),
              "timeout": 1, "connect_timeout": 1}
    report = supervise([sys.executable, "-c", "import os; os._exit(7)"],
                       config=config, env=dict(os.environ), directory=tmp_path)
    assert report["success"] is False
    assert report["first_failure"]["stage"] == "worker_startup"
    assert "7" in report["first_failure"]["error"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX SDK servers use a separate session")
def test_forced_cleanup_includes_separate_server_process_group(tmp_path):
    pid_file = tmp_path / "server-process.json"
    config = {"repo": str(tmp_path), "events": str(tmp_path / "events.jsonl"),
              "server_pid": str(pid_file), "timeout": 0.1, "connect_timeout": 0.1}
    code = (
        "import json, pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid': child.pid, 'pgid': child.pid})); "
        "time.sleep(60)"
    )
    report = supervise([sys.executable, "-c", code, str(pid_file)],
                       config=config, env=dict(os.environ), directory=tmp_path)
    child_pid = json.loads(pid_file.read_text())["pid"]
    assert report["forced_cleanup"]["server_process_group_terminated"] == child_pid
    assert report["forced_cleanup"]["worker_exit_code"] is not None


def test_comsol_path_override_is_process_local(tmp_path):
    before = dict(os.environ)
    root = tmp_path / "COMSOL with spaces"
    env = build_environment({"comsol_root": str(root), "report_directory": str(tmp_path)})
    assert env["PATH"].startswith(str(root / "bin"))
    assert env["COMSOL_MCP_COMSOL_ROOT"] == str(root)
    assert env["COMSOL_MCP_DATA_DIR"] == str(tmp_path / "mcp-data")
    assert dict(os.environ) == before


def test_copyable_report_handles_paths_and_nested_markdown(tmp_path):
    report = {"success": False, "configuration": {"mode": "mcp", "repo": str(tmp_path)},
              "events": [{"stage": "call", "status": "failed", "response": {"value": "```"}}]}
    (tmp_path / "server.stderr.log").write_text("诊断输出\n```\n", encoding="utf-8")
    write_report(report, tmp_path)
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == report
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "诊断输出" in text
    assert "````json" in text
