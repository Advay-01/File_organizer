$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot "dist\sortly-api-x86_64-pc-windows-msvc.exe"
$destinationDir = Join-Path $projectRoot "src-tauri\binaries"
$destination = Join-Path $destinationDir "sortly-api-x86_64-pc-windows-msvc.exe"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Python sidecar not found at $source. Build it with PyInstaller first."
}

New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $destination -Force
Write-Output "Staged sidecar: $destination"
