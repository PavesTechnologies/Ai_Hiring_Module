# AIRS isolated e2e stack - see e2e/README.md.
#
#   pwsh e2e/e2e.ps1 up       build-free start (image airs:e2e), migrate, seed, load fixtures, start worker
#   pwsh e2e/e2e.ps1 test     run the re-parse / re-score end-to-end check (fresh database each time)
#   pwsh e2e/e2e.ps1 logs     follow the worker
#   pwsh e2e/e2e.ps1 down     stop and DELETE the e2e database and files
#
# Never reaches live systems: configured only from e2e/.env.e2e (scripts/e2e/make_env.py),
# email is log-only, storage is local, and every script refuses to run if not isolated.
param([Parameter(Mandatory = $true)][ValidateSet("up", "test", "logs", "down", "build")] [string]$Command)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Compose = @("compose", "-f", (Join-Path $PSScriptRoot "docker-compose.e2e.yml"))
Set-Location $Root

function Invoke-Compose { param([string[]]$Arguments) & docker @Compose @Arguments; if ($LASTEXITCODE -ne 0) { throw "docker compose $Arguments failed" } }

# The stack's Mailpit (docker-compose.e2e.yml) - catches every email, relays nothing.
$MailpitApi = "http://127.0.0.1:48025/api/v1"
function Get-MailpitCount { (Invoke-RestMethod -Uri "$MailpitApi/messages?limit=1").total }
function Clear-Mailpit { Invoke-RestMethod -Method Delete -Uri "$MailpitApi/messages" | Out-Null }

function Ensure-Env {
    if (-not (Test-Path (Join-Path $PSScriptRoot ".env.e2e"))) {
        python -m scripts.e2e.make_env
        if ($LASTEXITCODE -ne 0) { throw "could not create e2e/.env.e2e" }
    }
}

function Start-Stack {
    Ensure-Env
    if (-not (docker images -q airs:e2e)) { throw "image airs:e2e not found - run: pwsh e2e/e2e.ps1 build" }
    Invoke-Compose @("up", "-d", "postgres", "redis", "mailpit")
    Invoke-Compose @("run", "--rm", "tools")          # migrate + seed + fixtures (scripts.e2e.setup_e2e)
    Invoke-Compose @("up", "-d", "worker")
    Write-Host "Waiting for the worker..."
    for ($i = 0; $i -lt 60; $i++) {
        if ((& docker @Compose logs worker 2>&1) -match "ready\.") { Write-Host "worker ready"; return }
        Start-Sleep -Seconds 3
    }
    throw "worker did not become ready"
}

switch ($Command) {
    "build" { & docker build -f arm.Dockerfile -t airs:e2e . }
    "up"    { Start-Stack }
    "test"  {
        Invoke-Compose @("down", "-v")                 # every run starts from an empty database
        Start-Stack
        Clear-Mailpit
        Invoke-Compose @("run", "--rm", "tools", "-m", "scripts.e2e.run_reparse_e2e")
        Invoke-Compose @("run", "--rm", "tools", "-m", "scripts.e2e.run_phase2_e2e")   # calibration / unknown skills
        Invoke-Compose @("run", "--rm", "tools", "-m", "scripts.e2e.run_fallback_e2e") # retire v1: retries, fallback
        Invoke-Compose @("run", "--rm", "tools", "-m", "scripts.e2e.run_scoring_versions_e2e") # retire v1 D: versions
        Invoke-Compose @("run", "--rm", "tools", "-m", "scripts.e2e.run_golden_estimate_e2e")  # Settings: golden estimate via the worker
        # None of these drivers sends email: Mailpit must have received nothing, and nothing was logged instead.
        $caught = Get-MailpitCount
        $logged = (& docker @Compose logs worker 2>&1 | Select-String "EMAIL NOT SENT").Count
        Write-Host "Emails Mailpit received (none expected): $caught; logged instead of sent: $logged"
        if ($caught -ne 0 -or $logged -ne 0) { throw "the drivers sent $caught email(s) to Mailpit and logged $logged" }
        # Then the one driver that sends: an HR alert over SMTP to Mailpit (it clears Mailpit after).
        Invoke-Compose @("run", "--rm", "tools", "-m", "scripts.e2e.run_mailpit_e2e")
    }
    "logs"  { Invoke-Compose @("logs", "-f", "worker") }
    "down"  { Invoke-Compose @("down", "-v") }
}
