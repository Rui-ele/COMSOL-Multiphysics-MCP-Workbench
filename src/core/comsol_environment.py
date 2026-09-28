"""Apply the configured COMSOL installation to this MCP process."""

import os
from pathlib import Path

from mph import discovery

from ..doctor import platform_architecture


def configure_comsol_environment(version: str | None = None) -> str | None:
    """Expose an explicit installation to MPh and select the requested version."""
    root = os.environ.get("COMSOL_MCP_COMSOL_ROOT", "").strip()
    if root:
        architecture = platform_architecture()
        binary_dir = Path(root).expanduser() / "bin" / architecture
        executable = binary_dir / ("comsol.exe" if architecture == "win64" else "comsol")
        if not executable.is_file():
            raise FileNotFoundError(
                f"COMSOL_MCP_COMSOL_ROOT does not contain the COMSOL executable: {executable}"
            )
        binary = str(binary_dir)
        entries = os.environ.get("PATH", "").split(os.pathsep)
        entries = [entry for entry in entries
                   if os.path.normcase(entry) != os.path.normcase(binary)]
        path = os.pathsep.join([binary, *entries])
        if path != os.environ.get("PATH", ""):
            os.environ["PATH"] = path
            # A previous discovery may have cached a different installation.
            discovery.find_backends.cache_clear()
    return version or os.environ.get("COMSOL_MCP_COMSOL_VERSION", "").strip() or None
