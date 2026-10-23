$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$toolsRoot = Join-Path $projectRoot "src-tauri\target\.tauri"
$nsisRoot = Join-Path $toolsRoot "NSIS"
$makensis = Join-Path $nsisRoot "makensis.exe"
$plugin = Join-Path $nsisRoot "Plugins\x86-unicode\additional\nsis_tauri_utils.dll"

function Get-Sha1([string]$Path) {
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        return ([System.BitConverter]::ToString($sha1.ComputeHash($stream))).Replace("-", "")
    } finally {
        $stream.Dispose()
        $sha1.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $makensis -PathType Leaf)) {
    $downloadRoot = Join-Path $projectRoot ".tools"
    $archive = Join-Path $downloadRoot "nsis-3.13.zip"
    $extractRoot = Join-Path $downloadRoot "nsis-3.13"
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null

    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        Invoke-WebRequest `
            -Uri "https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.13/nsis-3.13.zip" `
            -OutFile $archive `
            -TimeoutSec 180
    }

    $archiveHash = Get-Sha1 $archive
    if ($archiveHash -ne "DB14F8AF2EA346B786C6AE19343F61AAF5E09B8E") {
        throw "NSIS archive checksum mismatch."
    }
    if (-not (Test-Path -LiteralPath $extractRoot -PathType Container)) {
        Expand-Archive -LiteralPath $archive -DestinationPath $extractRoot
    }

    $expandedNsis = Get-ChildItem -LiteralPath $extractRoot -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "makensis.exe") } |
        Select-Object -First 1
    if (-not $expandedNsis) {
        throw "NSIS archive did not contain makensis.exe."
    }

    New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $nsisRoot)) {
        Copy-Item -LiteralPath $expandedNsis.FullName -Destination $nsisRoot -Recurse
    }
}

if (-not (Test-Path -LiteralPath $makensis -PathType Leaf)) {
    throw "NSIS preparation failed: makensis.exe is missing."
}

$pluginDirectory = Split-Path -Parent $plugin
New-Item -ItemType Directory -Path $pluginDirectory -Force | Out-Null
if (-not (Test-Path -LiteralPath $plugin -PathType Leaf)) {
    Invoke-WebRequest `
        -Uri "https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll" `
        -OutFile $plugin `
        -TimeoutSec 180
}

$pluginHash = Get-Sha1 $plugin
if ($pluginHash -ne "75197FEE3C6A814FE035788D1C34EAD39349B860") {
    throw "NSIS plugin checksum mismatch."
}

Write-Output "NSIS tools ready: $nsisRoot"
