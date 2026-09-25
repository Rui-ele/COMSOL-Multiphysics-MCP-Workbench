"""COMSOL MCP Server - Main entry point."""

import logging

from .core.tool_runtime import AuditedFastMCP
from .tools.session import register_session_tools
from .tools.model import register_model_tools
from .tools.geometry import register_geometry_tools
from .tools.mesh import register_mesh_tools
from .tools.study import register_study_tools
from .tools.results import register_results_tools
from .tools.diagnostics import register_diagnostic_tools
from .tools.model_api import register_model_api_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = AuditedFastMCP("COMSOL MCP")


def register_all_tools() -> None:
    """Register all MCP tools."""
    register_session_tools(mcp)
    register_model_tools(mcp)
    register_geometry_tools(mcp)
    register_mesh_tools(mcp)
    register_study_tools(mcp)
    register_results_tools(mcp)
    register_diagnostic_tools(mcp)
    register_model_api_tools(mcp)
    logger.info("Registered all tools")


def main() -> None:
    """Run the MCP server."""
    logger.info("Starting COMSOL MCP Server...")
    
    register_all_tools()
    
    mcp.run()


if __name__ == "__main__":
    main()
