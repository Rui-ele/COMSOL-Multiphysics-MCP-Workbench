"""Read-only COMSOL inspection and scoped model facts.

Node paths use a built-in MPh group followed by COMSOL tags, not labels.
The navigation/getter calls follow MPh 1.3's node.py; selection getters follow
COMSOL's Selection/AbstractSelection API. No method supplied by a caller is
invoked, and no geometry, mesh, solver or export is run by this module.
"""

from __future__ import annotations

from typing import Any, Callable

from .reports import fact, finish_diagnostic_packet, new_report_packet, read, serialize_value, unavailable

GROUPS = frozenset({
    "parameters", "functions", "components", "geometries", "views",
    "selections", "coordinates", "variables", "couplings", "physics",
    "multiphysics", "materials", "meshes", "studies", "solutions", "batches",
    "datasets", "evaluations", "tables", "plots", "exports",
})
OVERVIEW_GROUPS = (
    "studies", "solutions", "physics", "multiphysics", "geometries",
    "meshes", "materials", "selections", "components", "datasets",
)
# Sixty-four nodes in total, reserving space for every overview group.
OVERVIEW_NODE_LIMITS = {
    "studies": 12, "solutions": 16, "physics": 8, "multiphysics": 4,
    "geometries": 8, "meshes": 8, "materials": 2, "selections": 2,
    "components": 2, "datasets": 2,
}
MAX_DEPTH = 4
MAX_PARAMETERS = 40
TRAVERSAL_SCOPE = {
    "navigation": "Built-in group tags, then propertyGroup() where present, otherwise feature().",
    "coverage": "Descriptors and descendants reachable through that tag-path navigation.",
    "other_accessors": "Other named accessors, such as a component's physics(), geom() or material(), are listed where present and read separately with a documented comsol_api_read path.",
}


def _packet(tool_name: str, arguments: dict) -> dict:
    packet = new_report_packet(tool_name, arguments, success=True, status="collected")
    packet.update(
        collected_at=packet["started_at"],
        read_only=True,
        scope="Existing model state only; no build, mesh, solve, save or export. Reads are sequential, not an atomic model snapshot.",
        node_path_format="Built-in group / exact COMSOL tag / child tag (for example solutions/sol1/s1). Labels are informational only.",
        findings={},
    )
    return packet


def failure_packet(tool_name: str, arguments: dict, failure: dict) -> dict:
    """Wrap tool or AuditedFastMCP guard failures in the same report format."""
    packet = _packet(tool_name, arguments)
    packet.update(success=False, status="error")
    for key in ("error", "error_code", "candidate_tags", "hint"):
        if key in failure:
            packet[key] = serialize_value(failure[key])
    packet["findings"]["request_failure"] = {
        "status": "error", "source": "local MCP validation", "details": fact("failure", failure),
    }
    return finish_diagnostic_packet(packet)


def validate_names(names: list[str] | None, label: str, limit: int | None = None) -> None:
    if names is None:
        return
    if not isinstance(names, list):
        raise ValueError(f"{label} must be a list of names.")
    if limit is not None and len(names) > limit:
        raise ValueError(f"{label} must contain at most {limit} items.")
    for name in names:
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(ord(char) < 32 for char in name):
            raise ValueError(f"Each {label} entry must be a nonempty name without control characters (maximum 128 characters).")
    if len(set(names)) != len(names):
        raise ValueError(f"{label} must not contain duplicates.")


def validate_node_path(node_path: str) -> list[str]:
    if not isinstance(node_path, str) or len(node_path) > 2048:
        raise ValueError("node_path must be a tag path of at most 2048 characters.")
    parts = node_path.split("/")
    if parts[0] not in GROUPS:
        raise ValueError("node_path must begin with a supported built-in group.")
    if any(not part or part in {".", ".."} or any(ord(char) < 32 for char in part) for part in parts):
        raise ValueError("node_path must contain exact tags, with no empty, dot or control-character segments.")
    return parts


def _group(model: Any, name: str) -> Any:
    # MPh resolves only this fixed group name; all child lookup uses exact tags.
    return (model / name).java


