"""Check every native installer against its per-platform checksum manifest."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
version = json.loads((root / 'package.json').read_text(encoding='utf-8'))['version']
folder = Path(sys.argv[1]).resolve()
expected = {'windows-x64': {'.exe': 1}, 'linux-x64': {'.deb': 1, '.AppImage': 1},
            'macos-arm64': {'.dmg': 1}}
verified = set()
for platform, extensions in expected.items():
    manifest = folder / f'SHA256SUMS-{platform}.txt'
    counts = {}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        digest, name = line.split('  ', 1)
        if Path(name).name != name or version not in name:
            raise SystemExit(f'Invalid installer name: {name}')
        asset = folder / name
        if hashlib.sha256(asset.read_bytes()).hexdigest() != digest:
            raise SystemExit(f'Checksum mismatch: {name}')
        counts[asset.suffix] = counts.get(asset.suffix, 0) + 1
        if name in verified:
            raise SystemExit(f'Duplicate installer: {name}')
        verified.add(name)
    if counts != extensions:
        raise SystemExit(f'Incomplete platform: {platform}: {counts}')
allowed = verified | {f'SHA256SUMS-{platform}.txt' for platform in expected}
if {path.name for path in folder.iterdir()} != allowed:
    raise SystemExit('Unexpected files in release assets')
print(f'Verified all {len(verified)} native installers for {version}.')
