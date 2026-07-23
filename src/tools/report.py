"""Generate concise Chinese handoff reports for COMSOL simulations."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..access_control import READ_ONLY_MODEL_TOOLS as ACCESS_READ_ONLY_MODEL_TOOLS
from ..reporting import AuditRecorder, audit_recorder
from .session import session_manager


class PlotRecommendation(BaseModel):
    """A COMSOL Desktop result view recommended to the user."""

    plot_name: str = Field(description="COMSOL Results 下的 Plot Group 名称")
    dataset_name: Optional[str] = Field(
        default=None, description="该图应使用的 Dataset 名称"
    )
    reason: str = Field(description="为什么需要查看这张图")
    view_state: Optional[str] = Field(
        default=None, description="建议查看的频率、时间或参数步"
    )


READ_ONLY_MODEL_TOOLS = set(ACCESS_READ_ONLY_MODEL_TOOLS) | {
    "model_discover",
    "model_attach",
    "model_list",
    "physics_get_available",
}


def _safe_model_value(model, method: str, default):
    try:
        value = getattr(model, method)()
        if isinstance(value, tuple):
            return list(value)
        return value
    except Exception:
        return default


def inspect_report_model(model) -> dict[str, Any]:
    """Collect only the model metadata needed by a handoff report."""
    try:
        tag = str(model.java.tag())
    except Exception:
        tag = None
    return {
        "name": _safe_model_value(model, "name", None),
        "tag": tag,
        "file": str(_safe_model_value(model, "file", "") or ""),
        "version": _safe_model_value(model, "version", None),
        "studies": list(_safe_model_value(model, "studies", []) or []),
        "solutions": list(_safe_model_value(model, "solutions", []) or []),
        "datasets": list(_safe_model_value(model, "datasets", []) or []),
        "plots": list(_safe_model_value(model, "plots", []) or []),
        "problems": _safe_model_value(model, "problems", []) or [],
    }


def _markdown_cell(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return text.replace("|", "\\|").replace("\n", " ")


def _display_node_name(value: str) -> str:
    """Render MPh's escaped node paths the way COMSOL Desktop shows them."""
    return value.replace("//", "/")


def _human_command(record: dict[str, Any]) -> str:
    name = record["tool"]
    args = record.get("arguments", {})
    if name == "comsol_start":
        return "启动或复用 COMSOL 共享服务器"
    if name == "comsol_connect":
        return "连接外部 COMSOL Server"
    if name == "model_load":
        return f"加载模型 `{args.get('file_path', '')}`"
    if name == "model_create":
        return f"创建模型 `{args.get('name') or '未命名模型'}`"
    if name == "param_set":
        return f"设置参数 `{args.get('name')} = {args.get('value')}`"
    if name == "geometry_build":
        return f"重建几何 `{args.get('geometry_name') or '当前几何'}`"
    if name.startswith("mesh_") and name not in {"mesh_list", "mesh_info"}:
        return f"执行网格操作 `{name}`"
    if name == "study_solve":
        return f"求解 `{args.get('study_name') or '全部 Study'}`"
    if name == "study_solve_async":
        return f"后台求解 `{args.get('study_name') or '全部 Study'}`"
    if name in {"results_evaluate", "results_global_evaluate"}:
        return (
            f"在 `{args.get('dataset') or '默认 Dataset'}` 上计算 "
            f"`{args.get('expression')}`"
        )
    if name.startswith("results_export_"):
        return f"导出结果 `{args.get('node_name') or '全部导出节点'}`"
    if name.startswith("model_save"):
        return f"保存模型到 `{args.get('file_path') or '模型原路径'}`"
    return f"调用 `{name}`"


def _is_model_change(record: dict[str, Any]) -> bool:
    name = record["tool"]
    if not record.get("success") or name in READ_ONLY_MODEL_TOOLS:
        return False
    if name.startswith("model_"):
        return name not in {"model_load", "model_set_current", "model_remove"}
    return name.startswith(
        ("param_", "geometry_", "physics_", "multiphysics_", "mesh_")
    )


def _safe_filename(value: str) -> str:
    name = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    return (name or "simulation")[:80]


