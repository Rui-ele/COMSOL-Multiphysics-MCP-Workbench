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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
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
    torch_library = (
        Path(sys.prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "torch"
        / "lib"
    )
    if torch_library.exists():
        library_parts.insert(0, str(torch_library))
    existing = env.get("DYLD_LIBRARY_PATH")
    if existing:
        library_parts.append(existing)
    if library_parts:
        env["DYLD_LIBRARY_PATH"] = ":".join(dict.fromkeys(library_parts))

    if data_dir is not None:
        env["COMSOL_MCP_DATA_DIR"] = str(data_dir)
        env["COMSOL_MCP_DB_DIR"] = str(data_dir / "knowledge_base")
        env["COMSOL_MCP_REPORT_DIR"] = str(data_dir / "simulation_reports")
        env["HF_HOME"] = str(data_dir / "huggingface")
    env.setdefault("HF_ENDPOINT", "https://huggingface.co")
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
        f"- observe 拒绝后 Client A 读值：`{values.get('after_observe_denial')}`",
        f"- write 修改后 Client A 读值：`{values.get('after_write')}`",
        f"- detach 后 Client A 读值：`{values.get('after_detach')}`",
        f"- MCP 断开后 Client A 读值：`{values.get('after_mcp_disconnect')}`",
        f"- 禁止保存文件是否出现：{clean.get('forbidden_file_exists')}",
        f"- 模型是否始终未保存：{clean.get('model_never_saved')}",
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
            "- 未连接默认 2036 端口，未扫描现有用户 Server，未保存或删除模型。",
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
    """Verify the journal proves both denials and the successful write."""
    expected = {
        ("param_set", False, "model_write_access_required"),
        ("param_set", True, None),
        ("model_save", False, "external_model_lifecycle_protected"),
        ("model_remove", False, "external_model_lifecycle_protected"),
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


async def run_mcp_flow(
    *,
    port: int,
    model_tag: str,
    client_a: ClientAController,
    data_dir: Path,
    forbidden_path: Path,
    timeout: float,
    report: dict[str, Any],
) -> None:
    """Run Client B through the public MCP stdio transport."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = comsol_environment(data_dir=data_dir)
    params = StdioServerParameters(
        command=str(DEFAULT_PYTHON),
        args=["-m", "src.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()

            connected = await call_mcp(
                session,
                "comsol_connect",
                {"port": port, "host": "localhost"},
                timeout,
            )
            assert_result("connect", connected, success=True)
            if connected.get("session_mode") != "shared-external":
                raise AcceptanceFailure(
                    "connect",
                    "MCP did not enter shared-external mode.",
                    connected,
                )
            stage_record(
                report,
                "MCP 连接临时外部 Server",
                "Client B 通过 MCP SDK/STDIO 成功连接。",
                result=connected,
            )

            # Re-read the model from Client A after Client B has connected.
            # This both proves A remained attached across the second connection
            # and refreshes COMSOL's per-client model-use bookkeeping before B
            # asks modelsUsedByOtherClients().
            try:
                owner_state = await client_a.state()
            except Exception as exc:
                raise AcceptanceFailure(
                    "client_a_ownership",
                    f"Client A could not re-read the model: {exc}",
                ) from exc
            if (
                not owner_state.get("exists")
                or owner_state.get("value") != "1"
                or owner_state.get("file") is not None
            ):
                raise AcceptanceFailure(
                    "client_a_ownership",
                    "Client A did not retain the unsaved model after B connected.",
                    owner_state,
                )
            stage_record(
                report,
                "Client A 持有状态复核",
                "Client B 连接后，A 重新取得同 tag 且参数仍为 1。",
                client_a=owner_state,
            )

            discovered = await call_mcp(
                session,
                "model_discover",
                {},
                timeout,
            )
            assert_result("discover", discovered, success=True)
            candidates = [
                item
                for item in discovered.get("models", [])
                if item.get("tag") == model_tag
            ]
            if len(candidates) != 1:
                raise AcceptanceFailure(
                    "discover",
                    "MCP did not discover exactly one Client A model.",
                    discovered,
                )
            candidate = candidates[0]
            if (
                candidate.get("registered") is not False
                or candidate.get("used_by_other_clients") is not True
                or candidate.get("file") is not None
            ):
                raise AcceptanceFailure(
                    "discover",
                    "Discovered model metadata did not match external unsaved state.",
                    candidate,
                )
            report["discovery"] = candidate
            stage_record(
                report,
                "发现 Client A 模型",
                "按稳定 tag 找到未保存且由其他客户端持有的模型。",
                model=candidate,
            )

            attached = await call_mcp(
                session,
                "model_attach",
                {"model_tag": model_tag},
                timeout,
            )
            assert_result("attach", attached, success=True)
            attached_model = attached.get("model", {})
            if (
                attached_model.get("origin") != "external_attached"
                or attached_model.get("access_mode") != "observe"
                or attached_model.get("server_managed") is not False
            ):
                raise AcceptanceFailure(
                    "attach",
                    "Attached model ownership/access metadata is incorrect.",
                    attached,
                )
            stage_record(
                report,
                "observe 模式接管",
                "外部模型默认以 observe、非 MCP 管理状态登记。",
                result=attached,
            )

            denied = await call_mcp(
                session,
                "param_set",
                {
                    "name": "handoff_value",
                    "value": "42",
                    "model_name": model_tag,
                },
                timeout,
            )
            assert_result(
                "observe_denial",
                denied,
                success=False,
                error_code="model_write_access_required",
            )
            after_denial = await client_a.state()
            if after_denial.get("value") != "1":
                raise AcceptanceFailure(
                    "observe_denial",
                    "Observe-mode denial still changed Client A's parameter.",
                    after_denial,
                )
            report["values"]["after_observe_denial"] = after_denial["value"]
            stage_record(
                report,
                "observe 写入拦截",
                "param_set 在工具正文前被拒绝，Client A 仍读到 1。",
                denial=denied,
                client_a=after_denial,
            )

            access = await call_mcp(
                session,
                "model_access_set",
                {"model_name": model_tag, "access_mode": "write"},
                timeout,
            )
            assert_result("write_access", access, success=True)
            if access.get("access_mode") != "write":
                raise AcceptanceFailure(
                    "write_access",
                    "MCP did not grant write access.",
                    access,
                )

            changed = await call_mcp(
                session,
                "param_set",
                {
                    "name": "handoff_value",
                    "value": "42",
                    "model_name": model_tag,
                },
                timeout,
            )
            assert_result("write_parameter", changed, success=True)
            after_write = await client_a.state()
            if after_write.get("value") != "42":
                raise AcceptanceFailure(
                    "write_parameter",
                    "Client A did not observe MCP's parameter update.",
                    after_write,
                )
            report["values"]["after_write"] = after_write["value"]
            stage_record(
                report,
                "write 模式跨客户端修改",
                "MCP 将参数改为 42，Client A 从同一模型读到 42。",
                access=access,
                result=changed,
                client_a=after_write,
            )

            save_denied = await call_mcp(
                session,
                "model_save",
                {
                    "model_name": model_tag,
                    "file_path": str(forbidden_path),
                },
                timeout,
            )
            assert_result(
                "save_protection",
                save_denied,
                success=False,
                error_code="external_model_lifecycle_protected",
            )
            remove_denied = await call_mcp(
                session,
                "model_remove",
                {"model_name": model_tag},
                timeout,
            )
            assert_result(
                "remove_protection",
                remove_denied,
                success=False,
                error_code="external_model_lifecycle_protected",
            )
            protected_state = await client_a.state()
            if (
                forbidden_path.exists()
                or not protected_state.get("exists")
                or protected_state.get("file") is not None
            ):
                raise AcceptanceFailure(
                    "lifecycle_protection",
                    "External save/remove protection changed model lifecycle.",
                    {
                        "file_exists": forbidden_path.exists(),
                        "client_a": protected_state,
                    },
                )
            report["forbidden_file_exists"] = forbidden_path.exists()
            stage_record(
                report,
                "外部模型生命周期保护",
                "保存和删除均被拒绝；模型仍在内存且没有生成文件。",
                save=save_denied,
                remove=remove_denied,
                client_a=protected_state,
            )

            detached = await call_mcp(
                session,
                "model_detach",
                {"model_name": model_tag},
                timeout,
            )
            assert_result("detach", detached, success=True)
            if detached.get("server_model_preserved") is not True:
                raise AcceptanceFailure(
                    "detach",
                    "Detach did not report server model preservation.",
                    detached,
                )
            after_detach = await client_a.state()
            if (
                not after_detach.get("exists")
                or after_detach.get("value") != "42"
                or after_detach.get("file") is not None
            ):
                raise AcceptanceFailure(
                    "detach",
                    "Client A model was not preserved after MCP detach.",
                    after_detach,
                )
            report["values"]["after_detach"] = after_detach["value"]
            stage_record(
                report,
                "MCP detach",
                "MCP 仅清除登记，Client A 模型及参数 42 均保留。",
                result=detached,
                client_a=after_detach,
            )

            disconnected = await call_mcp(
                session,
                "comsol_disconnect",
                {},
                timeout,
            )
            assert_result("disconnect", disconnected, success=True)
            after_disconnect = await client_a.state()
            if (
                not after_disconnect.get("exists")
                or after_disconnect.get("value") != "42"
                or after_disconnect.get("file") is not None
            ):
                raise AcceptanceFailure(
                    "disconnect",
                    "External Server model disappeared after MCP disconnect.",
                    after_disconnect,
                )
            report["values"]["after_mcp_disconnect"] = after_disconnect["value"]
            report["model"]["final_file"] = after_disconnect.get("file")
            report["model_never_saved"] = after_disconnect.get("file") is None
            stage_record(
                report,
                "MCP 断开外部 Server",
                "仅断开 Client B；Client A 继续访问模型和临时 Server。",
                result=disconnected,
                client_a=after_disconnect,
            )


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
            "after_observe_denial": None,
            "after_write": None,
            "after_detach": None,
            "after_mcp_disconnect": None,
        },
        "forbidden_file_exists": None,
        "model_never_saved": False,
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
        forbidden_path = temp_dir / "must-not-exist.mph"
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
                "Codex External Handoff "
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
                forbidden_path=forbidden_path,
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
                "日志包含 observe 拒绝、write 成功及保存/删除拒绝。",
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
            if forbidden_path.exists():
                report["forbidden_file_exists"] = True
                report["success"] = False
                report["failed_stage"] = report["failed_stage"] or "cleanup"
                report["error"] = report["error"] or "Forbidden MPH file was created."

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
        default=PROJECT_ROOT / ".comsol-mcp-data" / "integration_reports",
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
