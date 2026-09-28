#!/usr/bin/env python3
"""Check a checkout through the MCP SDK; supervise timeouts outside its process.

The controller uses only the standard library. The selected Python runs a worker
and the target checkout's MCP server. Neither an editable reinstall nor a copy of
the client's JSON-RPC implementation is needed to compare old and new checkouts.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from importlib import metadata
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import traceback
from uuid import uuid4


SCRIPT = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT.parents[1]
PACKAGES = ("mcp", "anyio", "mph", "JPype1", "pydantic", "numpy")
ENVIRONMENT_KEYS = (
    "PATH", "JAVA_HOME", "COMSOL_MCP_COMSOL_ROOT", "COMSOL_MCP_COMSOL_VERSION",
    "COMSOL_MCP_DATA_DIR", "PYTHONPATH", "PYTHONPYCACHEPREFIX",
    "DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH",
)


def absolute_executable(value: str) -> str:
    # Resolving a venv's symlink changes which environment Python starts in.
    return os.path.abspath(os.path.expanduser(value))


def positive_seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a positive, finite number.")
    return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=json_value, allow_nan=False)


class ProbeFailure(RuntimeError):
    pass


class Events:
    def __init__(self, path: Path):
        self.path = path

    def emit(self, stage: str, status: str, **details) -> None:
        event = {"stage": stage, "status": status, "time": utc_now(), **details}
        with self.path.open("a", encoding="utf-8") as output:
            output.write(dump(event) + "\n")
            output.flush()

    async def run(self, stage: str, timeout: float, operation, *, request=None,
                  validate=None):
        self.emit(stage, "running", timeout_seconds=timeout, request=request)
        started = time.monotonic()
        response = None
        received = False
        try:
            response = await asyncio.wait_for(operation(), timeout=timeout)
            received = True
            if validate is not None:
                validate(response)
        except BaseException as exc:
            self.emit(stage, "failed", duration_seconds=time.monotonic() - started,
                      response_received=received, response=response,
                      error=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc())
            raise
        self.emit(stage, "passed", duration_seconds=time.monotonic() - started,
                  response_received=True, response=response)
        return response


def run_command(command: list[str], timeout: float = 5) -> dict:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        return {"command": command, "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"command": command, "error": f"{type(exc).__name__}: {exc}"}


def runtime_snapshot(repo: Path) -> dict:
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    spec = importlib.util.find_spec("src")
    origin = str(Path(spec.origin).resolve()) if spec and spec.origin else None
    return {
        "python": sys.executable, "python_version": platform.python_version(),
        "platform": platform.platform(), "architecture": platform.machine(),
        "packages": versions, "cwd": str(Path.cwd()), "source_file": origin,
        "expected_source_file": str(repo / "src" / "__init__.py"),
        "git_revision": run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]),
        "git_status": run_command(["git", "-C", str(repo), "status", "--short"]),
        "environment": {key: os.environ.get(key) for key in ENVIRONMENT_KEYS},
    }


def discover_installation() -> dict:
    """Run MPh's actual detector with its reasons for accepting/rejecting paths."""
    executable_search = run_command(
        ["where.exe", "comsol"] if os.name == "nt" else ["which", "comsol"])
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, force=True)
    logging.getLogger("mph").setLevel(logging.DEBUG)
    from mph import discovery

    result = {"executable_search": executable_search,
              "backends": discovery.find_backends()}
    # The stage validator records this empty list before marking the stage failed.
    return result


def payload(response: dict) -> dict:
    if response.get("isError"):
        raise ProbeFailure("MCP returned isError=true; see the full response.")
    value = response.get("structuredContent")
    if isinstance(value, dict):
        return value
    for block in response.get("content", []):
        if block.get("type") == "text":
            try:
                value = json.loads(block["text"])
            except (ValueError, KeyError):
                continue
            if isinstance(value, dict):
                return value
    raise ProbeFailure("Tool response contains no JSON object.")


def check_status(response: dict, connected: bool) -> None:
    data = payload(response)
    if data.get("success") is False:
        raise ProbeFailure(str(data.get("error", "Status tool reported success=false.")))
    if data.get("connected") is not connected:
        raise ProbeFailure(f"Expected connected={connected}; received {data!r}")


def check_success(response: dict) -> None:
    data = payload(response)
    if data.get("success") is not True:
        raise ProbeFailure(str(data.get("error", "Tool did not report success=true.")))