def _unique_report_path(report_dir: Path, model_name: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    base = report_dir / f"{stamp}_{_safe_filename(model_name)}.md"
    if not base.exists():
        return base
    counter = 2
    while True:
        candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _render_result_summary(records: list[dict[str, Any]]) -> list[str]:
    lines = []
    for record in records:
        if record["tool"] in {"study_solve", "study_solve_async"}:
            args = record.get("arguments", {})
            status = "成功" if record.get("success") else f"失败：{record.get('error')}"
            lines.append(
                f"- Study：`{args.get('study_name') or '全部 Study'}`；状态：{status}。"
            )
        elif record["tool"] in {"results_evaluate", "results_global_evaluate"}:
            args = record.get("arguments", {})
            result = record.get("result", {})
            value = result.get("value") if isinstance(result, dict) else result
            unit = args.get("unit") or (result.get("unit") if isinstance(result, dict) else None)
            unit_text = f" {unit}" if unit else ""
            lines.append(
                f"- `{args.get('expression')}` @ "
                f"`{_display_node_name(args.get('dataset')) if args.get('dataset') else '默认 Dataset'}`："
                f"`{_markdown_cell(value)}`{unit_text}。"
            )
    return lines


def render_simulation_report(
    *,
    title: str,
    summary: str,
    recommendations: list[PlotRecommendation],
    conclusions: list[str],
    status: dict[str, Any],
    model_info: dict[str, Any],
    records: list[dict[str, Any]],
    warnings: list[str],
) -> str:
    """Render a complete Chinese Markdown handoff report."""
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        f"# {title.strip()}",
        "",
        f"> 生成时间：{generated}",
        "",
        "## 仿真摘要",
        "",
        summary.strip(),
        "",
        "## 基本信息",
        "",
        f"- 模型：`{model_info.get('name') or status.get('current_model') or '未知'}`",
        f"- 模型 tag：`{model_info.get('tag') or status.get('current_model_tag') or '未知'}`",
        f"- 模型来源：`{model_info.get('origin') or '未知'}`",
        f"- MCP 访问模式：`{model_info.get('access_mode') or '未知'}`",
        f"- 模型文件：`{model_info.get('file') or '未保存模型'}`",
        (
            "- 保存状态：`已关联磁盘文件`"
            if model_info.get("file")
            else "- 保存状态：`服务器内存模型（未保存）`"
        ),
        f"- COMSOL 版本：`{status.get('version') or model_info.get('version') or '未知'}`",
        f"- 共享服务器：`{status.get('server_host') or 'localhost'}:{status.get('server_port') or '未知'}`",
        (
            "- 其他客户端正在使用："
            f"`{'是' if model_info.get('used_by_other_clients') else '否'}`"
        ),
        f"- Desktop 已导入：`{'是' if model_info.get('desktop_attached', status.get('desktop_attached')) else '否'}`",
        f"- 生命周期状态：`{'stale' if model_info.get('stale') else 'live'}`",
        f"- Study：`{', '.join(model_info.get('studies', [])) or '无'}`",
        f"- Dataset：`{', '.join(_display_node_name(item) for item in model_info.get('datasets', [])) or '无'}`",
        "",
        "## 在 COMSOL Desktop 中查看",
        "",
    ]

    if recommendations:
        lines.extend(
            [
                "| 优先级 | 结果图路径 | Dataset | 查看状态 | 查看理由 | 可用性 |",
                "|---:|---|---|---|---|---|",
            ]
        )
        available_plots = set(model_info.get("plots", []))
        available_datasets = set(model_info.get("datasets", []))
        for index, item in enumerate(recommendations, 1):
            plot_ok = item.plot_name in available_plots
            dataset_ok = not item.dataset_name or item.dataset_name in available_datasets
            availability = "✅ 可直接打开" if plot_ok and dataset_ok else "⚠️ 请核对名称"
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(index),
                        f"`Results → {item.plot_name}`",
                        f"`{_display_node_name(item.dataset_name) if item.dataset_name else '图中当前 Dataset'}`",
                        item.view_state or "默认解",
                        item.reason,
                        availability,
                    ]
                )
                + " |"
            )
    else:
        lines.append("- 本次没有提供推荐结果图；请在 `Results` 中核对可用 Plot Group。")

    lines.extend(["", "### 模型中的可用结果图", ""])
    plots = model_info.get("plots", [])
    lines.append("、".join(f"`{plot}`" for plot in plots) if plots else "无现成 Plot Group。")

    lines.extend(["", "## 求解与关键结果", ""])
    result_lines = _render_result_summary(records)
    lines.extend(result_lines or ["- 本次命令记录中没有求解或数值求值结果。"]) 

    lines.extend(["", "## 模型改动", ""])
    changes = [record for record in records if _is_model_change(record)]
    lines.extend(
        [f"- {_human_command(record)}" for record in changes]
        or ["- 本次没有记录模型结构或参数改动。"]
    )

    lines.extend(["", "## 关键结论", ""])
    lines.extend([f"- {item}" for item in conclusions] or ["- 未提供额外结论。"])

    successful_saves = [
        record
        for record in records
        if record["tool"].startswith("model_save") and record.get("success")
    ]
    lines.extend(["", "## 保存状态", ""])
    if successful_saves:
        lines.append("- 本次调用过模型保存工具，具体路径见命令附录。")
    else:
        lines.append(
            "- 本次审计记录中未调用模型保存工具；模型修改和求解结果仅保留在共享服务器内存中。"
        )

    if warnings:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {warning}" for warning in warnings)

    lines.extend(
        [
            "",
            "## COMSOL 命令附录",
            "",
            "| # | 时间 | 分类 | MCP 工具 | 关键参数 | 状态 | 耗时 |",
            "|---:|---|---|---|---|---|---:|",
        ]
    )
    for record in records:
        status_text = "成功" if record.get("success") else f"失败：{record.get('error') or '未知错误'}"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(record.get("sequence", "")),
                    record.get("timestamp", ""),
                    record.get("category", ""),
                    f"`{record.get('tool', '')}`",
                    f"`{_markdown_cell(record.get('arguments', {}))}`",
                    status_text,
                    f"{record.get('duration_ms', 0):.1f} ms",
                ]
            )
            + " |"
        )
    if not records:
        lines.append("| - | - | - | - | `{}` | 无命令记录 | 0 ms |")
    lines.append("")
    return "\n".join(lines)


