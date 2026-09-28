#!/usr/bin/env python3
"""Create a local virtual environment and install the workbench."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = PROJECT_ROOT / "vendor" / "windows-cp314"
MCP_CHECK = (
    "import asyncio; from src.server import mcp, register_all_tools; "
    "register_all_tools(); tools = asyncio.run(mcp.list_tools()); "
    "assert any(t.name == 'comsol_status' for t in tools); "
    "print('MCP tools loaded:', len(tools))"
)


def verify_bundle(bundle: Path) -> None:
    """Check the bundled wheels before asking pip to install them."""
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["packages"]:
        path = (bundle / item["file"]).resolve()
        if not path.is_relative_to(bundle.resolve()):
            raise ValueError(f"Invalid bundle path: {item['file']}")
        if not path.is_file():
            raise ValueError(f"Missing {path.name}. Run scripts/install_windows.ps1 to restore it.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Checksum mismatch: {path.name}. Restore this file before installing.")


def interpreter_info(python: Path) -> dict:
    code = (
        "import json, platform, struct, sys, sysconfig; "
        "print(json.dumps(dict(version=list(sys.version_info[:2]), "
        "system=platform.system(), machine=platform.machine(), "
        "implementation=platform.python_implementation(), "
        "bits=struct.calcsize('P')*8, free_threaded=bool(sysconfig.get_config_var('Py_GIL_DISABLED')))))"
    )
    return json.loads(subprocess.check_output([str(python), "-c", code], text=True, timeout=20))


def require_bundle_interpreter(info: dict) -> None:
    if not (
        info["system"] == "Windows"
        and info["version"] == [3, 14]
        and info["implementation"] == "CPython"
        and info["bits"] == 64
        and info["machine"].lower() in {"amd64", "x86_64"}
        and not info["free_threaded"]
    ):
        raise ValueError(
            "The offline bundle needs standard CPython 3.14 for Windows x64. "
            "Use scripts/install_windows.ps1 to find or install that Python. "
            "For another platform, use --online."
        )


def environment_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def install_target(*, dev: bool) -> str:
    return ".[dev]" if dev else "."


def planned_commands(
    *,
    python: Path,
    venv: Path,
    dev: bool,
    upgrade_pip: bool,
    bundle: Path | None = None,
    index_url: str | None = None,
    cert: Path | None = None,
) -> list[list[str]]:
    venv_python = environment_python(venv)
    commands = [[str(python), "-m", "venv", str(venv)]]
    network_options = []
    if index_url:
        network_options += ["--index-url", index_url]
    if cert:
        network_options += ["--cert", str(cert)]
    if bundle is not None:
        if dev or upgrade_pip:
            raise ValueError("The offline bundle contains runtime dependencies. Use --online for development tools or a pip upgrade.")
        commands.append([
            str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
            "--no-index", "--find-links", str(bundle / "wheels"),
            "--require-hashes", "-r", str(bundle / "requirements.txt"),
        ])
        commands.append([
            str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
            "--no-index", "--no-build-isolation", "--no-deps", "-e", ".",
        ])
        return commands
    if upgrade_pip:
        commands.append(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", *network_options]
        )
    commands.append(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *network_options,
            "-e",
            install_target(dev=dev),
        ]
    )
    return commands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create .venv and install COMSOL MCP Workbench."
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--venv", type=Path, default=PROJECT_ROOT / ".venv")
    parser.add_argument("--dev", action="store_true", help="Also install development dependencies (online mode).")
    parser.add_argument("--upgrade-pip", action="store_true", help="Upgrade pip (online mode).")
    parser.add_argument("--no-dev", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-pip-upgrade", action="store_true", help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Install the bundled Windows CPython 3.14 dependencies.")
    mode.add_argument("--online", action="store_true", help="Resolve dependencies from a package index.")
    parser.add_argument("--index-url", help="Package index for online installation.")
    parser.add_argument("--cert", type=Path, help="CA certificate bundle for online installation.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    python = args.python.expanduser().resolve()
    venv = args.venv.expanduser().resolve()
    if not python.is_file():
        raise SystemExit(f"Python executable not found: {python}")
    if venv.exists() and not environment_python(venv).is_file():
        raise SystemExit(f"Refusing to overwrite a non-venv path: {venv}")

    bundle = BUNDLE_ROOT if args.offline or (os.name == "nt" and not args.online) else None
    if bundle:
        try:
            require_bundle_interpreter(interpreter_info(python))
            if environment_python(venv).is_file():
                require_bundle_interpreter(interpreter_info(environment_python(venv)))
            verify_bundle(bundle)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise SystemExit(str(exc)) from exc
    if bundle and (args.index_url or args.cert):
        parser.error("--index-url and --cert apply to --online installation.")
    if bundle and (args.dev or args.upgrade_pip):
        parser.error("Use --online with --dev or --upgrade-pip.")

    commands = planned_commands(
        python=python,
        venv=venv,
        dev=args.dev and not args.no_dev,
        upgrade_pip=args.upgrade_pip and not args.no_pip_upgrade,
        bundle=bundle,
        index_url=args.index_url,
        cert=args.cert,
    )
    if args.dry_run:
        print(json.dumps({"cwd": str(PROJECT_ROOT), "commands": commands}, indent=2))
        return 0

    env = os.environ.copy()
    if bundle:
        env["PIP_NO_INDEX"] = "1"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    if args.index_url:
        env["PIP_INDEX_URL"] = args.index_url
    if args.cert:
        env["PIP_CERT"] = str(args.cert.resolve())
    for command in commands:
        creating_venv = command[2:3] == ["venv"]
        if creating_venv and environment_python(venv).exists():
            continue
        subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True)
    venv_python = environment_python(venv)
    subprocess.run([str(venv_python), "-m", "pip", "check"], cwd=PROJECT_ROOT, env=env, check=True)
    subprocess.run([str(venv_python), "-c", MCP_CHECK], cwd=PROJECT_ROOT, env=env, check=True, timeout=45)
    data_dir = PROJECT_ROOT / ".comsol-mcp-data"
    data_dir.mkdir(exist_ok=True)
    config_path = data_dir / "mcp-client.example.json"
    config_path.write_text(json.dumps({"mcpServers": {"comsol": {
        "command": str(venv_python), "args": ["-m", "src.server"],
        "cwd": str(PROJECT_ROOT), "env": {"COMSOL_MCP_DATA_DIR": str(data_dir)},
    }}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Installed COMSOL MCP Workbench in {venv}")
    print(f"Client configuration: {config_path}")
    print("Next: follow docs/initialize-windows.md to configure COMSOL and connect the client."
          if os.name == "nt" else f"Next: {venv_python} -m src.doctor")
    print("COMSOL connection and license have not been checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
