"""Session management tools for COMSOL MCP Server."""

from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..core.session import session_manager


def register_session_tools(mcp: FastMCP) -> None:
    """Register session management tools."""

    @mcp.tool()
    def comsol_start(
        cores: Optional[int] = None,
        version: Optional[str] = None,
        products: Optional[list[str]] = None,
        port: Optional[int] = None,
    ) -> dict:
        """
        Start a local multi-client COMSOL server and connect MCP to it.

        The returned host and port can be used by COMSOL Desktop to observe
        the same server-side model. If port is omitted, COMSOL starts with its
        default port 2036 or the next available port.

        Args:
            cores: Number of processor cores to use.
            version: COMSOL version, for example "6.4".
            products: Optional list of requested COMSOL products.
            port: Optional fixed server port.
        """
        return session_manager.start(
            cores=cores,
            version=version,
            products=products,
            port=port,
        )

    @mcp.tool()
    def comsol_connect(port: int, host: str = "localhost") -> dict:
        """Connect MCP to a COMSOL server managed outside this process."""
        return session_manager.connect(port=port, host=host)

    @mcp.tool()
    def comsol_disconnect(force: bool = False) -> dict:
        """
        Disconnect from COMSOL and stop an MCP-managed server.

        For an MCP-managed server this refuses to close while COMSOL Desktop
        is observing a server-side model. External servers are never stopped;
        MCP only disconnects and clears its local registry.
        """
        return session_manager.disconnect(force=force)

    @mcp.tool()
    def comsol_status() -> dict:
        """Get session, server, model, and Desktop observer status."""
        return session_manager.get_status()
