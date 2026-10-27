"""Build and stage the Python service on its native OS and architecture.

Install requirements-build.txt in the active Python environment first.
PyInstaller cannot cross-compile; run this on each target OS.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def stage(source: Path, destination: Path) -> None:
    target = destination.resolve()
    if not target.is_relative_to(ROOT) or target == ROOT:
        raise ValueError(f"Staging destination is outside this project: {target}")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args()
    if not args.stage_only:
        subprocess.run([
            sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
            '--distpath', str(ROOT / 'dist'),
            '--workpath', str(ROOT / 'build/sortly-sidecar'),
            str(ROOT / 'sortly-sidecar.spec'),
        ], cwd=ROOT, check=True)
    source = ROOT / 'dist/sortly-api'
    name = 'sortly-api.exe' if sys.platform == 'win32' else 'sortly-api'
    if not (source / name).is_file():
        raise FileNotFoundError(f'Native sidecar is missing: {source / name}')
    stage(source, ROOT / 'src-tauri/binaries/sortly-api')
    # Supports running the unbundled release executable as well as installers.
    stage(source, ROOT / 'src-tauri/target/release/binaries/sortly-api')
    print(f'Staged native service: {name}')


if __name__ == '__main__':
    main()
