#!/usr/bin/env python3
"""Create a local virtual environment and install the workbench."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def environment_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def install_target(*, dev: bool, knowledge: bool) -> str:
    extras = []
    if dev:
        extras.append("dev")
    if knowledge:
        extras.append("knowledge")
    suffix = f"[{','.join(extras)}]" if extras else ""
    return f".{suffix}"


def planned_commands(
    *,
    python: Path,
    venv: Path,
    dev: bool,
    knowledge: bool,
    upgrade_pip: bool,
) -> list[list[str]]:
    venv_python = environment_python(venv)
    commands = [[str(python), "-m", "venv", str(venv)]]
    if upgrade_pip:
        commands.append(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"]
        )
    commands.append(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "-e",
            install_target(dev=dev, knowledge=knowledge),
        ]
    )
    return commands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create .venv and install COMSOL MCP Workbench."
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--venv", type=Path, default=PROJECT_ROOT / ".venv")
    parser.add_argument("--knowledge", action="store_true")
    parser.add_argument("--no-dev", action="store_true")
    parser.add_argument("--no-pip-upgrade", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    python = args.python.expanduser().resolve()
    venv = args.venv.expanduser().resolve()
    if not python.is_file():
        raise SystemExit(f"Python executable not found: {python}")
    if venv.exists() and not environment_python(venv).is_file():
        raise SystemExit(f"Refusing to overwrite a non-venv path: {venv}")

    commands = planned_commands(
        python=python,
        venv=venv,
        dev=not args.no_dev,
        knowledge=args.knowledge,
        upgrade_pip=not args.no_pip_upgrade,
    )
    if args.dry_run:
        print(json.dumps({"cwd": str(PROJECT_ROOT), "commands": commands}, indent=2))
        return 0

    for command in commands:
        creating_venv = command[2:3] == ["venv"]
        if creating_venv and environment_python(venv).exists():
            continue
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    print(f"Installed COMSOL MCP Workbench in {venv}")
    print(f"Next: {environment_python(venv)} -m src.doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
