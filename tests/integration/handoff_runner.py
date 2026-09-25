#!/usr/bin/env python3
"""Real two-client acceptance test for external COMSOL model handoff.

This script starts a fresh, temporary multi-client COMSOL Server. Client A
creates one unsaved in-memory model. Client B is the project's MCP server,
connected through the MCP SDK over stdio. No user server or model is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.doctor import find_comsol_root, find_java_home, platform_architecture


def executable_path(executable: str) -> Path:
    """Return an absolute executable path without resolving venv symlinks."""
    return Path(os.path.abspath(os.path.expanduser(executable)))


DEFAULT_PYTHON = executable_path(sys.executable)
COMSOL_VERSION = os.environ.get("COMSOL_MCP_COMSOL_VERSION", "6.4")


class AcceptanceFailure(RuntimeError):
    """A failed acceptance stage with structured context."""

    def __init__(self, stage: str, message: str, details: Any = None):
        self.stage = stage
        self.details = details
        super().__init__(message)


def find_acceptance_failure(exception: BaseException) -> AcceptanceFailure | None:
    """Find the original stage failure inside AnyIO/TaskGroup exception groups."""
    if isinstance(exception, AcceptanceFailure):
        return exception
    for nested in getattr(exception, "exceptions", ()):
        found = find_acceptance_failure(nested)
        if found is not None:
            return found
    return None


def exception_summary(exception: BaseException) -> list[str]:
    """Flatten nested task-group exceptions into readable diagnostic lines."""
    lines = [f"{type(exception).__name__}: {exception}"]
    for nested in getattr(exception, "exceptions", ()):
        lines.extend(exception_summary(nested))
    return lines


def comsol_environment(
    base: dict[str, str] | None = None,
    *,
    data_dir: Path | None = None,
) -> dict[str, str]:
    """Build the local COMSOL environment without overwriting user choices."""
    env = dict(base or os.environ)
    root = find_comsol_root(COMSOL_VERSION)
    java_home = find_java_home(root)
    if java_home is not None:
        env.setdefault("JAVA_HOME", str(java_home))

    library_parts: list[str] = []
    if root is not None:
        arch = platform_architecture()
        library_parts.extend(
            str(path)
            for path in (
                root / "lib" / arch,
                root / "ext" / "graphicsmagick" / arch,
                root / "ext" / "dnn" / arch,
            )
            if path.is_dir()
        )
    existing = env.get("DYLD_LIBRARY_PATH")
    if existing:
        library_parts.append(existing)
    if library_parts:
        env["DYLD_LIBRARY_PATH"] = ":".join(dict.fromkeys(library_parts))

    if data_dir is not None:
        env["COMSOL_MCP_DATA_DIR"] = str(data_dir)
    return env


def available_port() -> int:
    """Choose a currently unused loopback port, never the normal 2036 port."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != 2036:
            return port
    raise RuntimeError("Could not allocate a temporary COMSOL Server port.")