def create_simulation_report(
    *,
    title: str,
    summary: str,
    recommended_plots: list[PlotRecommendation],
    conclusions: Optional[list[str]] = None,
    model_name: Optional[str] = None,
    recorder: AuditRecorder = audit_recorder,
    report_dir: Optional[Path] = None,
) -> dict[str, Any]:
    model = session_manager.get_model(model_name)
    if model is None:
        return {
            "success": False,
            "error": f"Model not found: {model_name or 'no current model'}",
        }

    status = session_manager.get_status()
    model_info = inspect_report_model(model)
    record = None
    get_record = getattr(session_manager, "get_model_record", None)
    if get_record is not None:
        try:
            record = get_record(model_name)
        except Exception:
            record = None
    if record is not None:
        model_info.update(
            record.metadata(
                is_current=record.tag == status.get("current_model_tag")
            )
        )

    tag = model_info.get("tag") or status.get("current_model_tag")
    status_model = next(
        (
            item
            for item in status.get("models", [])
            if item.get("tag") == tag
        ),
        {},
    )
    for key in (
        "origin",
        "access_mode",
        "server_managed",
        "stale",
        "stale_reason",
        "used_by_other_clients",
        "desktop_attached",
    ):
        if key not in model_info and key in status_model:
            model_info[key] = status_model[key]
    model_info["tag"] = tag
    if "used_by_other_clients" not in model_info:
        model_info["used_by_other_clients"] = bool(
            tag and tag in status.get("models_used_by_other_clients", [])
        )
    if "desktop_attached" not in model_info:
        model_info["desktop_attached"] = bool(
            model_info["used_by_other_clients"]
        )
    model_info["saved_to_disk"] = bool(model_info.get("file"))

    records = recorder.pending_records()
    warnings = []
    available_plots = set(model_info["plots"])
    available_datasets = set(model_info["datasets"])
    checks = []
    for item in recommended_plots:
        plot_exists = item.plot_name in available_plots
        dataset_exists = not item.dataset_name or item.dataset_name in available_datasets
        checks.append(
            {
                **item.model_dump(),
                "plot_exists": plot_exists,
                "dataset_exists": dataset_exists,
            }
        )
        if not plot_exists:
            warnings.append(f"模型中未找到推荐结果图：{item.plot_name}")
        if item.dataset_name and not dataset_exists:
            warnings.append(f"模型中未找到推荐 Dataset：{item.dataset_name}")

    if not recommended_plots:
        warnings.append("未提供推荐结果图。")
    if not records:
        warnings.append("当前报告游标之后没有新的 COMSOL 命令记录。")

    output_dir = report_dir
    if output_dir is None:
        configured = os.environ.get("COMSOL_MCP_REPORT_DIR")
        output_dir = Path(configured).expanduser() if configured else Path.cwd() / "simulation_reports"
    output_dir = output_dir.resolve()
    report_path = _unique_report_path(
        output_dir,
        model_info.get("name") or status.get("current_model") or "simulation",
    )
    content = render_simulation_report(
        title=title,
        summary=summary,
        recommendations=recommended_plots,
        conclusions=conclusions or [],
        status=status,
        model_info=model_info,
        records=records,
        warnings=warnings,
    )
    report_path.write_text(content, encoding="utf-8")
    command_count = recorder.advance_cursor()

    return {
        "success": True,
        "report_path": str(report_path),
        "command_count": command_count,
        "model": model_info.get("name"),
        "model_tag": model_info.get("tag"),
        "origin": model_info.get("origin"),
        "access_mode": model_info.get("access_mode"),
        "used_by_other_clients": model_info.get("used_by_other_clients", False),
        "stale": model_info.get("stale", False),
        "saved_to_disk": model_info["saved_to_disk"],
        "server_host": status.get("server_host"),
        "server_port": status.get("server_port"),
        "desktop_attached": model_info.get(
            "desktop_attached",
            status.get("desktop_attached", False),
        ),
        "plot_checks": checks,
        "warnings": warnings,
        "audit_journal": str(recorder.journal_path),
    }


def register_report_tools(mcp: FastMCP) -> None:
    """Register simulation report tools with the MCP server."""

    @mcp.tool()
    def simulation_report_create(
        title: str,
        summary: str,
        recommended_plots: list[PlotRecommendation],
        conclusions: Optional[list[str]] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a Chinese Markdown handoff report for the current simulation.

        Call this after solving and numerical checks, but before disconnecting
        COMSOL. The report records MCP commands since the previous report and
        tells the user which COMSOL Desktop plots and datasets to inspect.
        """
        return create_simulation_report(
            title=title,
            summary=summary,
            recommended_plots=recommended_plots,
            conclusions=conclusions,
            model_name=model_name,
        )
