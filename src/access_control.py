"""Central access policy for COMSOL model tools."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Literal


ToolAccessPolicy = Literal["none", "read", "write", "conditional"]


# Tools in this set may inspect registry metadata or model state, but they must
# not mutate the referenced COMSOL model or write simulation output files.
READ_ONLY_MODEL_TOOLS = frozenset(
    {
        "model_access_set",
        "model_clone",
        "model_detach",
        "model_inspect",
        "model_list_components",
        "model_set_current",
        "param_get",
        "param_list",
        "task_parameter_preview",
        "geometry_list",
        "geometry_list_features",
        "mesh_list",
        "mesh_info",
        "physics_list",
        "physics_list_features",
        "study_list",
        "solutions_list",
        "datasets_list",
        "results_evaluate",
        "results_global_evaluate",
        "results_inner_values",
        "results_outer_values",
        "results_exports_list",
        "results_plots_list",
        "simulation_report_create",
    }
)


CONDITIONAL_MODEL_TOOLS = frozenset({"param_description"})


def tool_access_policy(
    tool_name: str,
    parameter_names: Collection[str],
) -> ToolAccessPolicy:
    """Classify one tool using a future-safe default for model operations."""
    if "model_name" not in parameter_names:
        return "none"
    if tool_name in CONDITIONAL_MODEL_TOOLS:
        return "conditional"
    if tool_name in READ_ONLY_MODEL_TOOLS:
        return "read"
    return "write"


def requires_write_access(
    tool_name: str,
    arguments: Mapping[str, Any],
    parameter_names: Collection[str],
) -> bool:
    """Return whether this concrete invocation requires model write access."""
    policy = tool_access_policy(tool_name, parameter_names)
    if policy == "conditional":
        # Reading a parameter description is safe; supplying text mutates it.
        return arguments.get("text") is not None
    return policy == "write"