def tool_payload(result: Any) -> dict[str, Any]:
    """Extract one JSON object from an MCP SDK CallToolResult."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError(f"MCP tool returned no JSON object: {result!r}")


def assert_result(
    stage: str,
    payload: dict[str, Any],
    *,
    success: bool,
    error_code: str | None = None,
) -> None:
    """Validate a tool result and raise a structured acceptance failure."""
    if bool(payload.get("success")) is not success:
        raise AcceptanceFailure(
            stage,
            f"Expected success={success}, got {payload.get('success')!r}.",
            payload,
        )
    if error_code is not None and payload.get("error_code") != error_code:
        raise AcceptanceFailure(
            stage,
            f"Expected error_code={error_code!r}.",
            payload,
        )


def safe_report_value(value: Any) -> Any:
    """Remove credential-shaped fields before persisting acceptance evidence."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("password", "token", "secret")):
                clean[str(key)] = "<redacted>"
            else:
                clean[str(key)] = safe_report_value(item)
        return clean
    if isinstance(value, list):
        return [safe_report_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def redacted_log_tail(lines: list[str], limit: int = 20) -> list[str]:
    """Keep a short diagnostic tail while dropping credential-shaped lines."""
    output = []
    for line in lines[-limit:]:
        lowered = line.lower()
        if any(word in lowered for word in ("password", "token", "secret")):
            output.append("<redacted credential-shaped log line>")
        else:
            output.append(line[-1000:])
    return output


def write_acceptance_report(
    report: dict[str, Any],
    report_dir: Path,
) -> tuple[Path, Path]:
    """Persist JSON evidence plus a concise human-readable Markdown summary."""
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    base = report_dir / f"{stamp}_external_model_handoff"
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")

    clean = safe_report_value(report)
    json_path.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    status = "通过" if clean.get("success") else "失败"
    model = clean.get("model", {})
    values = clean.get("values", {})
    cleanup = clean.get("cleanup", {})
    stages = clean.get("stages", [])
    lines = [
        "# COMSOL 外部模型接管真实验收",
        "",
        f"- 结论：{status}",
        f"- COMSOL 版本：{clean.get('comsol_version')}",
        f"- Server 核数：{clean.get('server_cores')}",
        f"- 临时服务器：localhost:{clean.get('server_port')}",
        f"- 模型：{model.get('name')}（tag: `{model.get('tag')}`）",
        f"- 模型文件：{model.get('file') or '未保存，仅存在于服务器内存'}",
        f"- 参数初值：`{values.get('initial')}`",
        f"- 修改后 Client A 读值：`{values.get('after_write')}`",
        f"- detach 后 Client A 读值：`{values.get('after_detach')}`",
        f"- MCP 断开后 Client A 读值：`{values.get('after_mcp_disconnect')}`",
        f"- 保存副本：{clean.get('saved_file')}",
        f"- 临时模型是否已明确删除：{clean.get('model_removed')}",
        f"- MCP 断开后 Server 是否仍运行：{cleanup.get('server_running_after_mcp_disconnect')}",
        f"- Client A 是否已退出：{cleanup.get('client_a_stopped')}",
        f"- 临时 Server 是否已停止：{cleanup.get('server_stopped')}",
        f"- 总耗时：{clean.get('duration_seconds')} 秒",
        "",
        "## 验收阶段",
        "",
    ]
    for stage in stages:
        lines.append(
            f"- {'✅' if stage.get('success') else '❌'} "
            f"{stage.get('name')}：{stage.get('detail', '')}"
        )
    if clean.get("error"):
        lines.extend(
            [
                "",
                "## 失败信息",
                "",
                f"- 阶段：{clean.get('failed_stage')}",
                f"- 错误：{clean.get('error')}",
            ]
        )
    lines.extend(
        [
            "",
            "## 边界说明",
            "",
            "- 本记录来自真实 COMSOL API 与 MCP SDK 双客户端链路，不是 mock。",
            "- 使用非 2036 临时 Server；保存和删除仅针对本脚本创建的临时模型。",
            "- COMSOL Desktop 人工观察清单未在本自动化验收中执行。",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path.resolve(), markdown_path.resolve()


def worker_state(client: Any, tag: str) -> dict[str, Any]:
    """Read model state afresh from Client A's server connection."""
    tags = [str(item) for item in client.java.tags()]
    exists = tag in tags
    if not exists:
        return {"exists": False, "tag": tag, "value": None, "file": None}

    import mph

    model = mph.Model(client.java.model(tag))
    raw_path = str(model.java.getFilePath())
    return {
        "exists": True,
        "tag": tag,
        "name": str(model.name()),
        "value": str(model.parameter("handoff_value")),
        "file": raw_path or None,
    }


def emit_worker(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def run_client_a_worker(port: int, model_name: str) -> int:
    """Hold Client A open and answer narrow state/shutdown commands."""
    os.environ.update(comsol_environment())
    client = None
    try:
        import mph
        from jpype import JClass

        client = mph.Client(port=port, host="localhost")
        busy_handler = JClass("com.comsol.model.util.ServerBusyHandler")(120_000)
        client.java.setServerBusyHandler(busy_handler)
        # Use an explicit unique tag through COMSOL's Java API so this test
        # exercises ModelUtil's documented client-ownership semantics directly
        # instead of relying on MPh's createUnique convenience call.
        unique_tag = f"handoff_{uuid.uuid4().hex[:12]}"
        model = mph.Model(client.java.create(unique_tag))
        model.rename(model_name)
        model.parameter("handoff_value", "1")
        tag = str(model.java.tag())
        emit_worker(
            {
                "kind": "ready",
                "version": str(client.version),
                "cores": int(client.cores),
                "port": port,
                "model": worker_state(client, tag),
            }
        )

        for line in sys.stdin:
            try:
                request = json.loads(line)
                command = request.get("command")
                if command == "state":
                    emit_worker(
                        {
                            "kind": "state",
                            "request_id": request.get("request_id"),
                            "model": worker_state(client, tag),
                        }
                    )
                elif command == "shutdown":
                    client.disconnect()
                    client = None
                    emit_worker(
                        {
                            "kind": "shutdown",
                            "request_id": request.get("request_id"),
                            "disconnected": True,
                        }
                    )
                    return 0
                else:
                    emit_worker(
                        {
                            "kind": "error",
                            "request_id": request.get("request_id"),
                            "error": f"Unsupported worker command: {command!r}",
                        }
                    )
            except Exception as exc:
                emit_worker({"kind": "error", "error": str(exc)})
        return 0
    except Exception as exc:
        emit_worker({"kind": "fatal", "error": str(exc)})
        return 1
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass


class ClientAController:
    """Async JSON-line controller for the independent Client A process."""

    def __init__(self, process: asyncio.subprocess.Process, timeout: float):
        self.process = process
        self.timeout = timeout
        self._request_number = 0
        self._stderr_tail: list[str] = []
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Prevent a verbose COMSOL child from blocking on a full stderr pipe."""
        if self.process.stderr is None:
            return
        while True:
            raw = await self.process.stderr.readline()
            if not raw:
                return
            self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
            self._stderr_tail = self._stderr_tail[-40:]

    async def receive(self, expected_kind: str) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("Client A stdout is unavailable.")
        while True:
            raw = await asyncio.wait_for(
                self.process.stdout.readline(),
                timeout=self.timeout,
            )
            if not raw:
                stderr = "\n".join(self._stderr_tail)
                raise RuntimeError(
                    f"Client A exited before {expected_kind!r}: {stderr[-2000:]}"
                )
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if payload.get("kind") == "fatal":
                raise RuntimeError(f"Client A failed: {payload.get('error')}")
            if payload.get("kind") == "error":
                raise RuntimeError(f"Client A command failed: {payload.get('error')}")
            if payload.get("kind") == expected_kind:
                return payload

    async def command(self, command: str, expected_kind: str) -> dict[str, Any]:
        if self.process.stdin is None:
            raise RuntimeError("Client A stdin is unavailable.")
        self._request_number += 1
        request = {
            "command": command,
            "request_id": self._request_number,
        }
        self.process.stdin.write(
            (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await self.process.stdin.drain()
        return await self.receive(expected_kind)

    async def state(self) -> dict[str, Any]:
        return (await self.command("state", "state"))["model"]

    @property
    def stderr_tail(self) -> list[str]:
        return redacted_log_tail(self._stderr_tail)

    async def shutdown(self) -> None:
        if self.process.returncode is not None:
            return
        try:
            await self.command("shutdown", "shutdown")
            await asyncio.wait_for(self.process.wait(), timeout=15)
        except Exception:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        try:
            await asyncio.wait_for(self._stderr_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._stderr_task.cancel()


def stage_record(
    report: dict[str, Any],
    name: str,
    detail: str,
    **evidence: Any,
) -> None:
    report["stages"].append(
        {
            "name": name,
            "success": True,
            "detail": detail,
            "evidence": safe_report_value(evidence),
        }
    )


async def call_mcp(
    session: Any,
    name: str,
    arguments: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    result = await session.call_tool(
        name,
        arguments,
        read_timeout_seconds=timedelta(seconds=timeout),
    )
    return tool_payload(result)


def read_audit_records(data_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((data_dir / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def verify_audit(
    records: list[dict[str, Any]],
    *,
    model_tag: str,
) -> dict[str, Any]:
    """Verify the journal records the explicit write, save, and removal."""
    expected = {
        ("comsol_api_write", True, None),
        ("model_save", True, None),
        ("model_remove", True, None),
    }
    found: set[tuple[str, bool, str | None]] = set()
    for record in records:
        payload = record.get("result")
        error_code = payload.get("error_code") if isinstance(payload, dict) else None
        key = (record.get("tool"), bool(record.get("success")), error_code)
        arguments = record.get("arguments")
        same_model = (
            isinstance(arguments, dict)
            and arguments.get("model_name") == model_tag
        )
        if key in expected and same_model:
            found.add(key)
    missing = expected - found
    if missing:
        raise AcceptanceFailure(
            "audit",
            f"Audit journal is missing required events: {sorted(missing)!r}",
            records,
        )
    return {
        "record_count": len(records),
        "required_events": len(found),
        "model_tag": model_tag,
    }


async def complete_tool_report(session: Any, first: dict[str, Any], timeout: float) -> str:
    """Read the frozen continuation of one tool report without repeating actions."""
    text = first.get("report_markdown")
    if not isinstance(text, str):
        raise AcceptanceFailure("report", "Tool returned no copyable report.", first)
    parts = [text]
    delivery = first.get("report_delivery", {})
    report_id = delivery.get("report_id")
    seen_offsets = set()
    while delivery.get("next_offset") is not None:
        offset = delivery["next_offset"]
        if offset in seen_offsets:
            raise AcceptanceFailure("report", "Report pagination repeated an offset.", delivery)
        seen_offsets.add(offset)
        page = await call_mcp(
            session, "diagnostic_report_page", {"report_id": report_id, "offset": offset}, timeout,
        )
        assert_result("report_page", page, success=True)
        delivery = page.get("report_delivery", {})
        if delivery.get("report_id") != report_id:
            raise AcceptanceFailure("report", "Report identity changed during pagination.", page)
        parts.append(page["report_markdown"])
    return "".join(parts)


async def run_mcp_flow(
    *,
    port: int,
    model_tag: str,
    client_a: ClientAController,
    data_dir: Path,
    save_path: Path,
    timeout: float,
    report: dict[str, Any],
) -> None:
    """Execute the explicit task through MCP and independently read Client A."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(DEFAULT_PYTHON), args=["-m", "src.server"],
        cwd=str(PROJECT_ROOT), env=comsol_environment(data_dir=data_dir),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            connected = await call_mcp(
                session, "comsol_connect", {"port": port, "host": "localhost"}, timeout,
            )
            assert_result("connect", connected, success=True)
            if connected.get("session_mode") != "shared-external":
                raise AcceptanceFailure("connect", "Expected shared-external connection.", connected)
            owner_state = await client_a.state()
            if not owner_state.get("exists") or owner_state.get("value") != "1":
                raise AcceptanceFailure("client_a_ownership", "Client A did not retain its model.", owner_state)
            stage_record(report, "MCP 连接临时 Server", "两个客户端连接同一临时 Server。",
                         result=connected, client_a=owner_state)

            discovered = await call_mcp(session, "model_discover", {}, timeout)
            assert_result("discover", discovered, success=True)
            candidates = [item for item in discovered.get("models", []) if item.get("tag") == model_tag]
            if len(candidates) != 1 or candidates[0].get("registered") is not False:
                raise AcceptanceFailure("discover", "Expected one unregistered Client A model.", discovered)
            report["discovery"] = candidates[0]
            attached = await call_mcp(session, "model_attach", {"model_tag": model_tag}, timeout)
            assert_result("attach", attached, success=True)
            metadata = attached.get("model", {})
            if metadata.get("origin") != "external_attached":
                raise AcceptanceFailure("attach", "Unexpected attached model origin.", attached)
            stage_record(report, "按实际 tag 接管", "登记 Client A 的既有模型。", result=attached)

            steps = [{"method": "param", "args": []}]
            check = {"steps": steps, "method": "get", "args": ["handoff_value"]}
            changed = await call_mcp(
                session, "comsol_api_write",
                {
                    "model_name": model_tag, "steps": steps, "method": "set",
                    "args": ["handoff_value", "42"],
                    "preconditions": [{**check, "expect": {"operator": "equals", "value": "1"}}],
                    "before": [check],
                    "verify": [{**check, "expect": {"operator": "equals", "value": "42"}}],
                },
                timeout,
            )
            assert_result("write_parameter", changed, success=True)
            if changed.get("status") != "verified":
                raise AcceptanceFailure("write_parameter", "API write did not verify.", changed)
            report["handoff_report"] = await complete_tool_report(session, changed, timeout)
            after_write = await client_a.state()
            if after_write.get("value") != "42":
                raise AcceptanceFailure("write_parameter", "Client A did not read the expected value.", after_write)
            report["values"]["after_write"] = after_write["value"]
            stage_record(report, "执行与回读", "参数由 1 改为 42，工具验证与 Client A 回读一致。",
                         result=changed, client_a=after_write)

            saved = await call_mcp(
                session, "model_save",
                {"model_name": model_tag, "file_path": str(save_path), "save_copy": True},
                timeout,
            )
            assert_result("save", saved, success=True)
            if not save_path.is_file() or save_path.stat().st_size == 0:
                raise AcceptanceFailure("save", "Explicit save produced no nonempty file.", saved)
            report["saved_file"] = {"path": str(save_path), "size_bytes": save_path.stat().st_size}
            stage_record(report, "显式保存副本", "在临时目录保存 MPH 副本。", result=saved)

            detached = await call_mcp(session, "model_detach", {"model_name": model_tag}, timeout)
            assert_result("detach", detached, success=True)
            after_detach = await client_a.state()
            if not after_detach.get("exists") or after_detach.get("value") != "42":
                raise AcceptanceFailure("detach", "Detach changed the Server model.", after_detach)
            report["values"]["after_detach"] = after_detach["value"]
            stage_record(report, "移除本地登记", "Server 上的模型与参数保留。", result=detached)

            disconnected = await call_mcp(session, "comsol_disconnect", {}, timeout)
            assert_result("disconnect", disconnected, success=True)
            after_disconnect = await client_a.state()
            if not after_disconnect.get("exists") or after_disconnect.get("value") != "42":
                raise AcceptanceFailure("disconnect", "External Server model was not preserved.", after_disconnect)
            report["values"]["after_mcp_disconnect"] = after_disconnect["value"]
            report["model"]["save_location_after_copy"] = after_disconnect.get("file")
            if after_disconnect.get("file") is not None:
                raise AcceptanceFailure("save_copy", "Saving a copy changed the source save location.", after_disconnect)
            stage_record(report, "断开后模型保留", "Client A 仍取得参数 42，源模型保存位置保持为空。",
                         result=disconnected, client_a=after_disconnect)

            reconnected = await call_mcp(
                session, "comsol_connect", {"port": port, "host": "localhost"}, timeout,
            )
            assert_result("reconnect", reconnected, success=True)
            reattached = await call_mcp(session, "model_attach", {"model_tag": model_tag}, timeout)
            assert_result("reattach", reattached, success=True)
            removed = await call_mcp(session, "model_remove", {"model_name": model_tag}, timeout)
            assert_result("remove", removed, success=True)
            final_state = await client_a.state()
            if final_state.get("exists"):
                raise AcceptanceFailure("remove", "Client A still sees the explicitly removed model.", final_state)
            report["model_removed"] = True
            report["model"]["final_exists"] = False
            stage_record(report, "显式删除临时模型", "按同一 tag 删除并核验模型已不存在。",
                         result=removed, client_a=final_state)
            disconnected = await call_mcp(session, "comsol_disconnect", {}, timeout)
            assert_result("final_disconnect", disconnected, success=True)


async def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": 1,
        "success": False,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "comsol_version": None,
        "server_cores": None,
        "server_port": None,
        "model": {},
        "discovery": {},
        "values": {
            "initial": None,
            "after_write": None,
            "after_detach": None,
            "after_mcp_disconnect": None,
        },
        "saved_file": None,
        "model_removed": False,
        "audit": {},
        "stages": [],
        "cleanup": {
            "server_running_after_mcp_disconnect": False,
            "client_a_stopped": False,
            "server_stopped": False,
        },
        "failed_stage": None,
        "error": None,
        "error_details": None,
    }

    server = None
    client_a: ClientAController | None = None
    port = available_port()
    report["server_port"] = port

    with tempfile.TemporaryDirectory(prefix="comsol-mcp-handoff-") as temp_name:
        temp_dir = Path(temp_name)
        data_dir = temp_dir / "mcp-data"
        save_path = temp_dir / "explicit-copy.mph"
        os.environ.update(comsol_environment(data_dir=data_dir))

        try:
            import mph

            print(
                f"[handoff] Starting temporary COMSOL {COMSOL_VERSION} Server "
                f"on port {port}.",
                file=sys.stderr,
                flush=True,
            )
            server = mph.Server(
                cores=1,
                version=COMSOL_VERSION,
                port=port,
                multi="on",
                timeout=min(int(args.timeout), 180),
            )
            if not server.running():
                raise AcceptanceFailure(
                    "server_start",
                    "Temporary COMSOL Server did not remain running.",
                )
            stage_record(
                report,
                "启动临时 COMSOL Server",
                f"使用 {COMSOL_VERSION}、1 核和非 2036 临时端口。",
                port=port,
            )

            model_name = (
                "COMSOL External Handoff "
                + datetime.now().strftime("%Y%m%d-%H%M%S")
                + "-"
                + uuid.uuid4().hex[:6]
            )
            process = await asyncio.create_subprocess_exec(
                str(DEFAULT_PYTHON),
                str(Path(__file__).resolve()),
                "--client-a-worker",
                "--port",
                str(port),
                "--model-name",
                model_name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=comsol_environment(),
                cwd=str(PROJECT_ROOT),
            )
            client_a = ClientAController(process, timeout=args.timeout)
            ready = await client_a.receive("ready")
            model = ready["model"]
            if (
                not model.get("exists")
                or model.get("value") != "1"
                or model.get("file") is not None
            ):
                raise AcceptanceFailure(
                    "client_a_create",
                    "Client A did not create the expected unsaved model.",
                    ready,
                )
            if not str(ready.get("version", "")).startswith(COMSOL_VERSION):
                raise AcceptanceFailure(
                    "client_a_create",
                    f"Client A did not confirm COMSOL {COMSOL_VERSION}.",
                    ready,
                )
            if ready.get("cores") != 1:
                raise AcceptanceFailure(
                    "client_a_create",
                    "Temporary COMSOL Server did not use exactly one core.",
                    ready,
                )
            report["comsol_version"] = ready.get("version")
            report["server_cores"] = ready.get("cores")
            report["model"] = {
                "name": model.get("name"),
                "tag": model.get("tag"),
                "file": model.get("file"),
                "created_by": "independent_client_a",
            }
            report["values"]["initial"] = model.get("value")
            stage_record(
                report,
                "Client A 创建内存模型",
                "独立 Python 进程创建模型并设置 handoff_value=1。",
                ready=ready,
            )

            await run_mcp_flow(
                port=port,
                model_tag=str(model["tag"]),
                client_a=client_a,
                data_dir=data_dir,
                save_path=save_path,
                timeout=args.timeout,
                report=report,
            )

            server_still_running = bool(server.running())
            report["cleanup"][
                "server_running_after_mcp_disconnect"
            ] = server_still_running
            if not server_still_running:
                raise AcceptanceFailure(
                    "disconnect",
                    "Temporary Server stopped when external MCP disconnected.",
                )
            stage_record(
                report,
                "外部 Server 保持运行",
                "MCP disconnect 后 Server 对 Client A 仍可用且进程仍运行。",
                server_running=True,
            )

            audit = verify_audit(
                read_audit_records(data_dir),
                model_tag=str(model["tag"]),
            )
            report["audit"] = audit
            stage_record(
                report,
                "审计记录核对",
                "日志包含通用 API 写入、显式保存和显式删除的实际结果。",
                audit=audit,
            )
            report["success"] = True
        except AcceptanceFailure as exc:
            report["failed_stage"] = exc.stage
            report["error"] = str(exc)
            report["error_details"] = safe_report_value(exc.details)
        except Exception as exc:
            stage_failure = find_acceptance_failure(exc)
            if stage_failure is not None:
                report["failed_stage"] = stage_failure.stage
                report["error"] = str(stage_failure)
                report["error_details"] = safe_report_value(
                    stage_failure.details
                )
            else:
                report["failed_stage"] = (
                    report["stages"][-1]["name"]
                    if report["stages"]
                    else "bootstrap"
                )
                report["error"] = f"{type(exc).__name__}: {exc}"
                report["error_details"] = {
                    "exception_chain": exception_summary(exc)
                }
        finally:
            report["log_summary"] = {
                "client_a_stderr_tail": (
                    client_a.stderr_tail if client_a is not None else []
                ),
                "mcp_audit_tail": safe_report_value(
                    read_audit_records(data_dir)[-5:]
                ),
            }
            if client_a is not None:
                try:
                    await client_a.shutdown()
                except Exception as exc:
                    report["cleanup"]["client_a_error"] = str(exc)
                report["cleanup"]["client_a_stopped"] = (
                    client_a.process.returncode is not None
                )
            if server is not None:
                try:
                    server.stop()
                except Exception as exc:
                    report["cleanup"]["server_stop_error"] = str(exc)
                try:
                    report["cleanup"]["server_stopped"] = not server.running()
                except Exception as exc:
                    report["cleanup"]["server_stopped"] = False
                    report["cleanup"]["server_state_error"] = str(exc)

    if not report["cleanup"]["client_a_stopped"]:
        report["success"] = False
        report["failed_stage"] = report["failed_stage"] or "cleanup"
        report["error"] = report["error"] or "Client A did not stop."
    if not report["cleanup"]["server_stopped"]:
        report["success"] = False
        report["failed_stage"] = report["failed_stage"] or "cleanup"
        report["error"] = report["error"] or "Temporary COMSOL Server did not stop."

    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify external COMSOL model handoff through real MCP stdio."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Timeout in seconds for each client operation (default: 180).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path(os.environ.get("COMSOL_MCP_DATA_DIR", ".comsol-mcp-data")).expanduser().resolve() / "integration_reports",
        help="Directory for local JSON and Markdown acceptance records.",
    )
    parser.add_argument("--client-a-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--model-name", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.client_a_worker:
        if not args.port or not args.model_name:
            raise SystemExit("--client-a-worker requires --port and --model-name.")
        return run_client_a_worker(args.port, args.model_name)

    report = asyncio.run(run_acceptance(args))
    json_path, markdown_path = write_acceptance_report(report, args.report_dir)
    output = {
        "success": report["success"],
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
        "comsol_version": report.get("comsol_version"),
        "server_port": report.get("server_port"),
        "model": report.get("model"),
        "duration_seconds": report.get("duration_seconds"),
        "failed_stage": report.get("failed_stage"),
        "error": report.get("error"),
        "cleanup": report.get("cleanup"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
