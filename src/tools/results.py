"""Evaluate configured numerical nodes, inspect solutions, and run exports."""

from hashlib import sha256
from pathlib import Path
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP

from ..core.session import session_manager
from ..core.reports import read, serialize_value


def _model(model_name: str):
    record = session_manager.get_model_record(model_name)
    if record is None or record.tag != model_name:
        raise ValueError(f"Registered model tag not found: {model_name}")
    return record.model


def _settings(node, source: str) -> dict:
    """Read available result settings independently, preserving unavailable facts."""
    from mph.node import get

    requested = (
        "expr", "descr", "unit", "data", "solution", "innerinput", "solnum",
        "outerinput", "outersolnum", "looplevelinput", "looplevel", "t",
        "dataseries", "dataseriescumulative", "table",
    )
    available = read(source + ".properties()", lambda: node.properties())
    settings = {"available_properties": available}
    if available["status"] not in ("success", "empty"):
        return settings
    for name in requested:
        if name in available["value"]:
            settings[name] = read(
                source + f".property({name!r})",
                lambda name=name: get(node, name),
            )
    return settings


def _file_state(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    if not path.is_file():
        return {"exists": True, "is_file": False}
    metadata = path.stat()
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"exists": True, "is_file": True, "size_bytes": metadata.st_size,
            "modified_ns": metadata.st_mtime_ns, "sha256": digest.hexdigest()}


