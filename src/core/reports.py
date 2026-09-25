"""Complete factual reports, value serialization, and frozen text pagination."""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from .runtime_info import runtime_info
from .execution import ACTIVE_MODEL

REPORT_VERSION = 3
MAX_REPORT_CHARS = 60000
REPORT_PAGE_CHARS = 12000
MAX_PAGE_CHARS = 24000
MAX_CACHED_REPORTS = 16
_REPORTS: OrderedDict[str, dict] = OrderedDict()
_REPORT_LOCK = RLock()


def serialize_value(value: Any) -> Any:
    """Convert a COMSOL/MPh value to JSON data, retaining every item and character."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, complex):
        return {"real": str(value.real), "imaginary": str(value.imag)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value
    if type(value).__name__ in {"java.lang.String", "java.lang.Character"}:
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): serialize_value(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return serialize_value(value.model_dump())
    if getattr(value, "shape", None) == () and hasattr(value, "item"):
        return serialize_value(value.item())
    if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        return [serialize_value(value[index]) for index in range(len(value))]
    return str(value)


def fact(source: str, value: Any) -> dict:
    converted = serialize_value(value)
    empty = converted is None or converted == "" or converted == [] or converted == {}
    return {"status": "empty" if empty else "success", "source": source, "value": converted}


def unavailable(source: str, status: str, reason: str) -> dict:
    return {"status": status, "source": source, "reason": str(reason)}


def read(source: str, getter: Callable[[], Any]) -> dict:
    try:
        return fact(source, getter())
    except (AttributeError, NotImplementedError) as exc:
        return unavailable(source, "unsupported", str(exc))
    except Exception as exc:
        return unavailable(source, "error", f"{type(exc).__name__}: {exc}")


def _report_delivery(report_id: str, snapshot: dict, offset: int, size: int) -> dict:
    end = min(offset + size, len(snapshot["text"]))
    return {
        "report_id": report_id,
        "offset": offset,
        "returned_chars": end - offset,
        "total_chars": len(snapshot["text"]),
        "next_offset": end if end < len(snapshot["text"]) else None,
        "complete": end == len(snapshot["text"]),
        "sha256": snapshot["sha256"],
        "content_type": "text/markdown; charset=utf-8",
        "source": "Frozen report text collected by this MCP process",
        "cache_lifetime": f"Retained among the {MAX_CACHED_REPORTS} most recently used reports; expires when this MCP process restarts.",
        "continuation_tool": "diagnostic_report_page",
    }


def paginate_report(packet: dict, report_markdown: str) -> dict:
    """Keep the complete report and transport large reports as immutable text pages.

    Writers and readers can share this helper. A continuation only reads the
    cached text, so it never repeats a COMSOL operation or takes a new reading.
    Concatenate report_markdown fragments in offset order to obtain the report.
    """
    report_id = uuid4().hex
    snapshot = {
        "text": report_markdown,
        "sha256": hashlib.sha256(report_markdown.encode("utf-8")).hexdigest(),
        "report_kind": packet.get("report_kind"),
        "collected_at": packet.get("collected_at", packet.get("finished_at")),
    }
    with _REPORT_LOCK:
        _REPORTS[report_id] = snapshot
        while len(_REPORTS) > MAX_CACHED_REPORTS:
            _REPORTS.popitem(last=False)
    # Count both structured facts and their readable rendition, as clients can
    # receive both fields in one tool response.
    full_size = len(report_markdown) + len(json.dumps(packet, ensure_ascii=False))
    if full_size <= MAX_REPORT_CHARS:
        result = dict(packet)
        size = len(report_markdown)
    else:
        result = {
            key: packet[key]
            for key in ("success", "status", "report_kind", "report_version", "collected_at", "started_at", "finished_at", "collection_complete", "incomplete_field_count", "summary", "write_attempted", "write_returned", "operation_invoked", "stage", "model_tag", "model_reference", "source_model_tag", "clone_tag", "run_id", "error_code")
            if key in packet
        }
        if isinstance(packet.get("run"), dict):
            result["run"] = {key: packet["run"][key] for key in (
                "run_id", "model_tag", "study_tag", "status", "progress",
            ) if key in packet["run"]}
        result["delivery_instruction"] = "Append report_markdown pages in offset order using diagnostic_report_page until report_delivery.next_offset is null. All collected facts are retained in that report."
        size = REPORT_PAGE_CHARS
    result["report_markdown"] = report_markdown[:size]
    result["report_delivery"] = _report_delivery(report_id, snapshot, 0, size)
    return result


def report_page(report_id: str, offset: int = 0, max_chars: int = REPORT_PAGE_CHARS) -> dict:
    """Return a page of the existing report without contacting COMSOL."""
    if not isinstance(report_id, str) or not report_id:
        return {"success": False, "status": "error", "error": "report_id must be a report_delivery.report_id returned by a collection or API tool."}
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return {"success": False, "status": "error", "error": "offset must be a nonnegative integer."}
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= MAX_PAGE_CHARS:
        return {"success": False, "status": "error", "error": f"max_chars must be between 1 and {MAX_PAGE_CHARS}."}
    with _REPORT_LOCK:
        snapshot = _REPORTS.get(report_id)
        if snapshot is not None:
            _REPORTS.move_to_end(report_id)
    if snapshot is None:
        return {
            "success": False, "status": "expired", "report_id": report_id,
            "error": "This report is no longer cached. The MCP process may have restarted or newer reports may have replaced it.",
            "next_step": "Retain the pages already received. For a reading task, collect a new report and assemble only pages from that report_id. For a modification task, request a fresh read of the affected objects to establish current state.",
        }
    if offset > len(snapshot["text"]):
        return {"success": False, "status": "error", "report_id": report_id, "error": "offset exceeds total_chars.", "total_chars": len(snapshot["text"])}
    return {
        "success": True, "status": "report_page", "report_kind": snapshot["report_kind"],
        "collected_at": snapshot["collected_at"],
        "report_markdown": snapshot["text"][offset:offset + max_chars],
        "report_delivery": _report_delivery(report_id, snapshot, offset, max_chars),
    }


def new_report_packet(tool: str, request: dict, *, success: bool = False,
                      status: str = "rejected") -> dict:
    """Common envelope for operation, API, and diagnostic facts."""
    try:
        runtime = runtime_info()
    except Exception as exc:
        runtime = {"source_status": "unavailable", "source_error": str(exc)}
    active = ACTIVE_MODEL.get()
    reference = request.get("model_name", request.get("model_tag"))
    model_tag = active[2].tag if active is not None and reference in (active[1], active[2].tag) else None
    return {
        "report_version": REPORT_VERSION,
        "report_kind": tool,
        "success": success,
        "status": status,
        "model_reference": reference,
        "model_tag": model_tag,
        "request": serialize_value(request),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "errors": [],
    }


def _render_report(packet: dict) -> str:
    body = json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False)
    fence = "```"
    while fence in body:
        fence += "`"
    return (
        f"# COMSOL 调用报告：{packet['report_kind']}\n\n"
        f"## 简短摘要\n\n{packet['summary']}\n\n"
        f"## 完整事实\n\n{fence}json\n{body}\n{fence}\n"
    )


def finish_report(packet: dict, summary: str) -> dict:
    """Render and paginate once, retaining completed effects on delivery errors."""
    packet["report_version"] = REPORT_VERSION
    packet["finished_at"] = datetime.now(timezone.utc).isoformat()
    packet["summary"] = summary
    model = packet.get("model")
    model = model if isinstance(model, dict) else {}
    run = packet.get("run")
    run = run if isinstance(run, dict) else {}
    packet["model_tag"] = packet.get("model_tag") or model.get("model_tag", model.get("tag")) or run.get("model_tag")
    errors = packet.setdefault("errors", [])
    for key in ("error", "state_error", "readback_error", "save_location_error"):
        if packet.get(key) and str(packet[key]) not in errors:
            errors.append(str(packet[key]))
    packet = serialize_value(packet)
    report = _render_report(packet)
    try:
        return paginate_report(packet, report)
    except Exception as exc:
        packet["report_warning"] = f"Report pagination failed: {type(exc).__name__}: {exc}"
        packet["report_markdown"] = _render_report(packet)
        return packet


def finish_diagnostic_packet(packet: dict) -> dict:
    """Include all collected facts in one copyable text block, without inference."""
    issues = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if "source" in value and value.get("status") in {"error", "unsupported", "partial", "truncated", "omitted"}:
                issues.append({"field": path, "status": value["status"]})
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(packet.get("findings", {}), "findings")
    walk(packet.get("model", {}), "model")
    packet["incomplete_fields"] = issues
    packet["incomplete_field_count"] = len(issues)
    packet["incomplete_field_index_truncated"] = False
    packet["collection_complete"] = not issues and packet.get("success", False)
    if packet.get("success") and issues:
        packet["status"] = "partial"
    request = packet.get("request", {})
    scope = request.get("node_path") or packet.get("model", {}).get("model_tag", "请求对象")
    overview = packet.get("report_kind") == "diagnostic_collect"
    summary = {
        "completed": f"已采集 {scope} 的{'模型概况' if overview else '请求范围内的事实'}。" if packet.get("success") else "本次采集失败。",
        "findings": f"读取结果包含 {len(packet.get('findings', {}))} 项，明细与来源见完整事实。",
        "unfinished": (f"{len(issues)} 处读取失败、接口不支持或概况范围尚未覆盖，见 incomplete_fields。"
                       if issues else "请求范围内的读取已完成。" if packet.get("success") else "请求范围内的读取未完成。"),
    }
    return finish_report(packet, "\n".join(f"- {text}" for text in summary.values()))


def finish_api_packet(packet: dict) -> dict:
    summaries = {
        "read": "已读取请求的 COMSOL API 数据。",
        "verified": "已执行本次修改，全部回读与任务要求一致。",
        "rejected": "本次操作未执行，原因见完整事实。",
        "needs_review": "已尝试本次修改，执行或核验出现异常，实际状态见完整事实。",
        "error": "本次读取未完成，错误原文见完整事实。",
    }
    summary = summaries[packet["status"]]
    if packet.get("before_complete") is False:
        summary += " 部分修改前取证未取得，具体缺项与错误保留在 before 中。"
    return finish_report(packet, summary)


def tool_report(tool: str, request: dict, result: dict) -> dict:
    """Attach complete facts; report pages already carry their original envelope."""
    if "report_markdown" in result or tool == "diagnostic_report_page":
        return result
    success = result.get("success", True)
    status = result.get("status", "returned" if success else "error")
    packet = new_report_packet(tool, request, success=success, status=status)
    packet.update(result)
    return finish_report(packet, f"{tool}：{status}。具体动作、读取值和错误见完整事实。")
