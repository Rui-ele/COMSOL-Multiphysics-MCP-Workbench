"""Distribution bootstrap, doctor, and release-boundary tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.bootstrap import install_target, planned_commands
from scripts.release_audit import audit_tree
from src import doctor


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_extras_are_explicit(tmp_path):
    assert install_target(dev=False) == "."
    assert install_target(dev=True) == ".[dev]"
    commands = planned_commands(
        python=Path(sys.executable),
        venv=tmp_path / ".venv",
        dev=True,
        upgrade_pip=False,
    )
    assert commands[-1][-2:] == ["-e", ".[dev]"]


def test_bootstrap_dry_run_has_no_side_effects(tmp_path):
    venv = tmp_path / "dry-run-venv"
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "bootstrap.py"),
            "--venv",
            str(venv),
            "--dry-run",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["commands"]
    assert not venv.exists()


def test_doctor_honors_explicit_comsol_root(tmp_path, monkeypatch):
    root = tmp_path / "Multiphysics"
    arch = doctor.platform_architecture()
    executable = "comsol.exe" if os.name == "nt" else "comsol"
    (root / "bin" / arch).mkdir(parents=True)
    (root / "bin" / arch / executable).touch()
    (root / "plugins").mkdir()
    (root / "apiplugins").mkdir()
    monkeypatch.setenv("COMSOL_MCP_COMSOL_ROOT", str(root))
    assert doctor.find_comsol_root("9.9") == root.resolve()


def test_release_tree_has_no_local_artifacts():
    report = audit_tree(PROJECT_ROOT)
    assert report["success"], report["findings"]


def test_release_audit_ignores_generated_egg_info(tmp_path):
    generated = tmp_path / "package.egg-info"
    generated.mkdir()
    (generated / "PKG-INFO").write_text("generated metadata", encoding="utf-8")
    report = audit_tree(tmp_path)
    assert report["files"] == []


def test_release_audit_ignores_local_real_test_evidence(tmp_path):
    generated = tmp_path / ".pytest-real"
    generated.mkdir()
    (generated / "evidence.json").write_text("{}", encoding="utf-8")
    report = audit_tree(tmp_path)
    assert report["files"] == []
