# =============================================================================
# Create (or reset) a per-gateway MQTT account on the production broker.
#
# Username MUST equal the gateway_id: the broker ACL line
# `pattern readwrite amphive/gateways/%u/#` scopes each gateway to its own
# subtree (SECURITY.md section 3), so a matching username is what isolates one
# client site from another. Redeploys preserve these accounts (deploy.ps1
# never ships the broker files).
#
# Rewritten 2026-10-20 for the relay VM. The old version ran mosquitto_passwd
# against ~/amphive-relay/mosquitto_passwd and HUP'd a container named
# amphive-mqtt; on the live box the file is ~/amphive-relay/config/
# mosquitto_passwd (owner 1883:1883, mode 600), single-file bind-mounted
# read-only into the broker container amphive-relay-mqtt-1. This script now
# uploads deploy/scripts/relay_set_gateway_password.sh (the logic of the reset
# that was verified live on 2026-10-19) to /tmp on the VM and runs it over
# PLAIN OpenSSH, with the password on STDIN only - never on any command line.
# That script's header explains the broker-side gotchas (blank lines vs.
# mosquitto_passwd, the inode-pinning bind mount, verify-by-subscribe). The
# two that shape THIS file:
#   - Plain OpenSSH, not `gcloud compute ssh`: on Windows gcloud runs plink,
#     which auto-accepts the host key by injecting a "y" line into the remote
#     stdin, so a stdin-fed password would arrive as "y".
#   - On Windows the repo is checked out with CRLF (core.autocrlf) and bash
#     chokes on CR, so an LF-only copy of the script is uploaded.
#
# Usage:
#   .\deploy\scripts\add_gateway_user.ps1 -GatewayId 1cc3abb4fb54
#       Generates a strong password and prints it ONCE; it is not stored
#       anywhere, so put it straight into the gateway: the setup portal's MQTT
#       field on a fresh/unjoined device only (the firmware opens the portal
#       only when it has no saved Wi-Fi or can't join it at boot), otherwise
#       mqtt_pwd for tools/gateway_provision/provision_nvs.py over USB.
#   .\deploy\scripts\add_gateway_user.ps1 -GatewayId 1cc3abb4fb54 -Password <pwd>
#       Uses yours (16-63 chars of [A-Za-z0-9_-]). It then sits in your
#       PowerShell history, which is why omitting -Password is preferred.
#
# For an existing account this RESETS the password: the gateway loses its
# broker login (expect it to be dropped at once) until its own NVS holds the
# new one, so have the USB tool ready. The remote script refuses (exit 9)
# while the database shows an active charging session on the gateway's plugs;
# -Force skips that check (e.g. to lock out a compromised gateway at once).
#
# Requires the Windows OpenSSH client (ssh + scp) and the key gcloud created
# ($HOME\.ssh\google_compute_engine, authorized on the VM through project
# metadata). The first connection records the VM's host key
# (StrictHostKeyChecking=accept-new); a CHANGED host key still fails loudly.
# The software agent uses the same account via .env AMPHIVE_MQTT_USER/PASS.
# =============================================================================
param(
    [Parameter(Mandatory = $true)][string]$GatewayId,
    [string]$Password,
    # Defaults: the relay VM's static IP since the 2026-09-04 GCP account move,
    # and the login gcloud provisions for this box.
    [string]$VmUser = "Sarthak",
    [string]$VmIp = "8.235.4.66",
    [string]$KeyFile = (Join-Path $HOME ".ssh\google_compute_engine"),
    # Reset an existing account even if the database shows an active charging
    # session on the gateway's plugs (or the check can't run).
    [switch]$Force
)

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# The id ends up in a remote command line and in grep patterns.
if ($GatewayId -notmatch '^[A-Za-z0-9_-]{1,63}$') {
    Fail "GatewayId may only contain [A-Za-z0-9_-] (1-63 chars)."
}

