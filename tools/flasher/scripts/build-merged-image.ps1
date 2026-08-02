# =============================================================================
# Maintainer-only: build one AmpHive gateway chip target and merge its build
# output (bootloader + partition table + app) into a single "merged" binary
# suitable for tools/flasher, which writes it at flash offset 0x0 with
# `esptool write-flash` — no ESP-IDF, no compiler, on the end user's machine.
#
# Requires a working ESP-IDF environment ALREADY ACTIVE in this shell (run
# the IDF `export.ps1`/`export.bat` first). This script deliberately never
# hardcodes an IDF install path — even though the owner's box happens to have
# IDF v5.3.3 at C:\esp\v5.3.3\esp-idf, that path is not this script's
# business; it only checks that IDF_PATH is already set.
#
# Usage:
#   . C:\esp\v5.3.3\esp-idf\export.ps1      # once per shell — your path, not this script's
#   .\tools\flasher\scripts\build-merged-image.ps1 -Chip esp32c3
#   .\tools\flasher\scripts\build-merged-image.ps1 -Chip esp32c3 -OutDir tools\flasher\firmware-images
#
# Output filename matches what amphive_flasher/images.py expects:
#   amphive-gateway-<chip>-merged.bin
# =============================================================================
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("esp32", "esp32c3")]
    [string]$Chip,

    [string]$OutDir = (Join-Path $PSScriptRoot "..\firmware-images")
)

$ErrorActionPreference = "Stop"

if (-not $env:IDF_PATH) {
    Write-Host "ERROR: IDF_PATH is not set in this shell." -ForegroundColor Red
    Write-Host "Activate your ESP-IDF environment first, e.g.:" -ForegroundColor Yellow
    Write-Host '  . C:\path\to\your\esp-idf\export.ps1' -ForegroundColor Yellow
    Write-Host "(this script intentionally never hardcodes that path itself)" -ForegroundColor Yellow
    exit 1
}

if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: idf.py not found on PATH, even though IDF_PATH is set ($env:IDF_PATH)." -ForegroundColor Red
    Write-Host "Re-run the IDF export script for this shell." -ForegroundColor Yellow
    exit 1
}

if ($Chip -eq "esp32") {
    Write-Host "NOTE: firmware/sdkconfig.defaults is currently tuned for ESP32-C3 (no PSRAM," -ForegroundColor Yellow
    Write-Host "dual-OTA partitions sized for that target) - see docs/FIRMWARE.md. A plain" -ForegroundColor Yellow
    Write-Host "'esp32' target may need its own sdkconfig work before this produces a real," -ForegroundColor Yellow
    Write-Host "flashable image. Proceeding anyway; this is only a heads-up." -ForegroundColor Yellow
}

$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$firmwareDir = Join-Path $repoRoot "firmware"
$cmakeLists = Join-Path $firmwareDir "CMakeLists.txt"
if (-not (Test-Path $cmakeLists)) {
    Write-Host "ERROR: $firmwareDir doesn't look like the firmware project (no CMakeLists.txt)." -ForegroundColor Red
    exit 1
}

$verLine = Select-String -Path $cmakeLists -Pattern 'set\(PROJECT_VER\s+"([^"]+)"\)'
$version = if ($verLine) { $verLine.Matches[0].Groups[1].Value } else { "unknown" }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$outFile = Join-Path $OutDir "amphive-gateway-$Chip-merged.bin"

Write-Host "Building firmware for $Chip (fw $version) ..." -ForegroundColor Cyan
Push-Location $firmwareDir
try {
    idf.py set-target $Chip
    if ($LASTEXITCODE -ne 0) { throw "idf.py set-target $Chip failed" }

    idf.py build
    if ($LASTEXITCODE -ne 0) { throw "idf.py build failed" }

    idf.py merge-bin -o $outFile
    if ($LASTEXITCODE -ne 0) { throw "idf.py merge-bin failed" }
}
finally {
    Pop-Location
}

if (-not (Test-Path $outFile)) {
    Write-Host "ERROR: expected output not found at $outFile" -ForegroundColor Red
    exit 1
}

$sizeMB = [math]::Round((Get-Item $outFile).Length / 1MB, 2)
Write-Host ""
Write-Host "OK: $outFile ($sizeMB MB, fw $version)" -ForegroundColor Green
Write-Host "Next: attach it to a GitHub release (tools/flasher can download it automatically)," -ForegroundColor Green
Write-Host "or drop it straight into tools/flasher/firmware-images/ for local testing." -ForegroundColor Green
