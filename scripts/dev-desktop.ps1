$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$cargoHome = Join-Path $projectRoot ".cargo"
$cargo = Join-Path $cargoHome "bin\cargo.exe"
$rustupHome = Join-Path $projectRoot ".rustup"
$tauri = Join-Path $projectRoot "node_modules\.bin\tauri.cmd"
$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"

if (-not (Test-Path -LiteralPath $cargo -PathType Leaf)) {
    throw "Project-local Rust is missing. Run the documented rustup setup first."
}
if (-not (Test-Path -LiteralPath $tauri -PathType Leaf)) {
    throw "Tauri CLI is missing. Run npm install in the project root."
}

$vcvars = & $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -find "VC\Auxiliary\Build\vcvars64.bat" |
    Select-Object -First 1
if (-not $vcvars) {
    throw "Visual Studio C++ Build Tools were not found."
}

& (Join-Path $PSScriptRoot "stage-sidecar.ps1")

$command = (
    'call "' + $vcvars + '" >nul && ' +
    'set "RUSTUP_HOME=' + $rustupHome + '" && ' +
    'set "CARGO_HOME=' + $cargoHome + '" && ' +
    'set "PATH=' + $cargoHome + '\bin;%PATH%" && ' +
    'cd /d "' + $projectRoot + '" && ' +
    '"' + $tauri + '" dev'
)
& cmd.exe /d /s /c $command
exit $LASTEXITCODE
