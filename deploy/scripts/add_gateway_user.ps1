# =============================================================================
# Add (or update) a per-gateway MQTT account on the production broker.
#
# Username MUST equal the gateway_id: the broker ACL scopes each gateway to
# amphive/gateways/<username>/# via the %u pattern (see deploy.ps1 / SECURITY.md
# §3), so a matching username is what isolates one client site from another.
#
# The password is hashed on the VM by mosquitto_passwd; the broker is then
# SIGHUP'd to reload passwd+ACL without dropping existing connections.
# Redeploys preserve these accounts (deploy.ps1 no longer recreates the file).
#
# Usage:
#   .\deploy\scripts\add_gateway_user.ps1 -GatewayId gw-clientsite-01 -Password <pwd>
#
# Provision the same values into the device (captive portal MQTT fields -> NVS
# mqtt_user/mqtt_pwd) or the agent (.env AMPHIVE_MQTT_USER/AMPHIVE_MQTT_PASS).
# =============================================================================
param(
    [Parameter(Mandatory = $true)][string]$GatewayId,
    [Parameter(Mandatory = $true)][string]$Password,
    [string]$VmName = "amphive-vm-in",
    [string]$VmZone = "asia-south1-a",
    [string]$RemoteDir = "/home/Sarthak/amphive"
)

# Same alphanumeric policy deploy.ps1 enforces for the managed accounts — the
# values are interpolated into a remote shell command.
foreach ($pair in @(@("GatewayId", $GatewayId), @("Password", $Password))) {
    if ($pair[1] -notmatch '^[A-Za-z0-9_-]+$') {
        Write-Host "ERROR: $($pair[0]) may only contain [A-Za-z0-9_-]." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Adding broker account for gateway '$GatewayId'..." -ForegroundColor Cyan
$cmd = "sudo docker run --rm -v ${RemoteDir}:/work eclipse-mosquitto:2.0 sh -c 'mosquitto_passwd -b /work/mosquitto_passwd $GatewayId $Password && chown 1883:1883 /work/mosquitto_passwd && chmod 600 /work/mosquitto_passwd' && sudo docker kill -s HUP amphive-mqtt"
gcloud compute ssh --quiet $VmName --zone=$VmZone --command=$cmd
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED - passwd file not updated." -ForegroundColor Red
    exit 1
}

Write-Host "Done. '$GatewayId' can now use amphive/gateways/$GatewayId/# on both listeners." -ForegroundColor Green