def check_discovery(response: dict) -> None:
    if not response["backends"]:
        raise ProbeFailure("MPh found no usable COMSOL installation. See discovery DEBUG logs.")


async def probe_mcp(config: dict, events: Events) -> None:
    from contextlib import AsyncExitStack
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    timeout = config["timeout"]
    params = StdioServerParameters(command=sys.executable,
                                   args=["-u", str(SCRIPT), "--_server", config["configuration_file"]],
                                   cwd=config["repo"], env=dict(os.environ))
    with Path(config["server_stderr"]).open("w", encoding="utf-8") as server_log:
        started = time.monotonic()
        operation_error = None
        try:
            async with AsyncExitStack() as stack:
                try:
                    events.emit("mcp_transport", "running", timeout_seconds=timeout,
                                request={"command": params.command, "args": params.args, "cwd": params.cwd,
                                         "target_module": "src.server"})
                    # AnyIO contexts must enter and exit in this same task.
                    # The supervisor bounds startup even if it blocks here.
                    try:
                        reader, writer = await stack.enter_async_context(stdio_client(params, errlog=server_log))
                        session = await stack.enter_async_context(ClientSession(
                            reader, writer, read_timeout_seconds=timedelta(seconds=timeout)))
                    except BaseException as exc:
                        events.emit("mcp_transport", "failed", error=f"{type(exc).__name__}: {exc}",
                                    traceback=traceback.format_exc())
                        raise
                    events.emit("mcp_transport", "passed")
                    await events.run("initialize", timeout, session.initialize)

                    def check_tools(result):
                        if "comsol_status" not in {tool.name for tool in result.tools}:
                            raise ProbeFailure("The server did not advertise comsol_status.")

                    await events.run("tools_list", timeout, session.list_tools, validate=check_tools)

                    async def call(stage, name, arguments, limit, validate):
                        async def request():
                            result = await session.call_tool(
                                name, arguments, read_timeout_seconds=timedelta(seconds=limit))
                            return result.model_dump(mode="json")
                        return await events.run(stage, limit, request,
                                                request={"tool": name, "arguments": arguments},
                                                validate=validate)

                    await call("status_before_connect", "comsol_status", {}, timeout,
                               lambda result: check_status(result, False))
                    if config["mode"] == "connect":
                        await events.run("comsol_discovery", config["connect_timeout"],
                                         lambda: asyncio.to_thread(discover_installation),
                                         validate=check_discovery)
                        await call("comsol_connect", "comsol_connect",
                                   {"host": config["host"], "port": config["port"]},
                                   config["connect_timeout"], check_success)
                        await call("status_after_connect", "comsol_status", {}, timeout,
                                   lambda result: check_status(result, True))
                        await call("model_discover", "model_discover", {}, timeout, check_success)
                        await call("comsol_disconnect", "comsol_disconnect", {}, timeout, check_success)
                except BaseException as exc:
                    operation_error = exc
                finally:
                    started = time.monotonic()
                    events.emit("mcp_shutdown", "running", timeout_seconds=15)
        except BaseException:
            # Preserve the first failed operation; shutdown errors are also useful.
            events.emit("mcp_shutdown", "failed", duration_seconds=time.monotonic() - started,
                        error=traceback.format_exc())
            raise
        else:
            events.emit("mcp_shutdown", "passed", duration_seconds=time.monotonic() - started)
        if operation_error is not None:
            raise operation_error


def worker(config: dict) -> int:
    repo = Path(config["repo"])
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    events = Events(Path(config["events"]))
    events.emit("environment", "running", timeout_seconds=config["timeout"])
    try:
        snapshot = runtime_snapshot(repo)
        source_ok = bool(snapshot["source_file"]) and Path(snapshot["source_file"]).samefile(
            snapshot["expected_source_file"])
        events.emit("environment", "passed" if source_ok else "failed", response=snapshot,
                    error=None if source_ok else "Python did not select the requested checkout's src package.")
        if not source_ok:
            return 1
        if config["mode"] == "discovery":
            asyncio.run(events.run("comsol_discovery", config["connect_timeout"],
                                   lambda: asyncio.to_thread(discover_installation),
                                   validate=check_discovery))
        else:
            asyncio.run(probe_mcp(config, events))
    except BaseException as exc:
        events.emit("worker", "failed", error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc())
        return 1
    events.emit("worker", "passed")
    return 0


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            # A worker may currently be writing the final line.
            continue
    return result


