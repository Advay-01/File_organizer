# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


PROJECT_ROOT = Path(SPECPATH).resolve()

# Uvicorn loads protocol modules dynamically. Litellm discovers provider modules
# dynamically too, so include both sets to preserve existing provider behavior.
hiddenimports = collect_submodules("uvicorn") + collect_submodules("litellm")
datas = collect_data_files("litellm")

a = Analysis(
    [str(PROJECT_ROOT / "app" / "desktop_server.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sortly-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="sortly-api",
)
