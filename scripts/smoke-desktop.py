"""Check native Tauri startup and its bundled API using an empty temporary folder.

CI requires psutil for cleaning up only this test's process tree. On Linux run
under xvfb-run. No configured provider or user files are used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

import psutil


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sortly-desktop-check-') as temporary:
        state = Path(temporary)
        empty = state / 'empty'
        empty.mkdir()
        env_file = state / 'empty.env'
        env_file.write_text('PYTHON_DOTENV_DISABLED=1\n', encoding='utf-8')
        env = dict(os.environ, SORTLY_DATA_DIR=str(state / 'data'),
                   SORTLY_ENV_FILE=str(env_file), SORTLY_STARTUP_DIAGNOSTICS='1',
                   PYTHON_DOTENV_DISABLED='1', WEBKIT_DISABLE_DMABUF_RENDERER='1')
        log_path = args.output.with_suffix('.log')
        with log_path.open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(executable)], cwd=executable.parent,
                                       env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f'Desktop exited: {log_path.read_text(encoding="utf-8", errors="replace")}')
                    text = log_path.read_text(encoding='utf-8', errors='replace')
                    ready = re.search(r'API health succeeded on port (\d+)', text)
                    if ready:
                        break
                    time.sleep(.25)
                else:
                    raise TimeoutError(f'Desktop did not start its API: {log_path.read_text(encoding="utf-8", errors="replace")}')
                base = f'http://127.0.0.1:{ready.group(1)}'
                with urlopen(base + '/api/health', timeout=5) as response:
                    assert response.status == 200
                request = Request(base + '/api/preview',
                                  data=json.dumps({'source': str(empty)}).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=10) as response:
                    plan = json.load(response)
                assert plan['files'] == []
                report = {'desktop': str(executable), 'bundled_api_ready': True,
                          'empty_folder_analysis': True, 'platform': os.sys.platform}
                args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
                print(json.dumps(report, indent=2))
            finally:
                try:
                    owned = psutil.Process(process.pid)
                    children = owned.children(recursive=True)
                    owned.terminate()
                    for child in children:
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    _, alive = psutil.wait_procs([owned, *children], timeout=8)
                    for child in alive:
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    process.wait(timeout=10)
                except psutil.NoSuchProcess:
                    pass


if __name__ == '__main__':
    main()
