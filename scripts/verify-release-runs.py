"""Require successful native jobs and matching product source before publishing.

CI-only and documentation changes can follow a tested build. Application source,
dependencies, packaging config for that OS, and smoke checks must match exactly.
"""
import json
import os
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
common = ['app', 'frontend', 'src-tauri/src', 'src-tauri/icons', 'src-tauri/capabilities',
          'src-tauri/build.rs', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock',
          'src-tauri/tauri.conf.json', 'sortly-sidecar.spec', 'requirements.txt',
          'requirements-build.txt', 'requirements-dev.txt', 'package.json', 'package-lock.json',
          'scripts/build-sidecar.py', 'scripts/smoke-sidecar.py', 'scripts/smoke-desktop.py', 'tests']
for platform, variable in [('windows-x64', 'WINDOWS_RUN'), ('linux-x64', 'LINUX_RUN'),
                           ('macos-arm64', 'MACOS_RUN'), ('macos-x64', 'MACOS_RUN')]:
    run_id = os.environ[variable]
    if not re.fullmatch(r'\d+', run_id):
        raise SystemExit('Run IDs must be numeric')
    run = json.loads(subprocess.check_output(
        ['gh', 'run', 'view', run_id, '--json', 'headSha,jobs'], text=True, cwd=root))
    job = next((job for job in run['jobs'] if job['name'] == f'Build {platform}'), None)
    if not job or job['conclusion'] != 'success':
        raise SystemExit(f'{platform} has no successful native build in run {run_id}')
    config = 'macos' if platform.startswith('macos') else platform.split('-')[0]
    subprocess.run(['git', 'diff', '--exit-code', run['headSha'], 'HEAD', '--',
                    *common, f'src-tauri/tauri.{config}.conf.json'], cwd=root, check=True)
    print(f'{platform}: successful native checks; matching product source ({run_id}).')