$generated = $false
if (-not $Password) {
    # 21 random bytes -> 28 chars of base64url ([A-Za-z0-9_-]), 168 bits.
    $bytes = New-Object byte[] 21
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $Password = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    $generated = $true
}
# Same rule the remote script enforces: fits the firmware's 64-byte mqtt_pwd
# buffer and is safe to type into the setup portal.
if ($Password -notmatch '^[A-Za-z0-9_-]{16,63}$') {
    Fail "Password must be 16-63 chars of [A-Za-z0-9_-]."
}

foreach ($tool in "ssh", "scp") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail "$tool not found - install the Windows OpenSSH client."
    }
}
if (-not (Test-Path $KeyFile)) {
    Fail "SSH key $KeyFile not found (one 'gcloud compute ssh amphive-relay --zone=us-west1-a' creates and authorizes it)."
}
$src = Join-Path $PSScriptRoot "relay_set_gateway_password.sh"
if (-not (Test-Path $src)) { Fail "$src not found." }

$target = "$VmUser@$VmIp"
$sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15")
$remoteScript = "/tmp/relay_set_gateway_password.sh"

# LF-only, BOM-less copy for the VM.
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "relay_set_gateway_password.sh"
$text = [System.IO.File]::ReadAllText($src) -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($tmp, $text, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Uploading the broker script to ${target}:$remoteScript ..." -ForegroundColor Cyan
& scp @sshOpts $tmp "${target}:$remoteScript"
$scpRc = $LASTEXITCODE
Remove-Item $tmp -ErrorAction SilentlyContinue
if ($scpRc -ne 0) { Fail "scp to $target failed (exit $scpRc) - nothing changed on the VM." }

Write-Host "Setting the broker password for '$GatewayId' (sent over stdin) ..." -ForegroundColor Cyan
# The id (validated above) and the fixed --force flag are the only
# interpolated values.
$forceArg = if ($Force) { " --force" } else { "" }
$remoteCmd = 'bash {0} {1}{2}; rc=$?; rm -f {0}; exit $rc' -f $remoteScript, $GatewayId, $forceArg
$Password | & ssh @sshOpts $target $remoteCmd
$rc = $LASTEXITCODE

switch ($rc) {
    0 {
        Write-Host "Done. '$GatewayId' can now use amphive/gateways/$GatewayId/# on both listeners." -ForegroundColor Green
        if ($generated) {
            Write-Host ""
            Write-Host "MQTT password for $GatewayId (shown ONCE, not stored anywhere):" -ForegroundColor Yellow
            Write-Host "  $Password"
            Write-Host "Put it into the gateway now: the setup portal's MQTT field (fresh/unjoined device only), or mqtt_pwd in the USB provisioning file (tools/gateway_provision; the way to reach a deployed unit)."
        }
        exit 0
    }
    9 {
        Write-Host "Refused (see above): the database shows an active charging session on this gateway's plugs, or the check could not run. Nothing changed. A reset now would lock the gateway out mid-session; re-run once the session has ended, or with -Force after checking the CPO dashboard yourself." -ForegroundColor Red
        exit 9
    }
    { $_ -in 2, 3, 4, 5, 7 } {
        Write-Host "FAILED (remote exit $rc, see the output above) - the passwd file was NOT changed." -ForegroundColor Red
        exit $rc
    }
    6 {
        Write-Host "The passwd file WAS updated, but the broker reload or the verify failed (see above; the backup path is printed there)." -ForegroundColor Red
        if ($generated) { Write-Host "New password for $GatewayId (needed if you keep this change): $Password" -ForegroundColor Yellow }
        exit 6
    }
    8 {
        Write-Host "Write-back FAILED - restore the passwd file from the backup named above." -ForegroundColor Red
        exit 8
    }
    255 {
        Write-Host "ssh failed (connection or authentication, exit 255). If it dropped mid-run, check ~/amphive-relay/config/mosquitto_passwd on the VM." -ForegroundColor Red
        exit 255
    }
    default {
        Write-Host "Unexpected exit $rc - check the output above and the passwd file on the VM." -ForegroundColor Red
        exit 1
    }
}
