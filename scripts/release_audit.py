#!/usr/bin/env python3
"""Fail when a release tree contains local, generated, or oversized artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IGNORED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    ".pytest-real",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "local_acceptance",
}
FORBIDDEN_DIRECTORIES = {
    ".comsol-mcp-data",
    "comsol_models",
    "simulation_reports",
}
FORBIDDEN_SUFFIXES = {
    ".class",
    ".db",
    ".mph",
    ".pdf",
    ".pyc",
    ".recovery",
    ".sqlite3",
    ".status",
}
MAX_FILE_BYTES = 5 * 1024 * 1024


def candidate_files(root: Path) -> list[Path]:
    output: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if any(part.startswith(".venv-") for part in relative.parts):
            continue
        if any(part.endswith(".egg-info") for part in relative.parts):
            continue
        output.append(path)
    return sorted(output)


def audit_tree(root: Path) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    bundled_files: dict[str, str] = {}
    bundle = root / "vendor" / "windows-cp314"
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in [manifest["python_installer"], *manifest["packages"]]:
            path = (bundle / item["file"]).resolve()
            if not path.is_relative_to(bundle.resolve()):
                findings.append({"path": item["file"], "reason": "invalid bundle path"})
                continue
            relative = path.relative_to(root.resolve()).as_posix()
            if not path.is_file():
                findings.append({"path": relative, "reason": "missing bundle artifact"})
            elif hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                findings.append({"path": relative, "reason": "bundle checksum mismatch"})
            elif path.stat().st_size > 95 * 1024 * 1024:
                findings.append({"path": relative, "reason": "bundle artifact exceeds 95 MiB"})
            else:
                bundled_files[relative] = item["sha256"]
    home = str(Path.home())
    development_checkout = os.environ.get(
        "COMSOL_MCP_DEVELOPMENT_CHECKOUT",
        str(Path.home() / "Documents" / "COMSOL 2"),
    )
    files = candidate_files(root)
    for path in files:
        relative = path.relative_to(root)
        if any(part in FORBIDDEN_DIRECTORIES for part in relative.parts):
            findings.append({"path": str(relative), "reason": "forbidden directory"})
        suffixes = {suffix.lower() for suffix in path.suffixes}
        if suffixes & FORBIDDEN_SUFFIXES:
            findings.append({"path": str(relative), "reason": "forbidden file type"})
        if path.stat().st_size > MAX_FILE_BYTES and relative.as_posix() not in bundled_files:
            findings.append({"path": str(relative), "reason": "file exceeds 5 MiB"})
        if path.stat().st_size <= 2 * 1024 * 1024:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if home and home in text:
                findings.append({"path": str(relative), "reason": "contains user home path"})
            if development_checkout and development_checkout in text:
                findings.append(
                    {"path": str(relative), "reason": "contains development checkout path"}
                )
    return {
        "success": not findings,
        "root": str(root.resolve()),
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "findings": findings,
        "files": [str(path.relative_to(root)) for path in files],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = audit_tree(args.root.resolve())
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