def _children(java: Any, is_group: bool = False) -> Any | None:
    if is_group:
        return java
    if hasattr(java, "propertyGroup"):
        return java.propertyGroup()
    if hasattr(java, "feature"):
        return java.feature()
    return None


def resolve_node(model: Any, node_path: str) -> tuple[Any, bool]:
    parts = validate_node_path(node_path)
    java = _group(model, parts[0])
    if java is None:
        raise LookupError(f"Group unavailable: {parts[0]}")
    for index, tag in enumerate(parts[1:]):
        container = _children(java, is_group=index == 0)
        if container is None or tag not in {str(item) for item in container.tags()}:
            raise LookupError(f"Exact node tag not found at {'/'.join(parts[:index + 2])}; use returned node_path values, not labels.")
        java = container.get(tag)
    return java, len(parts) == 1


def _descriptor(java: Any, path: str) -> dict:
    return {
        "node_path": path,
        "tag": path.rsplit("/", 1)[-1],
        "label": read(f"{path}.label()", lambda: str(java.label())),
        "type": read(f"{path}.getType()", lambda: str(java.getType())),
        "active": read(f"{path}.isActive()", lambda: bool(java.isActive())),
        "child_navigation": _child_navigation(java),
    }


def _child_navigation(java: Any) -> dict:
    collection = "propertyGroup" if hasattr(java, "propertyGroup") else "feature" if hasattr(java, "feature") else None
    # Presence is useful for planning a documented API call, but does not prove
    # that every accessor supports a zero-argument tag listing on this object.
    names = ("feature", "propertyGroup", "component", "geom", "mesh", "physics", "multiphysics", "material", "coordSystem", "variable", "coupling", "func", "selection")
    other = [name for name in names if name != collection and hasattr(java, name)]
    return {
        "tag_path_collection": collection,
        "other_accessors_not_traversed": other,
        "accessor_scope": "Names present on this object; overloads and values require an explicit API read.",
    }


def _problems(java: Any, path: str, limit: int | None = None) -> dict:
    """Read current problem objects on this node, without recursive model walks."""
    source = f"{path}.problem(): current node problems (no history)"
    if not hasattr(java, "problem"):
        return unavailable(source, "unsupported", "This node has no problem() API.")
    items, errors = [], []
    try:
        stack = [(java, str(tag), f"{path}.problem({str(tag)!r})") for tag in reversed(list(java.problem().tags()))]
        while stack and (limit is None or len(items) < limit):
            owner, tag, problem_path = stack.pop()
            try:
                problem = owner.problem(tag)
                message = read(f"{problem_path}.message", lambda: str(problem.message()) if hasattr(problem, "message") else str(problem.getString("message")))
                items.append({
                    "node_path": path, "problem_tag": tag, "problem_path": problem_path,
                    "message": message,
                    "type": read(f"{problem_path}.getType()", lambda: str(problem.getType())),
                })
                if hasattr(problem, "problem"):
                    stack.extend((problem, str(child), f"{problem_path}.problem({str(child)!r})") for child in reversed(list(problem.problem().tags())))
            except Exception as exc:
                errors.append(unavailable(problem_path, "error", f"{type(exc).__name__}: {exc}"))
        result = {"status": "error" if errors else "success" if items else "empty", "source": source, "value": items}
        if errors:
            result["errors"] = errors
        if stack:
            result.update(status="partial", coverage={"kind": "overview", "limit": limit, "pending_at_least": len(stack), "follow_up_tool": "diagnostic_node_read", "node_path": path, "properties": [], "include_selection": False})
        return result
    except Exception as exc:
        result = unavailable(source, "error", f"{type(exc).__name__}: {exc}")
        # Later failures must not discard messages already obtained from
        # COMSOL; they can be the most useful evidence in a failed solve.
        result["value"] = items
        return result


