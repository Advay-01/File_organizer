"""Check every native installer against its per-platform checksum manifest."""
import hashlib
import json
from pathlib import Path
import sys
from zipfile import BadZipFile, ZipFile

expected = {'windows-x64': {'.zip': 1}, 'linux-x64': {'.deb': 1, '.AppImage': 1},
            'macos-arm64': {'.dmg': 1}}


def verify_assets(folder: Path, version: str) -> int:
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
            if platform == 'windows-x64':
                try:
                    with ZipFile(asset) as archive:
                        if archive.namelist() != [f'Sortly_{version}_x64-setup.exe'] or archive.testzip() is not None:
                            raise SystemExit('Windows ZIP must contain exactly the tested setup executable.')
                except BadZipFile as error:
                    raise SystemExit('Windows installer ZIP is invalid.') from error
            counts[asset.suffix] = counts.get(asset.suffix, 0) + 1
            if name in verified:
                raise SystemExit(f'Duplicate installer: {name}')
            verified.add(name)
        if counts != extensions:
            raise SystemExit(f'Incomplete platform: {platform}: {counts}')
    allowed = verified | {f'SHA256SUMS-{platform}.txt' for platform in expected}
    if {path.name for path in folder.iterdir()} != allowed:
        raise SystemExit('Unexpected files in release assets')
    return len(verified)


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / 'package.json').read_text(encoding='utf-8'))['version']
    count = verify_assets(Path(sys.argv[1]).resolve(), version)
    print(f'Verified all {count} native installers for {version}.')
