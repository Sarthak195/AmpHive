# =============================================================================
# Publish the built (signed) gateway firmware image to the public OTA bucket.
#
# Uploads firmware/build/amphive-gateway.bin to gs://amphive-fw under a
# versioned name (version read from PROJECT_VER in firmware/CMakeLists.txt),
# verifies the object is fetchable anonymously over HTTPS, and prints the
# OTA-trigger call. It does NOT trigger the OTA itself.
#
# The image must be the SIGNED build output (the default amphive-gateway.bin;
# never amphive-gateway-unsigned.bin) — firmware >= 1.4.0 rejects unsigned
# images. Full runbook: deploy/docs/ota_image_publishing.md.
#
# Usage:
#   .\deploy\scripts\publish_firmware.ps1
#   .\deploy\scripts\publish_firmware.ps1 -Bucket amphive-fw -ImagePath firmware\build\amphive-gateway.bin
# =============================================================================
param(
    [string]$Bucket = "amphive-fw",
    [string]$ImagePath = "firmware\build\amphive-gateway.bin"
)

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$image = Join-Path $repoRoot $ImagePath
$cmake = Join-Path $repoRoot "firmware\CMakeLists.txt"

if (-not (Test-Path $image)) {
    Write-Host "ERROR: $image not found - run idf.py build first." -ForegroundColor Red
    exit 1
}

$verLine = Select-String -Path $cmake -Pattern 'set\(PROJECT_VER\s+"([^"]+)"\)'
if (-not $verLine) {
    Write-Host "ERROR: PROJECT_VER not found in $cmake." -ForegroundColor Red
    exit 1
}
$version = $verLine.Matches[0].Groups[1].Value

# Guard against shipping the unsigned artifact: the signed image is exactly
# 68 bytes (one ECDSA v1 signature block) larger than its -unsigned sibling.
$unsigned = $image -replace '\.bin$', '-unsigned.bin'
if ((Test-Path $unsigned) -and ((Get-Item $image).Length -le (Get-Item $unsigned).Length)) {
    Write-Host "ERROR: $image is not larger than the -unsigned artifact - it looks unsigned." -ForegroundColor Red
    exit 1
}

$object = "amphive-gateway-$version.bin"
$url = "https://storage.googleapis.com/$Bucket/$object"

Write-Host "Uploading $ImagePath (fw $version) to gs://$Bucket/$object ..." -ForegroundColor Cyan
gcloud storage cp $image "gs://$Bucket/$object"
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED - upload did not complete." -ForegroundColor Red
    exit 1
}

# Anonymous HTTPS fetch check - exactly what the device will do.
try {
    $head = Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing
    Write-Host "OK: $url is publicly fetchable ($($head.Headers['Content-Length']) bytes)." -ForegroundColor Green
} catch {
    Write-Host "WARNING: uploaded, but anonymous HTTPS fetch failed: $_" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "Trigger the OTA with:" -ForegroundColor Cyan
Write-Host @"
curl -X POST "http://<backend>/api/cpo/gateways/<gateway_id>/ota" \
    -H "Authorization: Bearer <CPO JWT>" -H "Content-Type: application/json" \
    -d '{"firmware_url":"$url"}'
"@