def _tree(model: Any, node_path: str, depth: int | None = None, max_nodes: int | None = None, problem_limit: int | None = None) -> dict:
    source = f"COMSOL {node_path} tags and child feature/property-group tags"
    rows, cuts, errors = [], [], []
    try:
        root, is_group = resolve_node(model, node_path)
        group = node_path.split("/", 1)[0]
        # Each stack item represents one node, so a bad sibling cannot prevent
        # reads of the other siblings. Tags are resolved only within their parent.
        stack = [(root, None, node_path, 0)] if not is_group else [
            (root, str(tag), f"{node_path}/{tag}", 1)
            for tag in reversed(list(root.tags()))
        ]
        visited = set()
        while stack and (max_nodes is None or len(rows) < max_nodes):
            owner, tag, path, level = stack.pop()
            if path in visited:
                errors.append(unavailable(path, "error", "COMSOL returned a duplicate tag path during this traversal."))
                continue
            visited.add(path)
            try:
                java = owner if tag is None else owner.get(tag)
                item = _descriptor(java, path)
                if group in {"geometries", "meshes", "solutions", "studies"}:
                    item["current_problems"] = _problems(java, path, problem_limit)
                rows.append(item)
                child_container = _children(java)
                if child_container is not None:
                    tags = [str(child) for child in child_container.tags()]
                    if depth is None or level < depth:
                        stack.extend((child_container, child, f"{path}/{child}", level + 1) for child in reversed(tags))
                    elif tags:
                        cuts.append({"path": path, "reason": "overview_depth", "max_depth": depth, "child_count": len(tags)})
            except Exception as exc:
                errors.append({"path": path, "failure": unavailable(path, "error", f"{type(exc).__name__}: {exc}")})
        if stack:
            cuts.append({"reason": "overview_node_count", "limit": max_nodes, "unvisited_branches": [item[2] for item in stack]})
        result = {"status": "error" if errors else "partial" if cuts else "success" if rows else "empty", "source": source, "value": rows, "node_count": len(rows), "traversal_scope": TRAVERSAL_SCOPE}
        if errors:
            result["errors"] = errors
        if cuts:
            result["coverage"] = {"kind": "overview", "remaining": cuts, "follow_up_tool": "diagnostic_tree_read", "node_path": node_path}
        else:
            result["coverage"] = {"kind": "declared_tag_path_subtree", "complete": not errors, "not_covered": "Named accessors listed in each node's child_navigation.other_accessors_not_traversed."}
        return result
    except Exception as exc:
        return unavailable(source, "error", f"{type(exc).__name__}: {exc}")


def _parameters(model: Any, names: list[str] | None, limit: int | None = None) -> dict:
    try:
        available = [str(name) for name in model.java.param().varnames()]
    except Exception as exc:
        return unavailable("model.param().varnames()", "error", str(exc))
    selected = available[:limit] if names is None and limit is not None else available if names is None else names
    items = []
    for name in selected:
        if name not in available:
            items.append({"name": name, "expression": unavailable("model.param().varnames()", "error", "Requested parameter does not exist.")})
            continue
        items.append({"name": name,
                      "expression": read(f"parameter({name!r})", lambda name=name: model.parameter(name)),
                      "description": read(f"description({name!r})", lambda name=name: model.description(name))})
    result = {"status": "success" if items else "empty", "source": "global model parameters (raw expressions; not evaluated)", "value": items, "available_count": len(available), "returned_count": len(items)}
    if names is None and len(available) > len(selected):
        result.update(status="partial", coverage={"kind": "overview", "total": len(available), "returned": len(selected), "follow_up_tool": "diagnostic_parameters_read"})
    elif names is not None:
        result["scope"] = "Explicitly requested parameters."
    else:
        result["scope"] = "All global parameters."
    return result


def _identity(record: Any, client: Any) -> dict:
    model = record.model
    return {
        "model_tag": record.tag,
        "model_label": read("model.name()", model.name),
        "file": read("model.java.getFilePath()", lambda: str(model.java.getFilePath())),
        "last_saved_comsol_version": read("model.version() (last saved version, not runtime)", model.version),
        "comsol_client_version": read("connected mph.Client.version", lambda: str(client.version)),
    }


