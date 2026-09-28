"""Check the installation materials without executing Windows or COMSOL."""

from __future__ import annotations

from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from scripts.bootstrap import (
    BUNDLE_ROOT,
    PROJECT_ROOT,
    planned_commands,
    require_bundle_interpreter,
    verify_bundle,
)
from scripts.release_audit import audit_tree


class InstallationBundleTests(unittest.TestCase):
    def test_bundle_has_all_verified_artifacts(self):
        verify_bundle(BUNDLE_ROOT)
        manifest = json.loads((BUNDLE_ROOT / "manifest.json").read_text())
        installer = manifest["python_installer"]
        self.assertEqual(hashlib.sha256((BUNDLE_ROOT / installer["file"]).read_bytes()).hexdigest(), installer["sha256"])
        expected = {Path(p["file"]).name for p in manifest["packages"]}
        self.assertEqual(expected, {p.name for p in (BUNDLE_ROOT / "wheels").glob("*.whl")})

    def test_missing_or_corrupt_wheel_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"packages": [{"file": "example.whl", "sha256": hashlib.sha256(b"expected").hexdigest()}]}
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Missing"):
                verify_bundle(root)
            (root / "example.whl").write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "Checksum"):
                verify_bundle(root)

    def test_offline_plan_uses_only_local_materials(self):
        commands = planned_commands(python=Path("python.exe"), venv=Path("environment"), dev=False,
                                    upgrade_pip=False, bundle=BUNDLE_ROOT)
        self.assertEqual(len(commands), 3)
        for command in commands[1:]:
            self.assertIn("--no-index", command)
        self.assertIn("--require-hashes", commands[1])
        self.assertIn("--no-build-isolation", commands[2])
        self.assertIn("--no-deps", commands[2])
        with self.assertRaises(ValueError):
            planned_commands(python=Path("python.exe"), venv=Path("environment"), dev=True,
                             upgrade_pip=False, bundle=BUNDLE_ROOT)

    def test_wrong_python_is_rejected_before_install(self):
        expected = dict(system="Windows", version=[3, 14], implementation="CPython",
                        bits=64, machine="AMD64", free_threaded=False)
        require_bundle_interpreter(expected)
        for changed in ({"version": [3, 13]}, {"free_threaded": True}, {"bits": 32},
                        {"machine": "ARM64"}, {"system": "Darwin"}):
            with self.assertRaises(ValueError):
                require_bundle_interpreter({**expected, **changed})

    def test_default_online_dry_run_omits_dev_and_pip_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "environment"
            result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/bootstrap.py"),
                                     "--online", "--venv", str(env), "--dry-run"],
                                    text=True, capture_output=True, check=True)
            commands = json.loads(result.stdout)["commands"]
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[-1][-2:], ["-e", "."])
            self.assertFalse(env.exists())

    def test_windows_dependency_closure_and_wheel_tags(self):
        # Use the pure-Python packaging wheel already shipped in the bundle.
        packaging_wheel = next((BUNDLE_ROOT / "wheels").glob("packaging-*.whl"))
        sys.path.insert(0, str(packaging_wheel))
        try:
            from packaging.markers import default_environment
            from packaging.requirements import Requirement
            from packaging.specifiers import SpecifierSet
            from packaging.tags import compatible_tags, cpython_tags
            from packaging.utils import canonicalize_name, parse_wheel_filename
        finally:
            sys.path.remove(str(packaging_wheel))
        environment = default_environment()
        environment.update(python_version="3.14", python_full_version="3.14.5", os_name="nt",
                           sys_platform="win32", platform_system="Windows", platform_machine="AMD64",
                           implementation_name="cpython", platform_python_implementation="CPython",
                           implementation_version="3.14.5")
        allowed = set(cpython_tags((3, 14), abis=["cp314"], platforms=["win_amd64"]))
        allowed.update(compatible_tags((3, 14), interpreter="cp314", platforms=["win_amd64"]))
        packages = {}
        manifest = json.loads((BUNDLE_ROOT / "manifest.json").read_text())
        lock_lines = (BUNDLE_ROOT / "requirements.txt").read_text().splitlines()
        for item in manifest["packages"]:
            path = BUNDLE_ROOT / item["file"]
            self.assertTrue(parse_wheel_filename(path.name)[3] & allowed, path.name)
            with ZipFile(path) as archive:
                metadata_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
                metadata = BytesParser().parsebytes(archive.read(metadata_name))
            self.assertIn("3.14.5", SpecifierSet(metadata.get("Requires-Python", "")), path.name)
            self.assertEqual(metadata["Name"], item["name"])
            self.assertEqual(metadata["Version"], item["version"])
            packages[canonicalize_name(metadata["Name"])] = metadata
            self.assertIn(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}", lock_lines)
        queue = [(name, "") for name in ["mcp", "mph", "pydantic", "setuptools", "wheel"]]
        visited = set()
        while queue:
            name, extra = queue.pop()
            if (name, extra) in visited:
                continue
            visited.add((name, extra))
            self.assertIn(name, packages)
            for value in packages[name].get_all("Requires-Dist", []):
                requirement = Requirement(value)
                if requirement.marker and not requirement.marker.evaluate({**environment, "extra": extra}):
                    continue
                dependency = canonicalize_name(requirement.name)
                self.assertIn(dependency, packages, f"{name} needs {requirement}")
                self.assertIn(packages[dependency]["Version"], requirement.specifier)
                queue.append((dependency, ""))
                queue.extend((dependency, entry) for entry in requirement.extras)
        self.assertEqual(set(packages), {name for name, extra in visited})

    def test_release_audit_accepts_verified_bundle(self):
        report = audit_tree(PROJECT_ROOT)
        self.assertTrue(report["success"], report["findings"])


if __name__ == "__main__":
    unittest.main()
