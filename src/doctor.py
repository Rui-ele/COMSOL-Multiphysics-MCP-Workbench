"""Read-only environment diagnostics for the COMSOL MCP workbench."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Iterable


CORE_DISTRIBUTIONS = ("mcp", "MPh", "pydantic", "JPype1")


def platform_architecture() -> str:
    """Return the COMSOL platform directory for this Python process."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        return "macarm64" if machine in {"arm64", "aarch64"} else "maci64"
    if system == "Linux":
        return "glnxa64"
    if system == "Windows":
        return "win64"
    return "unknown"


def candidate_comsol_roots(version: str) -> list[Path]:
    """Return explicit and conventional COMSOL installation roots."""
    token = version.replace(".", "")
    explicit = [
        os.environ.get("COMSOL_MCP_COMSOL_ROOT"),
        os.environ.get("COMSOL_ROOT"),
        os.environ.get("COMSOL_HOME"),
    ]
    candidates = [Path(value).expanduser() for value in explicit if value]
    system = platform.system()
    if system == "Darwin":
        candidates.extend(
            [
                Path(f"/Applications/COMSOL{token}/Multiphysics"),
                Path.home() / f"Applications/COMSOL{token}/Multiphysics",
            ]
        )
    elif system == "Linux":
        candidates.extend(
            [
                Path(f"/usr/local/comsol{token}/multiphysics"),
                Path(f"/opt/comsol{token}/multiphysics"),
            ]
        )
    elif system == "Windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates.append(
            Path(program_files) / "COMSOL" / f"COMSOL{token}" / "Multiphysics"
        )
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def find_comsol_root(version: str) -> Path | None:
    """Find a plausible COMSOL root without starting COMSOL."""
    arch = platform_architecture()
    executable = "comsol.exe" if platform.system() == "Windows" else "comsol"
    for root in candidate_comsol_roots(version):
        if (
            (root / "bin" / arch / executable).is_file()
            and (root / "plugins").is_dir()
            and (root / "apiplugins").is_dir()
        ):
            return root.resolve()
    return None


def find_java_home(root: Path | None) -> Path | None:
    """Find JAVA_HOME from an override or a discovered COMSOL installation."""
    configured = os.environ.get("JAVA_HOME")
    if configured:
        path = Path(configured).expanduser()
        if path.is_dir():
            return path.resolve()
    if root is None:
        return None
    base = root / "java" / platform_architecture() / "jre"
    candidates = [base / "Contents" / "Home", base]
    return next((path.resolve() for path in candidates if path.is_dir()), None)


def distribution_versions(names: Iterable[str]) -> dict[str, str | None]:
    """Read installed package versions without importing COMSOL."""
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def build_report(version: str) -> dict[str, Any]:
    """Build a JSON-serializable diagnostic report."""
    root = find_comsol_root(version)
    java_home = find_java_home(root)
    packages = distribution_versions(CORE_DISTRIBUTIONS)
    checks = {
        "python_supported": sys.version_info >= (3, 10),
        "core_dependencies_installed": all(packages.values()),
        "comsol_installation_found": root is not None,
        "java_runtime_found": java_home is not None,
    }
    return {
        "success": all(checks.values()),
        "checks": checks,
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "platform": platform.platform(),
            "comsol_architecture": platform_architecture(),
        },
        "packages": packages,
        "comsol": {
            "requested_version": version,
            "root": str(root) if root else None,
            "java_home": str(java_home) if java_home else None,
        },
        "notes": [
            "This command does not start COMSOL or consume a license.",
            "Use COMSOL_MCP_COMSOL_ROOT when COMSOL is installed in a custom location.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check Python, dependencies, COMSOL, and Java without starting COMSOL."
    )
    parser.add_argument(
        "--version",
        default=os.environ.get("COMSOL_MCP_COMSOL_VERSION", "6.4"),
        help="COMSOL version to locate (default: env or 6.4).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)
    report = build_report(args.version)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("COMSOL MCP environment doctor")
        for name, passed in report["checks"].items():
            print(f"  {'OK' if passed else 'FAIL'}  {name}")
        print(f"  Python: {report['python']['version']}")
        print(f"  COMSOL root: {report['comsol']['root'] or 'not found'}")
        print(f"  JAVA_HOME: {report['comsol']['java_home'] or 'not found'}")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
