# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


PROJECT_ROOT = Path(SPECPATH).resolve()

# Uvicorn loads the selected loop, lifespan, and HTTP protocol modules
# dynamically. LiteLLM discovers provider integrations dynamically, so retain
# its complete provider surface for models not handled by direct HTTP.
hiddenimports = (
    collect_submodules("uvicorn.loops")
    + collect_submodules("uvicorn.lifespan")
    + collect_submodules("uvicorn.protocols")
    + collect_submodules("pypdf._crypt_providers")
    + collect_submodules("litellm")
)
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