def stop_worker_tree(process: subprocess.Popen, server_pid_file: Path | None = None) -> dict:
    """Stop only the worker process tree created by this invocation."""
    if os.name == "nt":
        result = run_command(["taskkill", "/PID", str(process.pid), "/T", "/F"], timeout=10)
    else:
        result = {"process_group": process.pid}
        # The SDK starts its server in a separate POSIX session. Record that
        # owned process at its entry point so forced cleanup can reach it too.
        if server_pid_file and server_pid_file.exists():
            try:
                owned = json.loads(server_pid_file.read_text(encoding="utf-8"))
                server_pid = owned["pid"]
                if owned["pgid"] == server_pid and os.getpgid(server_pid) == server_pid:
                    os.killpg(server_pid, signal.SIGKILL)
                    result["server_process_group_terminated"] = server_pid
            except ProcessLookupError:
                result["server_already_exited"] = True
            except (OSError, ValueError, KeyError) as exc:
                result["server_cleanup_error"] = str(exc)
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            # A descendant could remain even if the group leader has exited.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            result["error"] = str(exc)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        result["error"] = "Worker remained alive after process-tree termination."
    result["worker_exit_code"] = process.poll()
    return result


def supervise(command: list[str], *, config: dict, env: dict, directory: Path) -> dict:
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    started = time.monotonic()
    failure = None
    cleanup = None
    with (directory / "worker.stdout.log").open("w", encoding="utf-8") as out, \
            (directory / "worker.stderr.log").open("w", encoding="utf-8") as err:
        process = subprocess.Popen(command, cwd=config["repo"], env=env,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err, **options)
        last_count = 0
        active = "worker_startup"
        deadline = started + config["timeout"] + 5
        total_limit = 8 * config["timeout"] + 2 * config["connect_timeout"] + 60
        try:
            while process.poll() is None:
                events = read_events(Path(config["events"]))
                for event in events[last_count:]:
                    active = event["stage"]
                    if event["status"] == "running":
                        deadline = time.monotonic() + event["timeout_seconds"] + 3
                    else:
                        deadline = time.monotonic() + 15
                    if event["status"] != "running":
                        print(f"{event['stage']}: {event['status']}", flush=True)
                last_count = len(events)
                if time.monotonic() > deadline or time.monotonic() - started > total_limit:
                    failure = {"stage": active, "status": "timeout",
                               "error": "Supervisor deadline exceeded; terminating this test's worker tree."}
                    cleanup = stop_worker_tree(process, Path(config["server_pid"]) if config.get("server_pid") else None)
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            failure = {"stage": active, "status": "interrupted", "error": "Interrupted by user."}
            cleanup = stop_worker_tree(process, Path(config["server_pid"]) if config.get("server_pid") else None)
        except BaseException:
            stop_worker_tree(process, Path(config["server_pid"]) if config.get("server_pid") else None)
            raise
    events = read_events(Path(config["events"]))
    for event in events[last_count:]:
        if event["status"] != "running":
            print(f"{event['stage']}: {event['status']}", flush=True)
    failed = [event for event in events if event["status"] == "failed"]
    success = (process.returncode == 0 and not failed and failure is None
               and any(e["stage"] == "worker" and e["status"] == "passed" for e in events))
    unexpected_exit = None
    if not success and not failed and failure is None:
        unexpected_exit = {"stage": events[-1]["stage"] if events else "worker_startup",
                           "status": "failed", "error": f"Worker exited without completing the check (exit code {process.returncode})."}
    return {"success": success, "duration_seconds": round(time.monotonic() - started, 3),
            "worker_exit_code": process.returncode, "events": events,
            "first_failure": failed[0] if failed else failure or unexpected_exit,
            "supervisor_failure": failure, "forced_cleanup": cleanup}


def build_environment(config: dict) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["COMSOL_MCP_DATA_DIR"] = str(Path(config["report_directory"]) / "mcp-data")
    root = config.get("comsol_root")
    if root:
        system = platform.system()
        arch = ("win64" if system == "Windows" else
                "macarm64" if system == "Darwin" and platform.machine() == "arm64" else
                "maci64" if system == "Darwin" else "glnxa64")
        env["COMSOL_MCP_COMSOL_ROOT"] = root
        env["PATH"] = str(Path(root) / "bin" / arch) + os.pathsep + env.get("PATH", "")
    return env


