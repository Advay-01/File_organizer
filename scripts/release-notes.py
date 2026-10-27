"""Print the matching changelog entry for a validated release tag."""
import json
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
version = json.loads((root / 'package.json').read_text(encoding='utf-8'))['version']
if sys.argv[1:] != [f'v{version}'] or not re.fullmatch(r'\d+\.\d+\.\d+', version):
    raise SystemExit('Release tag must match package.json version.')
changelog = (root / 'CHANGELOG.md').read_text(encoding='utf-8')
entry = changelog.split(f'## {version} - ', 1)[1].split('\n## ', 1)[0]
print(entry.split('\n', 1)[1].strip())
print('\nInstallers include the local API; users do not need Python or Node.js. Verify downloads against the matching SHA256SUMS manifest.')
print('\nmacOS builds use ad-hoc signatures and are not Apple notarized. Windows installers are unsigned. Linux builds target Ubuntu 22.04 or newer compatible distributions with WebKitGTK 4.1.')
print(f'\n[README and working demo](https://github.com/Advay-01/Sortly/blob/v{version}/README.md)')