def collect(record: Any, client: Any, arguments: dict, async_progress: Callable[[], dict]) -> dict:
    packet = _packet("diagnostic_collect", arguments)
    packet["model"] = _identity(record, client)
    findings = packet["findings"]
    packet["collection_scope"] = {
        "kind": "overview", "parameter_preview_count": MAX_PARAMETERS,
        "node_preview_limits": OVERVIEW_NODE_LIMITS, "maximum_preview_depth": MAX_DEPTH,
        "follow_up": "Use diagnostic_parameters_read for all or selected parameters, diagnostic_tree_read for the selected tag-path subtree described by traversal_scope, and diagnostic_node_read for node settings, selections and current problems.",
    }
    findings["parameters"] = _parameters(record.model, arguments.get("parameter_names"), limit=MAX_PARAMETERS)
    findings["model_tree"] = {group: _tree(record.model, group, MAX_DEPTH if group in {"studies", "solutions", "physics", "geometries", "meshes"} else 1, OVERVIEW_NODE_LIMITS[group], problem_limit=4) for group in OVERVIEW_GROUPS}
    findings["current_problem_scope"] = fact("collector policy", "Current problems are sampled on visited study, solution, geometry and mesh nodes. diagnostic_node_read reads the complete current problem list for a selected node; diagnostic_tree_read reads these lists throughout a selected subtree. Historical solver logs require a separate source.")
    observation = read("local MCP AsyncSolver.get_progress() (latest local run only)", async_progress)
    worker_state = observation.get("value", {})
    if observation.get("status") == "success" and isinstance(worker_state, dict) and worker_state.get("model_tag") != record.tag:
        observation = fact("local MCP AsyncSolver", None)
        observation["reason"] = "The latest local run does not belong to this model tag. Query a known run_id for an earlier run."
    findings["async_solver_observation"] = observation
    findings["async_solver_scope"] = fact("local MCP AsyncSolver", "Runs carry model_tag and run_id. This observation covers this MCP process; Desktop computations have their own state.")
    findings["omitted"] = unavailable("collector policy", "omitted", "Detailed node properties, selection entities and field solution arrays are not collected by default. Use diagnostic_node_read for targeted node properties/selection; no result arrays are evaluated here.")
    return finish_diagnostic_packet(packet)


def parameters_read(record: Any, client: Any, arguments: dict) -> dict:
    packet = _packet("diagnostic_parameters_read", arguments)
    packet["model"] = _identity(record, client)
    packet["collection_scope"] = {"kind": "selected_parameters" if arguments.get("parameter_names") is not None else "all_global_parameters"}
    packet["findings"]["parameters"] = _parameters(record.model, arguments.get("parameter_names"))
    return finish_diagnostic_packet(packet)


def tree_read(record: Any, client: Any, arguments: dict) -> dict:
    packet = _packet("diagnostic_tree_read", arguments)
    packet["model"] = _identity(record, client)
    packet["collection_scope"] = {"kind": "complete_declared_tag_path_subtree", "node_path": arguments["node_path"], "traversal_scope": TRAVERSAL_SCOPE, "properties": "Read with diagnostic_node_read for selected nodes."}
    packet["findings"]["model_tree"] = _tree(record.model, arguments["node_path"])
    return finish_diagnostic_packet(packet)


def _properties(java: Any, path: str, requested: list[str] | None, offset: int) -> dict:
    if not hasattr(java, "properties"):
        return unavailable(f"{path}.properties()", "unsupported", "This node has no properties() API.")
    try:
        # Use MPh's existing type-aware reader on the exact Java handle. Going
        # through model/label would silently select the first duplicate label.
        from mph.node import get
        available = sorted(str(name) for name in java.properties())
        selected = available[offset:] if requested is None else requested
        items = {}
        for name in selected:
            if name not in available:
                items[name] = unavailable(f"{path}.{name}", "error", "Property not listed by COMSOL for this node.")
            else:
                items[name] = read(f"{path}.property({name!r})", lambda name=name: get(java, name))
                if items[name]["status"] == "error" and "Cannot convert Java data type" in items[name].get("reason", ""):
                    items[name]["status"] = "unsupported"
        result = {"status": "success" if items else "empty", "source": f"{path}.properties() and MPh type-aware getter", "value": items}
        result["available_properties"] = {
            "status": "success" if available[offset:] else "empty",
            "source": f"{path}.properties()", "value": available[offset:],
            "total": len(available), "offset": offset,
            "next_offset": None,
        }
        result["scope"] = "Explicitly requested properties." if requested is not None else f"All available properties from index {offset}."
        return result
    except Exception as exc:
        return unavailable(f"{path}.properties()", "error", f"{type(exc).__name__}: {exc}")


