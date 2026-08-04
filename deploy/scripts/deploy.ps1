# =============================================================================
# AmpHive Relay Deployment Script
#
# Deploys the COMMITTED git HEAD (backend/ + frontend/ source) to the
# free-tier prod VM `amphive-relay` and rebuilds the two app images on-box.
#
# Usage:   .\deploy\scripts\deploy.ps1 [-SkipVerify]
# Target:  amphive-relay (us-west1-a, e2-micro) — stack dir ~/amphive-relay,
#          compose file docker-compose.relay.yml (image-pinned services
#          amphive_backend:latest / amphive_frontend:latest, no build: keys).
#
# (The pre-2026-07-27 version of this script targeted the deleted paid VM
# amphive-vm-in and shipped .env/Caddyfile/mosquitto config — see git history.)
#
# What this script deliberately does NOT ship:
#   - .env             — lives only on the VM; holds live secrets (DB, MQTT,
#                        Razorpay, SMTP, Google OAuth). Never overwritten here.
#   - Caddyfile        — live on the VM with working Let's Encrypt config.
#   - mosquitto config — live under ~/amphive-relay/config/; per-gateway broker
#                        accounts are managed by add_gateway_user.ps1.
# Sync .env keys by hand (append + `docker compose up -d backend`); first-time
# host bootstrap is deploy/relay/deploy-relay.sh; the full consolidation story
# is deploy/docs/relay_consolidation_runbook.md.
#
# Notes:
#   - Deploys HEAD, not the working tree: commit first. Intentional — prod
#     should always equal a commit CI has seen.
#   - Source is swapped via a staging dir (extract THEN rm+mv) so a corrupt
#     tarball never wipes the live tree, and renames/deletions propagate
#     (the 2026-07-13 stale-file-overlay outage lesson).
#   - Builds run sequentially under nohup on the VM (1 GB RAM + 2 GB swap;
#     survives an SSH drop) and this script polls ~/build.log for the result.
#   - DB migrations auto-apply at backend startup (init_db → alembic upgrade);
#     the verify step asserts alembic_version equals the repo's newest
#     migration file (revision id == filename stem by repo convention, and
#     must stay ≤32 chars — alembic_version.version_num is VARCHAR(32)).
# =============================================================================
param(
    [switch]$SkipVerify
)

$PROJECT_ROOT      = (Resolve-Path "$PSScriptRoot\..\..").Path
$VM_NAME           = "amphive-relay"
$VM_ZONE           = "us-west1-a"
$REMOTE_DIR        = "~/amphive-relay"
$COMPOSE_FILE      = "docker-compose.relay.yml"
$EXPECTED_PROJECT  = "project-7ee69f02-c0cf-4f51-952"
$BUILD_TIMEOUT_MIN = 25

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Ssh($cmd) { gcloud compute ssh $VM_NAME --zone=$VM_ZONE --command=$cmd 2>$null }

# ---- Step 1: Preflight ------------------------------------------------------
Write-Host "`n[1/6] Preflight..." -ForegroundColor Cyan

# Other projects' work can silently switch the gcloud default project; every
# gcloud call below relies on it, so hard-gate rather than deploy elsewhere.
$activeProject = (gcloud config get-value project 2>$null | Select-Object -Last 1)
if ("$activeProject".Trim() -ne $EXPECTED_PROJECT) {
    Fail "gcloud project is '$activeProject', expected '$EXPECTED_PROJECT'. Run: gcloud config set project $EXPECTED_PROJECT"
}

Push-Location $PROJECT_ROOT
$headSha = (git rev-parse --short HEAD).Trim()
$dirty = git status --porcelain -- backend frontend
if ($dirty) {
    Write-Host "WARNING: uncommitted changes under backend/ or frontend/ will NOT deploy (HEAD ships, not the working tree):" -ForegroundColor Yellow
    $dirty | Select-Object -First 8 | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
}

# Newest migration file's revision id — what alembic_version must equal after
# the deploy.
$latestMigration = Get-ChildItem "$PROJECT_ROOT\backend\migrations\versions\*.py" |
    Where-Object { $_.Name -match '^\d{4}_' } | Sort-Object Name | Select-Object -Last 1
$expectedHead = $latestMigration.BaseName
Write-Host "Deploying HEAD $headSha  (expected alembic head: $expectedHead)"

# The VM must already carry its operator-managed files; refuse to limp on.
$remoteCheck = Ssh "cd $REMOTE_DIR 2>/dev/null && ls .env $COMPOSE_FILE >/dev/null 2>&1 && echo READY || echo MISSING"
if ("$remoteCheck" -notmatch "READY") {
    Fail "$REMOTE_DIR/.env or $COMPOSE_FILE missing on $VM_NAME — this script only refreshes source. See deploy/relay/deploy-relay.sh for first-time bootstrap."
}

# Re-validate POSTGRES_PASSWORD on EVERY deploy, not just first-time bootstrap
# (deploy-relay.sh only gates it once). A weak/legacy DB password reintroduced
# by a later .env edit would otherwise slip through unnoticed. Same blocklist as
# deploy-relay.sh: empty, the template placeholder, and the rotated-out legacy
# literal. (Strong values pass — this rejects only known-bad ones, never
# heuristically.)
$pgPwd = (Ssh "grep -E '^POSTGRES_PASSWORD=' $REMOTE_DIR/.env | head -1 | cut -d= -f2-").Trim()
if ($pgPwd -eq "" -or $pgPwd -eq "set-a-strong-db-password" -or $pgPwd -eq "amphive_db_admin") {
    Fail "POSTGRES_PASSWORD in $REMOTE_DIR/.env is missing/placeholder/legacy ('amphive_db_admin' or the template default). Set a strong value on the VM before deploying."
}

