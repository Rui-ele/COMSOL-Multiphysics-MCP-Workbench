"""Small, local provenance stamp for reports copied to an analysis assistant."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import subprocess

from .. import __version__


REPOSITORY_URL = "https://github.com/Rui-ele/COMSOL-Multiphysics-MCP-Workbench"


def runtime_info() -> dict:
    """Describe this installation without connecting to COMSOL or the network.

    Source revisions are only reported for this checkout. A wheel installation
    without Git metadata stays unknown rather than borrowing an ancestor repo.
    A local commit does not imply that GitHub already contains that commit.
    """
    result = {
        "repository": REPOSITORY_URL,
        "workbench_version": __version__,
        "package_version": None,
        "mph_version": None,
        "source_revision": None,
        "source_dirty": None,
        "source_status": "unavailable",
        "publication_status": "not_checked",
    }
    for package, key in (
        ("comsol-mcp-workbench", "package_version"),
        ("mph", "mph_version"),
    ):
        try:
            result[key] = version(package)
        except PackageNotFoundError:
            pass

    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return result
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.strip()
        state = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout
        result.update(
            source_revision=revision,
            source_dirty=bool(state.strip()),
            source_status="local_checkout",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["source_error"] = str(exc)
    return result