def _selection(java: Any, path: str) -> dict:
    # Geometry-object selections use a different interface. Do not guess that
    # they are geometric entity numbers, or run geometry to obtain them.
    if path.startswith("geometries/"):
        return unavailable(f"{path}.selection", "unsupported", "Geometry feature object selections are not supported by this entity-selection reader.")
    try:
        if hasattr(java, "entities"):
            selection = java
        elif hasattr(java, "selection"):
            selection = java.selection()
        else:
            return unavailable(f"{path}.selection", "unsupported", "Node has no entity selection API.")
        # Documented getters: COMSOL Selection/AbstractSelection/LocalSelection.
        # https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/Selection.html
        return {
            "status": "success", "source": f"{path}.selection() (existing geometry only)",
            "value": {
                "named_selection_tag": read("selection.named()", lambda: _optional_text(selection.named())),
                "geometry_tag": read("selection.geom()", lambda: _optional_text(selection.geom())),
                "dimension": read("selection.dim()", lambda: int(selection.dim())),
                "dimensions": read("selection.dimension()", lambda: selection.dimension()),
                "is_global": read("selection.isGlobal()", lambda: bool(selection.isGlobal())),
                "is_whole_geometry": read("selection.isGeom()", lambda: bool(selection.isGeom())),
                "entities": read("selection.entities() (maximum selection dimension)", lambda: selection.entities()),
            },
            "interpretation": "An empty entities array alone does not mean an invalid boundary condition: check global/whole-geometry flags, dimension and read errors. No geometry was rebuilt.",
        }
    except Exception as exc:
        return unavailable(f"{path}.selection()", "error", f"{type(exc).__name__}: {exc}")


def _optional_text(value: Any) -> str | None:
    """Convert a Java string while preserving null, e.g. global selection geom."""
    return None if value is None else str(value)


def node_read(record: Any, client: Any, arguments: dict) -> dict:
    packet = _packet("diagnostic_node_read", arguments)
    packet["model"] = _identity(record, client)
    path = arguments["node_path"]
    java, is_group = resolve_node(record.model, path)
    findings = packet["findings"]
    findings["node"] = fact("exact tag lookup", {"node_path": path}) if is_group else _descriptor(java, path)
    findings["properties"] = _properties(java, path, arguments.get("properties"), arguments.get("property_offset", 0))
    findings["current_problems"] = _problems(java, path)
    if arguments.get("include_selection", True) and not is_group:
        findings["selection"] = _selection(java, path)
    else:
        findings["selection"] = {"status": "not_requested", "source": "caller scope", "reason": "Selection skipped for this request or group path."}
    try:
        container = _children(java, is_group)
        tags = list(container.tags()) if container is not None else []
        offset = arguments.get("child_offset", 0)
        children, errors = [], []
        for raw_tag in tags[offset:]:
            tag = str(raw_tag)
            try:
                children.append(_descriptor(container.get(tag), f"{path}/{tag}"))
            except Exception as exc:
                errors.append(unavailable(f"{path}/{tag}", "error", f"{type(exc).__name__}: {exc}"))
        findings["children"] = {
            "status": "error" if errors else "success" if children else "empty",
            "source": f"{path} child tags", "total": len(tags), "offset": offset,
            "next_offset": None, "scope": f"All immediate children from index {offset}.",
            "value": children,
            "traversal_scope": TRAVERSAL_SCOPE,
        }
        if errors:
            findings["children"]["errors"] = errors
    except Exception as exc:
        findings["children"] = unavailable(f"{path} child tags", "error", str(exc))
    return finish_diagnostic_packet(packet)
