$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$cargoHome = Join-Path $projectRoot ".cargo"
$rustupHome = Join-Path $projectRoot ".rustup"
$toolsRoot = Join-Path $projectRoot ".tools"
$rustupInit = Join-Path $toolsRoot "rustup-init.exe"
$rustup = Join-Path $cargoHome "bin\rustup.exe"

New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $rustupInit -PathType Leaf)) {
    Invoke-WebRequest -Uri "https://win.rustup.rs/x86_64" -OutFile $rustupInit -TimeoutSec 180
}

$env:RUSTUP_HOME = $rustupHome
$env:CARGO_HOME = $cargoHome
$env:RUSTUP_INIT_SKIP_PATH_CHECK = "yes"

if (-not (Test-Path -LiteralPath $rustup -PathType Leaf)) {
    & $rustupInit `
        -y `
        --no-modify-path `
        --profile minimal `
        --default-toolchain stable-x86_64-pc-windows-msvc
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

& $rustup component add rustfmt
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& (Join-Path $cargoHome "bin\rustc.exe") --version
Write-Output "Project-local Rust ready: $cargoHome"
