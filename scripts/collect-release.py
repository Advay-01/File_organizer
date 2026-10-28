"""Collect only native installers and a per-platform SHA-256 manifest for CI."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def collect(platform: str, root: Path = ROOT) -> None:
    version = json.loads((root / 'package.json').read_text(encoding='utf-8'))['version']
    for name in ('frontend/package.json', 'src-tauri/tauri.conf.json'):
        assert json.loads((root / name).read_text(encoding='utf-8'))['version'] == version
    patterns = {'windows-x64': ['nsis/*-setup.exe'], 'linux-x64': ['deb/*.deb', 'appimage/*.AppImage'],
                'macos-arm64': ['dmg/*.dmg']}[platform]
    output = root / 'release-assets'
    output.mkdir(exist_ok=True)
    manifest = []
    for pattern in patterns:
        matches = list((root / 'src-tauri/target/release/bundle').glob(pattern))
        assert len(matches) == 1, f'Expected one {pattern}, found {matches}'
        asset = matches[0]
        assert version in asset.name
        if platform == 'windows-x64':
            zipped = output / f'Sortly_{version}_windows-x64.zip'
            with ZipFile(zipped, 'w', compression=ZIP_DEFLATED) as archive:
                archive.write(asset, arcname=asset.name)
            asset = zipped
        else:
            shutil.copy2(asset, output / asset.name)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        manifest.append(f'{digest}  {asset.name}')
    (output / f'SHA256SUMS-{platform}.txt').write_text('\n'.join(manifest) + '\n', encoding='utf-8')
    print('\n'.join(manifest))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=['windows-x64', 'linux-x64', 'macos-arm64'], required=True)
    collect(parser.parse_args().platform)
