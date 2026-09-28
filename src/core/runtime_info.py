"""Small, local provenance stamp for reports copied to an analysis assistant."""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re

from .. import __version__


REPOSITORY_URL = "https://github.com/Rui-ele/COMSOL-Multiphysics-MCP-Workbench"


@lru_cache(maxsize=1)
def _package_versions() -> dict:
    versions = {}
    for package, key in (("comsol-mcp-workbench", "package_version"), ("mph", "mph_version")):
        try:
            versions[key] = version(package)
        except PackageNotFoundError:
            versions[key] = None
    return versions


def _source_revision(root: Path) -> str | None:
    """Read Git's local revision files, including worktrees and packed refs."""
    git_dir = root / ".git"
    if git_dir.is_file():
        pointer = git_dir.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir: "):
            return None
        git_dir = root / pointer.removeprefix("gitdir: ")
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref = head.removeprefix("ref: ")
        if not ref.startswith("refs/") or ".." in Path(ref).parts:
            return None
        common_dir = git_dir
        if (git_dir / "commondir").is_file():
            common_dir = git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()
        for directory in dict.fromkeys((git_dir, common_dir)):
            if (directory / ref).is_file():
                head = (directory / ref).read_text(encoding="utf-8").strip()
                break
        else:
            head = ""
            packed = common_dir / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    fields = line.split()
                    if len(fields) == 2 and fields[1] == ref:
                        head = fields[0]
                        break
    return head if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head) else None


def runtime_info() -> dict:
    """Stamp reports using local metadata, without launching subprocesses.

    MCP executes synchronous tools on its event loop. Git process creation and
    worktree scans therefore belong in the standalone connection checker.
    """
    result = {
        "repository": REPOSITORY_URL,
        "workbench_version": __version__,
        "package_version": None,
        "mph_version": None,
        "source_revision": None,
        "source_dirty": None,
        "source_dirty_status": "not_checked",
        "source_status": "unavailable",
        "publication_status": "not_checked",
    }
    result.update(_package_versions())

    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return result
    try:
        revision = _source_revision(root)
        result.update(source_revision=revision,
                      source_status="local_checkout" if revision else "unavailable")
    except (OSError, ValueError) as exc:
        result["source_error"] = str(exc)
    return result
