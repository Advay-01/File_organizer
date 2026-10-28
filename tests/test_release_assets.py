"""Release ZIP and checksum checks use tiny generated installer fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def assets(tmp_path):
    for file in ("package.json", "frontend/package.json", "src-tauri/tauri.conf.json"):
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": "2.0.0"}))
    bundle = tmp_path / "src-tauri/target/release/bundle"
    for name in ("nsis/Sortly_2.0.0_x64-setup.exe", "deb/Sortly_2.0.0_amd64.deb", "appimage/Sortly_2.0.0_amd64.AppImage", "dmg/Sortly_2.0.0_aarch64.dmg"):
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Generated installer fixture: " + name.encode())
    collect = script("collect-release")
    for platform in ("windows-x64", "linux-x64", "macos-arm64"):
        collect.collect(platform, tmp_path)
    return tmp_path / "release-assets"


def test_windows_zip_contains_the_exact_built_setup_and_all_platform_hashes_verify(assets):
    zipped = assets / "Sortly_2.0.0_windows-x64.zip"
    with ZipFile(zipped) as archive:
        assert archive.namelist() == ["Sortly_2.0.0_x64-setup.exe"]
        expected = assets.parent / "src-tauri/target/release/bundle/nsis/Sortly_2.0.0_x64-setup.exe"
        assert archive.read(archive.namelist()[0]) == expected.read_bytes()
    assert not list(assets.glob("*.exe"))
    assert script("verify-release-artifacts").verify_assets(assets, "2.0.0") == 4


def test_release_rejects_a_changed_download(assets):
    (assets / "Sortly_2.0.0_windows-x64.zip").write_bytes(b"Corrupted download")
    with pytest.raises(SystemExit, match="Checksum mismatch"):
        script("verify-release-artifacts").verify_assets(assets, "2.0.0")


@pytest.mark.parametrize("member", ["../setup.exe", "Sortly_1.0.10_x64-setup.exe", "sortly.exe"])
def test_release_rejects_wrong_or_nested_zip_payload_even_with_valid_outer_hash(assets, member):
    zipped = assets / "Sortly_2.0.0_windows-x64.zip"
    with ZipFile(zipped, "w") as archive:
        archive.writestr(member, b"Fixture")
    digest = hashlib.sha256(zipped.read_bytes()).hexdigest()
    (assets / "SHA256SUMS-windows-x64.txt").write_text(f"{digest}  {zipped.name}\n")
    with pytest.raises(SystemExit, match="exactly the tested setup"):
        script("verify-release-artifacts").verify_assets(assets, "2.0.0")


def test_release_rejects_extra_unverified_assets(assets):
    (assets / "unexpected.exe").write_bytes(b"Fixture")
    with pytest.raises(SystemExit, match="Unexpected files"):
        script("verify-release-artifacts").verify_assets(assets, "2.0.0")


def test_application_versions_agree():
    import re

    version = json.loads((ROOT / "package.json").read_text())["version"]
    for file in ("frontend/package.json", "src-tauri/tauri.conf.json"):
        assert json.loads((ROOT / file).read_text())["version"] == version
    for file in ("package-lock.json", "frontend/package-lock.json"):
        data = json.loads((ROOT / file).read_text())
        assert data["version"] == data["packages"][""]["version"] == version
    assert f'version = "{version}"' in (ROOT / "src-tauri/Cargo.toml").read_text()
    assert re.search(r'name = "sortly"\nversion = "' + re.escape(version) + '"', (ROOT / "src-tauri/Cargo.lock").read_text())
