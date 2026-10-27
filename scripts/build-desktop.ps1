$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$cargoHome = Join-Path $projectRoot ".cargo"
$cargo = Join-Path $cargoHome "bin\cargo.exe"
$rustupHome = Join-Path $projectRoot ".rustup"
$tauri = Join-Path $projectRoot "node_modules\.bin\tauri.cmd"
$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$python = Get-Command python.exe -ErrorAction SilentlyContinue
$pyInstallerPython = Join-Path $projectRoot ".pyinstaller-venv\Scripts\python.exe"
$buildRequirements = Join-Path $projectRoot "requirements-build.txt"

function Remove-GeneratedPath([string]$Path) {
    $fullProjectRoot = [IO.Path]::GetFullPath($projectRoot).TrimEnd("\") + "\"
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($fullProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the project: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath $cargo -PathType Leaf)) {
    throw "Project-local Rust is missing. Run the documented rustup setup first."
}
if (-not (Test-Path -LiteralPath $tauri -PathType Leaf)) {
    throw "Tauri CLI is missing. Run npm install in the project root."
}
if (-not $python) {
    throw "Python is missing from PATH. Install Python 3.10 or newer before building."
}
& $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10 or newer is required to build the sidecar."
}
if (-not (Test-Path -LiteralPath $pyInstallerPython -PathType Leaf)) {
    Write-Output "Creating the Python build environment..."
    & $python.Source -m venv (Split-Path -Parent (Split-Path -Parent $pyInstallerPython))
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Python build environment."
    }
}
if (-not (Test-Path -LiteralPath $buildRequirements -PathType Leaf)) {
    throw "Build requirements are missing at $buildRequirements."
}
Write-Output "Installing Python sidecar and build dependencies..."
& $pyInstallerPython -m pip install --disable-pip-version-check --requirement $buildRequirements
if ($LASTEXITCODE -ne 0) {
    throw "Could not install Python sidecar and build dependencies."
}
& $pyInstallerPython -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not available in $pyInstallerPython."
}

$vcvars = & $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -find "VC\Auxiliary\Build\vcvars64.bat" |
    Select-Object -First 1
if (-not $vcvars) {
    throw "Visual Studio C++ Build Tools were not found."
}

Write-Output "Cleaning generated sidecar and installer staging paths..."
Remove-GeneratedPath (Join-Path $projectRoot "build\sortly-sidecar")
Remove-GeneratedPath (Join-Path $projectRoot "dist\sortly-api")
Remove-GeneratedPath (Join-Path $projectRoot "dist\sortly-api-x86_64-pc-windows-msvc.exe")
Remove-GeneratedPath (Join-Path $projectRoot "src-tauri\binaries\sortly-api")
Remove-GeneratedPath (Join-Path $projectRoot "src-tauri\target\release\binaries\sortly-api")
Remove-GeneratedPath (Join-Path $projectRoot "src-tauri\target\release\bundle\nsis")

Write-Output "Building the Python sidecar from source..."
Push-Location $projectRoot
try {
    & $pyInstallerPython -m PyInstaller --clean --noconfirm "sortly-sidecar.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller sidecar build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
$builtSidecar = Join-Path $projectRoot "dist\sortly-api\sortly-api.exe"
if (-not (Test-Path -LiteralPath $builtSidecar -PathType Leaf)) {
    throw "Sidecar build completed without producing $builtSidecar."
}

& (Join-Path $PSScriptRoot "stage-sidecar.ps1")
$stagedSidecar = Join-Path $projectRoot "src-tauri\binaries\sortly-api\sortly-api.exe"
if (-not (Test-Path -LiteralPath $stagedSidecar -PathType Leaf)) {
    throw "Sidecar staging completed without producing $stagedSidecar."
}
& (Join-Path $PSScriptRoot "prepare-nsis.ps1")

$command = (
    'call "' + $vcvars + '" >nul && ' +
    'set "RUSTUP_HOME=' + $rustupHome + '" && ' +
    'set "CARGO_HOME=' + $cargoHome + '" && ' +
    'set "PATH=' + $cargoHome + '\bin;%PATH%" && ' +
    'cd /d "' + $projectRoot + '" && ' +
    '"' + $tauri + '" build'
)
& cmd.exe /d /s /c $command
exit $LASTEXITCODE