def write_report(report: dict, directory: Path) -> None:
    (directory / "report.json").write_text(dump(report) + "\n", encoding="utf-8")
    lines = ["# COMSOL 连接自检", "", f"- 结果：{'通过' if report['success'] else '未通过'}",
             f"- 模式：{report['configuration']['mode']}",
             f"- 被测目录：{report['configuration']['repo']}",
             f"- 首个失败阶段：{(report.get('first_failure') or {}).get('stage', '无')}",
             "", "## 阶段结果", "", "| 阶段 | 结果 |", "| --- | --- |"]
    for event in report.get("events", []):
        if event["status"] != "running":
            lines.append(f"| {event['stage']} | {event['status']} |")
    if report.get("supervisor_failure"):
        lines.append(f"| {report['supervisor_failure']['stage']} | supervisor timeout / interruption |")
    lines.extend(["", "## 完整记录", ""])
    evidence = json.dumps(report, ensure_ascii=False, default=json_value, indent=2)
    fence = "```"
    while fence in evidence:
        fence += "`"
    lines.extend([fence + "json", evidence, fence])
    for filename in ("worker.stderr.log", "server.stderr.log", "worker.stdout.log"):
        path = directory / filename
        if path.exists():
            log = path.read_text(encoding="utf-8", errors="replace")
            fence = "```"
            while fence in log:
                fence += "`"
            lines.extend(["", f"## {filename}", "", fence + "text", log, fence])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--mode", choices=("mcp", "discovery", "connect"), default="mcp")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=2036)
    parser.add_argument("--comsol-root", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--timeout", type=positive_seconds, default=30)
    parser.add_argument("--connect-timeout", type=positive_seconds, default=90)
    args = parser.parse_args(argv)
    repo = args.repo.expanduser().resolve()
    python = absolute_executable(args.python)
    if not (repo / "src" / "server.py").is_file():
        parser.error("--repo must contain src/server.py")
    if not Path(python).is_file():
        parser.error("--python must be an existing Python executable path")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    base = (args.report_dir or repo / ".comsol-mcp-data" / "connection-check").expanduser().resolve()
    directory = base / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + args.mode + "_" + uuid4().hex[:8])
    directory.mkdir(parents=True)
    config = {
        "repo": str(repo), "python": python, "mode": args.mode,
        "host": args.host, "port": args.port, "timeout": args.timeout,
        "connect_timeout": args.connect_timeout,
        "comsol_root": str(args.comsol_root.expanduser().resolve()) if args.comsol_root else None,
        "report_directory": str(directory), "events": str(directory / "events.jsonl"),
        "server_stderr": str(directory / "server.stderr.log"),
        "server_pid": str(directory / "server-process.json"),
        "configuration_file": str(directory / "configuration.json"),
    }
    config_path = directory / "configuration.json"
    config_path.write_text(dump(config), encoding="utf-8")
    print(f"Report directory: {directory}", flush=True)
    report = {"schema_version": 1, "started_at": utc_now(), "configuration": config,
              "checker_source": str(SCRIPT), "success": False}
    try:
        report.update(supervise([python, "-u", str(SCRIPT), "--_worker", str(config_path)],
                                config=config, env=build_environment(config), directory=directory))
    except Exception as exc:
        report["first_failure"] = {"stage": "controller", "error": f"{type(exc).__name__}: {exc}"}
    report["finished_at"] = utc_now()
    write_report(report, directory)
    print(f"Result: {'PASS' if report['success'] else 'FAIL'}")
    print(f"Copyable report: {directory / 'report.md'}")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--_worker"]:
        raise SystemExit(worker(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))))
    if sys.argv[1:2] == ["--_server"]:
        import runpy
        configuration = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        Path(configuration["server_pid"]).write_text(
            dump({"pid": os.getpid(), "pgid": os.getpgrp() if os.name != "nt" else None}),
            encoding="utf-8")
        os.chdir(configuration["repo"])
        sys.path.insert(0, configuration["repo"])
        runpy.run_module("src.server", run_name="__main__")
        raise SystemExit(0)
    raise SystemExit(main())
