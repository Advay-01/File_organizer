"""Regression tests for the Windows desktop packaging workflow."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = (PROJECT_ROOT / "scripts" / "build-desktop.ps1").read_text(
    encoding="utf-8"
)
STAGE_SCRIPT = (PROJECT_ROOT / "scripts" / "stage-sidecar.ps1").read_text(
    encoding="utf-8"
)
TAURI_CONFIG = json.loads(
    (PROJECT_ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
)


class DesktopBuildWorkflowTests(unittest.TestCase):
    def test_build_uses_project_venv_for_install_and_pyinstaller(self):
        self.assertIn(
            '$pyInstallerPython = Join-Path $projectRoot ".pyinstaller-venv\\Scripts\\python.exe"',
            BUILD_SCRIPT,
        )
        pip_install = BUILD_SCRIPT.index(
            "& $pyInstallerPython -m pip install"
        )
        pyinstaller = BUILD_SCRIPT.index(
            "& $pyInstallerPython -m PyInstaller --clean"
        )
        self.assertLess(pip_install, pyinstaller)
        self.assertNotIn(
            "& $python.Source -m pip install",
            BUILD_SCRIPT,
        )

    def test_dependency_installation_and_sidecar_build_fail_fast(self):
        self.assertIn('$ErrorActionPreference = "Stop"', BUILD_SCRIPT)
        self.assertIn(
            'throw "Could not install Python sidecar and build dependencies."',
            BUILD_SCRIPT,
        )
        self.assertIn(
            'throw "PyInstaller sidecar build failed with exit code $LASTEXITCODE."',
            BUILD_SCRIPT,
        )
        self.assertIn(
            'throw "Sidecar build completed without producing $builtSidecar."',
            BUILD_SCRIPT,
        )

    def test_sidecar_is_rebuilt_before_staging_and_staging_is_validated(self):
        build = BUILD_SCRIPT.index(
            "& $pyInstallerPython -m PyInstaller --clean"
        )
        stage = BUILD_SCRIPT.index(
            '& (Join-Path $PSScriptRoot "stage-sidecar.ps1")'
        )
        self.assertLess(build, stage)
        self.assertIn(
            'if (-not (Test-Path -LiteralPath $destination -PathType Leaf))',
            STAGE_SCRIPT,
        )
        self.assertIn(
            'if (-not (Test-Path -LiteralPath $stagedSidecar -PathType Leaf))',
            BUILD_SCRIPT,
        )

    def test_cleanup_is_limited_to_explicit_generated_paths(self):
        cleanup_paths = re.findall(
            r'Remove-GeneratedPath \(Join-Path \$projectRoot "([^"]+)"\)',
            BUILD_SCRIPT,
        )
        self.assertEqual(
            cleanup_paths,
            [
                r"build\sortly-sidecar",
                r"dist\sortly-api",
                r"dist\sortly-api-x86_64-pc-windows-msvc.exe",
                r"src-tauri\binaries\sortly-api",
                r"src-tauri\target\release\binaries\sortly-api",
                r"src-tauri\target\release\bundle\nsis",
            ],
        )
        self.assertNotIn(
            'Remove-GeneratedPath $projectRoot',
            BUILD_SCRIPT,
        )
        self.assertIn(
            "Refusing to remove a path outside the project",
            BUILD_SCRIPT,
        )

    def test_tauri_packages_frontend_and_freshly_staged_sidecar(self):
        self.assertEqual(
            TAURI_CONFIG["build"]["beforeBuildCommand"],
            "npm --prefix frontend run build",
        )
        self.assertIn(
            "binaries/sortly-api/",
            TAURI_CONFIG["bundle"]["resources"],
        )
        stage = BUILD_SCRIPT.index(
            '& (Join-Path $PSScriptRoot "stage-sidecar.ps1")'
        )
        tauri_build = BUILD_SCRIPT.index("""'"' + $tauri + '" build'""")
        self.assertLess(stage, tauri_build)
        self.assertLess(
            BUILD_SCRIPT.index(
                'if (-not (Test-Path -LiteralPath $stagedSidecar -PathType Leaf))'
            ),
            tauri_build,
        )


if __name__ == "__main__":
    unittest.main()