def register_results_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def results_evaluate(
        model_name: str,
        evaluation_tag: str,
        method: Literal["computeResult", "getComplex", "getData"],
        outer_index: Optional[int] = None,
    ) -> dict:
        """Evaluate one existing numerical node with the task's explicit method.

        Configure expr, data, unit, and solution selection using comsol_api_write
        first. model_name/evaluation_tag are actual tags. computeResult returns
        COMSOL's real/imaginary matrices; getComplex returns its cached/computed
        real/imaginary pair and requires a one-based outer_index. getData is for
        Eval/Interp/Global nodes and returns [expression][solution][vertex], with
        getImagData when isComplex() reports complex values. Choose the method
        from the COMSOL reference for the node type; no fallback is attempted.

        Full arrays retain dimensions. Metadata reads report actual node
        settings and individual errors. Units/settings are COMSOL's returned
        properties; an empty or automatic setting stays explicit. Evaluation
        computes/caches results without appending to an output table.
        """
        source = f"model({model_name}).result().numerical({evaluation_tag})"
        result = {"success": False, "model_tag": model_name, "node": source,
                  "evaluation_tag": evaluation_tag, "method": method,
                  "args": [] if outer_index is None else [outer_index],
                  "execution_attempted": False, "execution_completed": False,
                  "write_attempted": False}
        node = None
        try:
            if method not in ("computeResult", "getComplex", "getData"):
                raise ValueError("method must be computeResult, getComplex, or getData.")
            if method == "getComplex":
                if isinstance(outer_index, bool) or not isinstance(outer_index, int) or outer_index < 1:
                    raise ValueError("getComplex requires a positive one-based outer_index.")
            elif outer_index is not None:
                raise ValueError("outer_index applies to getComplex; other methods use the node's configured selection.")
            model = _model(model_name)
            container = model.java.result().numerical()
            if evaluation_tag not in [str(tag) for tag in container.tags()]:
                raise ValueError(f"Numerical node tag not found: {evaluation_tag}")
            node = model.java.result().numerical(evaluation_tag)
            result["node_type"] = read(source + ".getType()", lambda: node.getType())
            try:
                result["settings_before"] = _settings(node, source)
            except Exception as exc:
                result["settings_before"] = {"status": "error", "reason": str(exc)}
            result["execution_attempted"] = True
            if method == "getData":
                result["real"] = serialize_value(node.getData())
                result["layout"] = "[expression][solution][vertex]"
                complex_state = read(source + ".isComplex()", lambda: bool(node.isComplex()))
                result["is_complex"] = complex_state
                if complex_state.get("value") is True:
                    result["imaginary"] = read(source + ".getImagData()", lambda: node.getImagData())
                elif complex_state.get("value") is False:
                    result["imaginary"] = {"status": "not_applicable", "value": None,
                                           "reason": "COMSOL reports a real-valued result."}
                else:
                    result["imaginary"] = {"status": "unknown", "value": None,
                                           "reason": "The complex-valued state could not be read."}
                result["values_complete"] = (
                    complex_state.get("value") is False or
                    result["imaginary"]["status"] in ("success", "empty")
                )
            else:
                operation = getattr(node, method)
                raw = operation() if outer_index is None else operation(outer_index)
                # Both documented methods return real and imaginary parts in
                # the first and second elements, without scalar coercion.
                result["raw_result"] = serialize_value(raw)
                result["layout"] = "[real/imaginary][row][column]; COMSOL dimensions preserved"
                result["values_complete"] = True
            result["execution_completed"] = True
            result["success"] = result["values_complete"]
            if not result["values_complete"]:
                result["error"] = "Real values were obtained, but complex-result collection is incomplete."
        except Exception as exc:
            result.update(error=str(exc), error_type=type(exc).__name__)
        if node is not None:
            # Auxiliary metadata cannot erase the numerical values or primary error.
            try:
                result["settings_after"] = _settings(node, source)
            except Exception as exc:
                result["settings_after"] = {"status": "error", "reason": str(exc)}
        return result

    @mcp.tool()
    def results_solution_info(model_name: str, dataset_tag: str) -> dict:
        """Read the full solution index/parameter map for one solution dataset.

        dataset_tag is an actual result dataset tag whose solution property
        identifies a solver sequence. Returns every [outer, inner] index tuple,
        matching parameter names, units, real and imaginary parameter values,
        plus outer-sweep names/units. Derived dataset chains remain explicit:
        inspect their source and call this tool on the source solution dataset.
        Independent metadata read failures retain all successfully read values.
        """
        source = f"model({model_name}).result().dataset({dataset_tag})"
        result = {"success": False, "model_tag": model_name,
                  "dataset_tag": dataset_tag, "node": source, "read_only": True}
        try:
            import jpype

            model = _model(model_name)
            if dataset_tag not in [str(tag) for tag in model.java.result().dataset().tags()]:
                raise ValueError(f"Dataset tag not found: {dataset_tag}")
            dataset = model.java.result().dataset(dataset_tag)
            try:
                result["dataset_settings"] = _settings(dataset, source)
            except Exception as exc:
                result["dataset_settings"] = {"status": "error", "reason": str(exc)}
            solution_tag = str(dataset.getString("solution"))
            result["solution_tag"] = solution_tag
            if solution_tag not in [str(tag) for tag in model.java.sol().tags()]:
                raise ValueError(f"Dataset has no existing solution reference: {solution_tag!r}")
            solution = model.java.sol(solution_tag)
            solution_source = f"model({model_name}).sol({solution_tag})"
            result["solution_is_empty"] = read(solution_source + ".isEmpty()", lambda: bool(solution.isEmpty()))
            info = solution.getSolutioninfo()
            info_source = solution_source + ".getSolutioninfo()"
            result["outer_indices"] = read(info_source + ".getOuterSolnum()", lambda: info.getOuterSolnum())
            result["outer_parameter_names"] = read(info_source + ".getPNamesOuter()", lambda: info.getPNamesOuter())
            result["outer_parameter_units"] = read(info_source + ".getPUnitsOuter()", lambda: info.getPUnitsOuter())
            # Empty name constraints request all tuples, including non-sweep solutions.
            tuples = info.getSolnums(jpype.JArray(jpype.JString)([]))
            result["solution_tuples"] = serialize_value(tuples)
            result["tuple_layout"] = "[outer_solution_index, inner_solution_index], one-based"
            facts = {}
            for name, method_name in (
                ("parameter_names", "getPNames"), ("parameter_units", "getUnits"),
                ("parameter_values_real", "getPvals"), ("parameter_values_imaginary", "getPvalsImag"),
            ):
                facts[name] = read(
                    info_source + f".{method_name}(solution_tuples)",
                    lambda method_name=method_name: getattr(info, method_name)(tuples),
                )
            result.update(facts)
            result["success"] = True
            result["collection_complete"] = all(
                value.get("status") in ("success", "empty")
                for value in (*facts.values(), result["outer_indices"],
                              result["outer_parameter_names"], result["outer_parameter_units"])
            )
        except Exception as exc:
            result.update(error=str(exc), error_type=type(exc).__name__)
        return result

    @mcp.tool()
    def results_export(
        model_name: str, export_tag: str, file_path: str,
        file_property: str, overwrite: bool = False,
    ) -> dict:
        """Run one configured export node to an explicit absolute file path.

        export_tag is an actual tag. file_property is its documented filename
        property (for example filename or giffilename). The format, data, and
        plot settings remain those already configured on the node. MCP and
        COMSOL must see the same path. Existing files require overwrite=true.

        Sets that property, reads it back, runs the export, and checks the output
        file's size/hash/modification time. Returns before/after facts and each
        completed action. A returned run() and a verified file are separate facts;
        incomplete/multiple-file export verification is reported as incomplete.
        """
        source = f"model({model_name}).result().export({export_tag})"
        result = {"success": False, "model_tag": model_name, "export_tag": export_tag,
                  "node": source, "file_path": file_path, "file_property": file_property,
                  "write_attempted": False, "execution_completed": False,
                  "completed_actions": []}
        node = None
        path = None
        try:
            path = Path(file_path).expanduser()
            if not path.is_absolute():
                raise ValueError("file_path must be absolute.")
            if not path.parent.is_dir():
                raise ValueError(f"Output directory not found on the MCP host: {path.parent}")
            if not file_property.endswith("filename"):
                raise ValueError("file_property must identify a documented filename property.")
            before = _file_state(path)
            result["file_before"] = before
            if before["exists"] and not overwrite:
                raise ValueError("Output file exists; set overwrite=true when the task authorizes replacement.")
            if before["exists"] and not before.get("is_file"):
                raise ValueError("Output path is not a regular file.")
            model = _model(model_name)
            if export_tag not in [str(tag) for tag in model.java.result().export().tags()]:
                raise ValueError(f"Export node tag not found: {export_tag}")
            node = model.java.result().export(export_tag)
            if file_property not in [str(name) for name in node.properties()]:
                raise ValueError(f"Export node has no property: {file_property}")
            result["node_type"] = read(source + ".getType()", lambda: node.getType())
            result["filename_before"] = read(source + f".getString({file_property})",
                                               lambda: node.getString(file_property))
            result["method"] = "set"
            result["args"] = [file_property, str(path)]
            result["write_attempted"] = True
            node.set(file_property, str(path))
            result["completed_actions"].append({"method": "set", "args": [file_property, str(path)]})
            actual = str(node.getString(file_property))
            result["filename_readback"] = actual
            if actual != str(path):
                raise ValueError("Export filename readback differs from the requested path.")
            result["method"] = "run"
            result["args"] = []
            node.run()
            result["completed_actions"].append({"method": "run", "args": []})
            result["execution_completed"] = True
            after = _file_state(path)
            result["file_after"] = after
            result["file_verified"] = (
                after.get("is_file", False) and after.get("size_bytes", 0) > 0
                and (not before["exists"] or before != after)
            )
            result["success"] = result["file_verified"]
            if not result["file_verified"]:
                result["error"] = "Export run() returned, but a new or updated nonempty output file could not be verified."
        except Exception as exc:
            result.update(error=str(exc), error_type=type(exc).__name__)
            if path is not None and result["write_attempted"]:
                result["file_after"] = read(str(path), lambda: _file_state(path))
            if node is not None and result["write_attempted"]:
                result["filename_readback"] = read(
                    source + f".getString({file_property})",
                    lambda: node.getString(file_property),
                )
        return result
