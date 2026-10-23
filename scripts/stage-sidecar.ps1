$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot "dist\sortly-api"
$sourceExecutable = Join-Path $source "sortly-api.exe"
$destinationDir = Join-Path $projectRoot "src-tauri\binaries\sortly-api"
$destination = Join-Path $destinationDir "sortly-api.exe"

if (-not (Test-Path -LiteralPath $sourceExecutable -PathType Leaf)) {
    throw "Onedir Python sidecar not found at $sourceExecutable. Build it with PyInstaller first."
}

New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $destinationDir -Recurse -Force
Write-Output "Staged sidecar: $destination"
