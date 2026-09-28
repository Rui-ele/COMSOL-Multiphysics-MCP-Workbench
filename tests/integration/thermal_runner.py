#!/usr/bin/env python3
"""Real MCP acceptance: build and solve an isolated 1D heat-transfer model."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .handoff_runner import (
    AcceptanceFailure,
    COMSOL_VERSION,
    DEFAULT_PYTHON,
    PROJECT_ROOT,
    assert_result,
    available_port,
    call_mcp,
    comsol_environment,
    find_acceptance_failure,
    safe_report_value,
)


def selector(method: str, *args: str) -> dict[str, Any]:
    return {"method": method, "args": list(args)}


def tag_check(steps: list[dict], tag: str) -> dict:
    return {"steps": steps, "method": "tags", "args": [],
            "expect": {"operator": "contains", "value": tag}}


def value_check(steps: list[dict], method: str, args: list, value: Any) -> dict:
    return {"steps": steps, "method": method, "args": args,
            "expect": {"operator": "equals", "value": value}}


def record(report: dict, stage: str, request: dict, response: dict) -> None:
    result = {key: value for key, value in response.items() if key != "report_markdown"}
    report["stages"].append({"stage": stage, "request": request,
                             "result": safe_report_value(result)})


async def verified_write(session: Any, report: dict, stage: str,
                         steps: list[dict], method: str, args: list,
                         verify: list[dict], timeout: float) -> dict:
    request = {"model_name": report["model_tag"], "steps": steps,
               "method": method, "args": args, "verify": verify}
    response = await call_mcp(session, "comsol_api_write", request, timeout)
    record(report, stage, request, response)
    assert_result(stage, response, success=True)
    if response.get("status") != "verified":
        raise AcceptanceFailure(stage, "COMSOL API write was not verified.", response)
    return response


async def checked_call(session: Any, report: dict, stage: str,
                       name: str, request: dict, timeout: float) -> dict:
    response = await call_mcp(session, name, request, timeout)
    record(report, stage, {"tool": name, **request}, response)
    assert_result(stage, response, success=True)
    return response


def flatten_numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str):
        try:
            return [float(value)]
        except ValueError:
            return []
    if isinstance(value, list):
        return [number for item in value for number in flatten_numbers(item)]
    return []


async def run_model(session: Any, report: dict, timeout: float) -> None:
    tag = report["model_tag"]
    component = [selector("component", "comp1")]
    geometry = component + [selector("geom", "geom1")]
    interval = geometry + [selector("feature", "i1")]
    physics = component + [selector("physics", "ht")]
    mesh = component + [selector("mesh", "mesh1")]
    study = [selector("study", "std1")]
    results = [selector("result")]
    datasets = results + [selector("dataset")]
    numericals = results + [selector("numerical")]

    await verified_write(session, report, "component_create", [selector("component")],
                         "create", ["comp1", True],
                         [tag_check([selector("component")], "comp1")], timeout)
    await verified_write(session, report, "geometry_create", component + [selector("geom")],
                         "create", ["geom1", 1],
                         [tag_check(component + [selector("geom")], "geom1")], timeout)
    await verified_write(session, report, "interval_create", geometry,
                         "create", ["i1", "Interval"],
                         [tag_check(geometry + [selector("feature")], "i1")], timeout)
    await verified_write(session, report, "interval_endpoint", interval,
                         "setIndex", ["coord", 1.0, 1],
                         [value_check(interval, "getDouble", ["coord", 1], 1.0)], timeout)
    built = await checked_call(session, report, "geometry_build", "geometry_build",
                               {"model_name": tag, "component_tag": "comp1", "geometry_tag": "geom1"}, timeout)
    if built.get("execution_completed") is not True:
        raise AcceptanceFailure("geometry_build", "Geometry did not complete.", built)
    for method, expected in (("getNDomains", 1), ("getNBoundaries", 2),
                             ("getBoundingBox", [0.0, 1.0])):
        result = await checked_call(session, report, f"geometry_{method}", "comsol_api_read",
                                    {"model_name": tag, "steps": geometry, "method": method, "args": []}, timeout)
        if result.get("value") != expected:
            raise AcceptanceFailure(f"geometry_{method}", f"Expected {expected!r}.", result)

    await verified_write(session, report, "mesh_create", component + [selector("mesh")],
                         "create", ["mesh1"],
                         [tag_check(component + [selector("mesh")], "mesh1")], timeout)
    await verified_write(session, report, "physics_create", component + [selector("physics")],
                         "create", ["ht", "HeatTransfer", "geom1"],
                         [tag_check(component + [selector("physics")], "ht")], timeout)
    solid = physics + [selector("feature", "solid1")]
    await verified_write(session, report, "conductivity_source", solid,
                         "set", ["k_mat", "userdef"],
                         [value_check(solid, "getString", ["k_mat"], "userdef")], timeout)
    conductivity = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    conductivity_type = await checked_call(session, report, "conductivity_type", "comsol_api_read",
                                           {"model_name": tag, "steps": solid,
                                            "method": "getValueType", "args": ["k"]}, timeout)
    await verified_write(session, report, "conductivity_value", solid,
                         "set", ["k", {"type": "double[]", "value": conductivity}],
                         [value_check(solid, "getValueType", ["k"], conductivity_type["value"])], timeout)
    getter = {"StringArray": "getStringArray", "StringMatrix": "getStringMatrix",
              "DoubleArray": "getDoubleArray", "DoubleMatrix": "getDoubleMatrix"}.get(
                  conductivity_type["value"])
    if getter is None:
        raise AcceptanceFailure("conductivity_value", "Unsupported COMSOL conductivity value type.", conductivity_type)
    readback = await checked_call(session, report, "conductivity_readback", "comsol_api_read",
                                  {"model_name": tag, "steps": solid, "method": getter, "args": ["k"]}, timeout)
    # COMSOL's 1D HeatTransfer feature retains only the active x component.
    if flatten_numbers(readback.get("value")) != [1.0]:
        raise AcceptanceFailure("conductivity_readback", "1D conductivity differs from 1 W/(m·K).", readback)
    for feature_tag, boundary, temperature in (("temp1", 1, "300[K]"),
                                                ("temp2", 2, "400[K]")):
        feature = physics + [selector("feature", feature_tag)]
        await verified_write(session, report, f"{feature_tag}_create", physics,
                             "create", [feature_tag, "TemperatureBoundary", 0],
                             [tag_check(physics + [selector("feature")], feature_tag)], timeout)
        await verified_write(session, report, f"{feature_tag}_selection",
                             feature + [selector("selection")], "set",
                             [{"type": "int[]", "value": [boundary]}],
                             [value_check(feature + [selector("selection")], "entities", [], [boundary])], timeout)
        await verified_write(session, report, f"{feature_tag}_temperature", feature,
                             "set", ["T0", temperature],
                             [value_check(feature, "getString", ["T0"], temperature)], timeout)
    built = await checked_call(session, report, "mesh_build", "mesh_build",
                               {"model_name": tag, "component_tag": "comp1", "mesh_tag": "mesh1"}, timeout)
    if built.get("readback", {}).get("is_complete", {}).get("value") is not True:
        raise AcceptanceFailure("mesh_build", "COMSOL did not report a complete mesh.", built)
    await verified_write(session, report, "study_create", [selector("study")],
                         "create", ["std1"], [tag_check([selector("study")], "std1")], timeout)
    await verified_write(session, report, "stationary_create", study,
                         "create", ["stat", "Stationary"],
                         [tag_check(study + [selector("feature")], "stat")], timeout)

    solve = await checked_call(session, report, "study_solve", "study_solve",
                               {"model_name": tag, "study_tag": "std1"}, timeout)
    run_id = solve.get("run", {}).get("run_id")
    if not run_id:
        raise AcceptanceFailure("study_solve", "No run_id returned.", solve)
    report["run_id"] = run_id
    for _ in range(8):
        waited = await checked_call(session, report, "study_wait", "study_wait",
                                    {"run_id": run_id, "timeout": 30}, max(timeout, 40))
        if waited.get("finished"):
            if waited.get("run", {}).get("status") != "completed":
                raise AcceptanceFailure("study_wait", "COMSOL study failed.", waited)
            break
    else:
        raise AcceptanceFailure("study_wait", "Study did not finish within eight 30-second waits.")

    found = await checked_call(session, report, "dataset_tags", "comsol_api_read",
                               {"model_name": tag, "steps": datasets, "method": "tags", "args": []}, timeout)
    dataset_tag = None
    solution_tag = None
    for candidate in found.get("value", []):
        node = results + [selector("dataset", candidate)]
        kind = await checked_call(session, report, f"dataset_type_{candidate}", "comsol_api_read",
                                  {"model_name": tag, "steps": node, "method": "getType", "args": []}, timeout)
        if kind.get("value") == "Solution":
            source = await checked_call(session, report, f"dataset_solution_{candidate}", "comsol_api_read",
                                        {"model_name": tag, "steps": node, "method": "getString", "args": ["solution"]}, timeout)
            dataset_tag, solution_tag = candidate, source.get("value")
            break
    if not dataset_tag or not solution_tag:
        raise AcceptanceFailure("dataset_discovery", "No Solution dataset linked to a solution was found.", found)
    report["dataset_tag"] = dataset_tag
    report["solution_tag"] = solution_tag
    nonempty = await checked_call(session, report, "solution_nonempty", "comsol_api_read",
                                  {"model_name": tag, "steps": [selector("sol", solution_tag)],
                                   "method": "isEmpty", "args": []}, timeout)
    if nonempty.get("value") is not False:
        raise AcceptanceFailure("solution_nonempty", "Solution is empty.", nonempty)

    await verified_write(session, report, "cutpoint_create", datasets,
                         "create", ["cpt1", "CutPoint1D"], [tag_check(datasets, "cpt1")], timeout)
    cutpoint = results + [selector("dataset", "cpt1")]
    await verified_write(session, report, "cutpoint_source", cutpoint,
                         "set", ["data", dataset_tag],
                         [value_check(cutpoint, "getString", ["data"], dataset_tag)], timeout)
    await verified_write(session, report, "cutpoint_position", cutpoint,
                         "set", ["pointx", "0.5"],
                         [value_check(cutpoint, "getString", ["pointx"], "0.5")], timeout)
    await verified_write(session, report, "evaluation_create", numericals,
                         "create", ["pev1", "EvalPoint"], [tag_check(numericals, "pev1")], timeout)
    evaluation = results + [selector("numerical", "pev1")]
    await verified_write(session, report, "evaluation_source", evaluation,
                         "set", ["data", "cpt1"],
                         [value_check(evaluation, "getString", ["data"], "cpt1")], timeout)
    await verified_write(session, report, "evaluation_expression", evaluation,
                         "set", ["expr", {"type": "string[]", "value": ["T"]}],
                         [value_check(evaluation, "getStringArray", ["expr"], ["T"])], timeout)
    result = await checked_call(session, report, "results_evaluate", "results_evaluate",
                                {"model_name": tag, "evaluation_tag": "pev1", "method": "computeResult"}, timeout)
    values = flatten_numbers(result.get("raw_result", [None])[0])
    if len(values) != 1 or not 349.0 <= values[0] <= 351.0:
        raise AcceptanceFailure("results_evaluate", "Expected one center temperature near 350 K.", result)
    report["temperature_center_K"] = values[0]


async def run_acceptance(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    report = {"success": False, "started_at": datetime.now().astimezone().isoformat(),
              "comsol_version_requested": COMSOL_VERSION,
              "model_tag": f"thermal_{uuid.uuid4().hex[:12]}",
              "server_port": available_port(), "run_id": None,
              "dataset_tag": None, "solution_tag": None,
              "temperature_center_K": None, "stages": [], "failed_stage": None,
              "error": None, "error_details": None,
              "cleanup": {"model_removed": False, "mcp_disconnected": False,
                          "server_stopped": False}}
    server = None
    try:
        os.environ.update(comsol_environment())
        import mph
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(prefix="comsol-mcp-thermal-") as data_name:
            environment = comsol_environment(data_dir=Path(data_name))
            server = mph.Server(cores=1, version=COMSOL_VERSION,
                                port=report["server_port"], multi="on",
                                timeout=min(int(args.timeout), 180))
            if not server.running():
                raise AcceptanceFailure("server_start", "Temporary COMSOL Server did not stay running.")
            params = StdioServerParameters(command=str(DEFAULT_PYTHON),
                                           args=["-m", "src.server"],
                                           cwd=str(PROJECT_ROOT), env=environment)
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=args.timeout),
                ) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    required = {"model_create", "comsol_api_write", "comsol_api_read",
                                "geometry_build", "mesh_build", "study_solve", "study_wait",
                                "results_evaluate", "model_remove"}
                    missing = sorted(required - {tool.name for tool in listed.tools})
                    if missing:
                        raise AcceptanceFailure("list_tools", "Required tools are missing.", missing)
                    try:
                        await checked_call(session, report, "comsol_connect", "comsol_connect",
                                           {"host": "localhost", "port": report["server_port"]}, args.timeout)
                        await checked_call(session, report, "model_create", "model_create",
                                           {"model_tag": report["model_tag"], "label": "1D heat acceptance"}, args.timeout)
                        await run_model(session, report, args.timeout)
                        report["success"] = True
                    finally:
                        try:
                            removed = await call_mcp(session, "model_remove",
                                                     {"model_name": report["model_tag"]}, args.timeout)
                            report["cleanup"]["model_remove_result"] = safe_report_value(
                                {k: v for k, v in removed.items() if k != "report_markdown"})
                            report["cleanup"]["model_removed"] = (
                                removed.get("success") is True and
                                removed.get("tag_present_after") is False)
                        except Exception as exc:
                            report["cleanup"]["model_remove_error"] = str(exc)
                        try:
                            disconnected = await call_mcp(session, "comsol_disconnect", {}, args.timeout)
                            report["cleanup"]["mcp_disconnected"] = disconnected.get("success") is True
                        except Exception as exc:
                            report["cleanup"]["mcp_disconnect_error"] = str(exc)
    except Exception as exc:
        report["success"] = False
        failure = find_acceptance_failure(exc)
        report["failed_stage"] = failure.stage if failure else "runtime"
        report["error"] = str(failure or exc)
        report["error_details"] = safe_report_value(failure.details) if failure else {
            "type": type(exc).__name__}
    finally:
        if server is not None:
            try:
                server.stop()
                report["cleanup"]["server_stopped"] = not server.running()
            except Exception as exc:
                report["cleanup"]["server_stop_error"] = str(exc)
    if not all(report["cleanup"].get(key) for key in ("model_removed", "mcp_disconnected", "server_stopped")):
        report["success"] = False
        report["failed_stage"] = report["failed_stage"] or "cleanup"
        report["error"] = report["error"] or "One or more test resources were not confirmed cleaned up."
    report["finished_at"] = datetime.now().astimezone().isoformat()
    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run_acceptance(args))
    args.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    path = args.report_dir / f"{stamp}_thermal_acceptance.json"
    path.write_text(json.dumps(safe_report_value(report), ensure_ascii=False,
                               indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"success": report["success"], "json_report": str(path),
                      "model_tag": report["model_tag"], "run_id": report["run_id"],
                      "temperature_center_K": report["temperature_center_K"],
                      "failed_stage": report["failed_stage"], "error": report["error"],
                      "cleanup": {k: v for k, v in report["cleanup"].items()
                                  if k in ("model_removed", "mcp_disconnected", "server_stopped")}},
                     ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