# ---- Step 2: Archive committed source --------------------------------------
Write-Host "`n[2/6] Archiving HEAD (backend/ + frontend/)..." -ForegroundColor Cyan
$tarball = Join-Path $env:TEMP "amphive-relay-src.tar.gz"
git archive --format=tar.gz -o $tarball HEAD backend frontend
if ($LASTEXITCODE -ne 0) { Fail "git archive failed" }
Write-Host ("Archive: {0:N0} bytes" -f (Get-Item $tarball).Length)

# ---- Step 3: Upload ---------------------------------------------------------
Write-Host "`n[3/6] Uploading to $VM_NAME..." -ForegroundColor Cyan
gcloud compute scp $tarball "${VM_NAME}:/tmp/relay-src.tar.gz" --zone=$VM_ZONE
if ($LASTEXITCODE -ne 0) { Fail "scp failed" }

# ---- Step 4: Stage source (extract to staging, THEN swap) -------------------
# The && chain means a corrupt/short tarball fails BEFORE the old
# backend/frontend are removed — a bad transfer never wipes the live tree,
# and the swap (not overlay) means local deletions/renames propagate.
Write-Host "`n[4/6] Staging source on the VM..." -ForegroundColor Cyan
Ssh "cd $REMOTE_DIR && rm -rf .deploy_stage && mkdir .deploy_stage && tar -xzf /tmp/relay-src.tar.gz -C .deploy_stage && rm /tmp/relay-src.tar.gz && rm -rf backend frontend && mv .deploy_stage/backend .deploy_stage/frontend . && rm -rf .deploy_stage && ls -d backend frontend" | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "remote staging failed" }

# ---- Step 5: Build images sequentially (nohup + poll), then roll ------------
Write-Host "`n[5/6] Building images on the VM (sequential, ~10 min on e2-micro)..." -ForegroundColor Cyan
Ssh "cd $REMOTE_DIR && rm -f ~/build.log && nohup sh -c 'sudo docker build -t amphive_backend:latest ./backend && sudo docker build -t amphive_frontend:latest ./frontend && echo BUILDS_DONE || echo BUILD_FAILED' > ~/build.log 2>&1 & echo kicked" | Out-Null

$deadline = (Get-Date).AddMinutes($BUILD_TIMEOUT_MIN)
$result = ""
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 40
    $probe = Ssh "grep -m1 -E 'BUILDS_DONE|BUILD_FAILED' ~/build.log 2>/dev/null || tail -c 120 ~/build.log 2>/dev/null"
    $line = ("$probe" -split "`n" | Select-Object -Last 1).Trim()
    Write-Host "  build: $line"
    if ($line -match "BUILDS_DONE") { $result = "ok"; break }
    if ($line -match "BUILD_FAILED") { $result = "failed"; break }
}
if ($result -eq "failed") { Ssh "tail -40 ~/build.log" | Out-Host; Fail "image build failed on the VM (full log: ~/build.log)" }
if ($result -ne "ok")     { Fail "build did not finish within $BUILD_TIMEOUT_MIN min — check ~/build.log on the VM (it may still complete; re-run this script once it has)" }

Write-Host "Rolling containers..." -ForegroundColor Cyan
Ssh "cd $REMOTE_DIR && sudo docker compose -f $COMPOSE_FILE up -d 2>&1 | tail -4" | Out-Host

# ---- Step 6: Verify ---------------------------------------------------------
if ($SkipVerify) { Write-Host "`n[6/6] Skipped verify (-SkipVerify)." -ForegroundColor Yellow; Pop-Location; exit 0 }
Write-Host "`n[6/6] Verifying..." -ForegroundColor Cyan
Start-Sleep -Seconds 20

$verify = Ssh ("cd $REMOTE_DIR && " +
    "curl -s -m 10 http://localhost:8000/api/health; echo; " +
    "sudo docker compose -f $COMPOSE_FILE exec -T db psql -U postgres -d amphive -tAc 'SELECT version_num FROM alembic_version'; " +
    "sudo docker inspect --format 'restarts={{.RestartCount}} status={{.State.Status}}' `$(sudo docker compose -f $COMPOSE_FILE ps -q backend)")
$verify | Out-Host

$ok = $true
if ("$verify" -notmatch '"status":\s*"healthy"')        { Write-Host "FAIL: /api/health not healthy" -ForegroundColor Red; $ok = $false }
if ("$verify" -notmatch [regex]::Escape($expectedHead)) { Write-Host "FAIL: alembic_version != $expectedHead" -ForegroundColor Red; $ok = $false }
if ("$verify" -match "restarts=[1-9]")                  { Write-Host "FAIL: backend has restarted since the roll" -ForegroundColor Red; $ok = $false }

$public = try { (Invoke-WebRequest -Uri "https://amphive.app/api/health" -UseBasicParsing -TimeoutSec 15).StatusCode } catch { 0 }
if ($public -ne 200) { Write-Host "FAIL: https://amphive.app/api/health returned $public" -ForegroundColor Red; $ok = $false }

Pop-Location
if (-not $ok) { Fail "verification failed — inspect with: gcloud compute ssh $VM_NAME --zone=$VM_ZONE" }
Write-Host "`nDeploy complete: prod == $headSha, alembic @ $expectedHead, public health 200." -ForegroundColor Green
