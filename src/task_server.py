"""Restricted MCP entry point for the expert-confirmed parameter-task PoC."""

from __future__ import annotations

from .reporting import AuditedFastMCP
from .tools.model import register_model_tools
from .tools.parameters import register_parameter_tools
from .tools.session import register_session_tools
from .tools.task import register_task_tools


TASK_TOOL_ALLOWLIST = frozenset(
    {
        "comsol_connect",
        "model_attach",
        "model_detach",
        "model_access_set",
        "param_get",
        "task_parameter_preview",
        "task_parameter_execute",
        "task_parameter_status",
    }
)


class TaskFastMCP(AuditedFastMCP):
    """Expose only the tools needed for one approved parameter operation."""

    def add_tool(self, fn, name=None, **kwargs) -> None:
        if (name or fn.__name__) in TASK_TOOL_ALLOWLIST:
            super().add_tool(fn, name=name, **kwargs)


def build_task_server() -> TaskFastMCP:
    mcp = TaskFastMCP("COMSOL Parameter Tasks")
    register_session_tools(mcp)
    register_model_tools(mcp)
    register_parameter_tools(mcp)
    register_task_tools(mcp)
    return mcp


def main() -> None:
    build_task_server().run()


if __name__ == "__main__":
    main()
