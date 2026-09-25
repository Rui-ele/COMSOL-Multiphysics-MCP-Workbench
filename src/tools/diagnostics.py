"""Read-only diagnosis tools for the GPT ↔ local Agent handoff."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import diagnostics, reports
from ..core.solver import async_solver
from ..core.session import session_manager


def _run(tool_name: str, arguments: dict) -> dict:
    try:
        reference = arguments["model_name"]
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("model_name must be an explicit COMSOL tag or unique registered label; discover/attach the intended model first.")
        if tool_name in {"diagnostic_collect", "diagnostic_parameters_read"}:
            diagnostics.validate_names(arguments.get("parameter_names"), "parameter_names")
            symptom = arguments.get("symptom", "")
            if not isinstance(symptom, str):
                raise ValueError("symptom must be text.")
        elif tool_name in {"diagnostic_node_read", "diagnostic_tree_read"}:
            diagnostics.validate_names(arguments.get("properties"), "properties")
            diagnostics.validate_node_path(arguments["node_path"])
            for key in ("child_offset", "property_offset"):
                offset = arguments.get(key, 0)
                if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                    raise ValueError(f"{key} must be a nonnegative integer.")
        else:
            raise ValueError(f"Unknown diagnostic reader: {tool_name}")
        record = session_manager.get_model_record(reference)
        if record is None:
            raise LookupError("Model is not registered. Discover and attach the intended server model; no default model was selected.")
        if tool_name == "diagnostic_collect":
            return diagnostics.collect(record, session_manager.client, arguments, async_solver.get_progress)
        if tool_name == "diagnostic_parameters_read":
            return diagnostics.parameters_read(record, session_manager.client, arguments)
        if tool_name == "diagnostic_tree_read":
            return diagnostics.tree_read(record, session_manager.client, arguments)
        return diagnostics.node_read(record, session_manager.client, arguments)
    except Exception as exc:
        failure = exc.to_result() if hasattr(exc, "to_result") else {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        if hasattr(exc, "candidate_tags"):
            failure["candidate_tags"] = exc.candidate_tags
        return diagnostics.failure_packet(tool_name, arguments, failure)


def register_diagnostic_tools(mcp: FastMCP) -> None:
    """Register overview, scoped readers and frozen-report pagination."""
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    @mcp.tool(annotations=annotations)
    def diagnostic_collect(
        model_name: str,
        symptom: str = "",
        parameter_names: list[str] | None = None,
    ) -> dict:
        """Obtain a model overview so GPT can choose the next inspection scope.

        Supply an exact model tag or unique registered label; symptom records
        the question alongside the facts. The overview includes 40 parameter
        expressions (or every explicitly named parameter) and previews up to
        64 nodes across selected groups, to depth 4, with 4 current problems
        per visited study/solver/geometry/mesh node. Coverage and follow-up
        tools identify what remains outside this overview.

        Use diagnostic_parameters_read for complete parameter requests,
        diagnostic_tree_read for a selected supported subtree, and
        diagnostic_node_read for complete node settings and current problems.
        Long reports retain every collected value; append report_markdown
        pages using diagnostic_report_page and report_delivery.next_offset.
        """
        return _run("diagnostic_collect", {"model_name": model_name, "symptom": symptom, "parameter_names": parameter_names})

    @mcp.tool(annotations=annotations)
    def diagnostic_node_read(
        model_name: str,
        node_path: str,
        properties: list[str] | None = None,
        include_selection: bool = True,
        child_offset: int = 0,
        property_offset: int = 0,
    ) -> dict:
        """Read the facts requested for one exact model node.

        node_path uses returned tags, e.g. physics/ht/hf1, studies/std1/stat,
        solutions/sol1/s1, or a group such as physics. properties=None reads
        all available properties; a name list reads those properties;
        properties=[] lists names only. Returns all immediate children in the
        supported feature/propertyGroup collection, all current node problems,
        and the complete entity selection where that API is supported.
        include_selection=False skips selection reads. Geometry-feature object
        selections require a documented API getter via comsol_api_read.

        child_offset/property_offset select the starting index for existing
        callers; default 0 collects the complete requested list. Large arrays,
        strings and error text remain intact in the stored report. Follow
        report_delivery.next_offset with diagnostic_report_page and append its
        report_markdown until the complete report has been received.
        """
        return _run("diagnostic_node_read", {"model_name": model_name, "node_path": node_path, "properties": properties, "include_selection": include_selection, "child_offset": child_offset, "property_offset": property_offset})

    @mcp.tool(annotations=annotations)
    def diagnostic_parameters_read(
        model_name: str,
        parameter_names: list[str] | None = None,
    ) -> dict:
        """Read every global parameter, or every parameter in the supplied list.

        Returns raw expressions and descriptions with their actual read status.
        A failed parameter read leaves other independent reads available.
        Complete long values are retained; follow report_delivery.next_offset
        with diagnostic_report_page to assemble all report_markdown pages.
        """
        return _run("diagnostic_parameters_read", {"model_name": model_name, "parameter_names": parameter_names})

    @mcp.tool(annotations=annotations)
    def diagnostic_tree_read(model_name: str, node_path: str) -> dict:
        """Read a complete subtree within the returned tag-path navigation scope.

        Supply a built-in group or returned exact node_path, e.g. physics or
        solutions/sol1. Traversal follows propertyGroup() where present,
        otherwise feature(), at every depth. Returns each node's tag, label,
        type and active state, plus all current problems on study, solution,
        geometry and mesh nodes. Other named child collections are identified
        for an explicit comsol_api_read follow-up. Node settings are read with
        diagnostic_node_read. Independent branches continue after read errors.

        Read all report_markdown pages using diagnostic_report_page and the
        returned next_offset; continuation pages use the stored report.
        """
        return _run("diagnostic_tree_read", {"model_name": model_name, "node_path": node_path})

    @mcp.tool(annotations=annotations)
    def diagnostic_report_page(
        report_id: str,
        offset: int = 0,
        max_chars: int = 12000,
    ) -> dict:
        """Fetch a stored diagnostic or API report page without rereading COMSOL.

        Use report_delivery.report_id and next_offset from the previous page.
        Append report_markdown fragments in offset order until next_offset is
        null. The final concatenation is the complete Markdown report, including
        a summary and complete facts; sha256 describes that full UTF-8 text.
        max_chars selects 1–24000 characters per page. The cache retains the
        16 most recently used reports for the lifetime of this MCP process.
        An expired report is reported explicitly so its pages can be kept
        separate from a new collection.
        """
        return reports.report_page(report_id, offset, max_chars)
