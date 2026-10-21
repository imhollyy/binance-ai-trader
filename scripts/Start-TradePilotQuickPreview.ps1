# Free, email-PIN-protected Quick Tunnel and isolated local PAPER preview.
# No Cloudflare account, DNS or paid service changes. No real trade execution.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$')]
    [string]$AllowedEmail
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logDir = Join-Path $root ".local-tradepilot"
$pidFile = Join-Path $logDir "quick-cloudflared.pid"
$stderr = Join-Path $logDir "cloudflared.stderr.log"
$stdout = Join-Path $logDir "cloudflared.stdout.log"
$envFile = Join-Path $root ".env.preview.quick"
$tunnelProcess = $null
$started = $false

Push-Location $root
try {
    if (-not (Get-Command "docker" -ErrorAction SilentlyContinue)) {
        throw "Docker Desktop must be installed and on PATH."
    }
    & docker info --format "{{.ServerVersion}}" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop engine is not running."
    }
    $cloudflared = Get-Command "cloudflared" -ErrorAction SilentlyContinue
    if (-not $cloudflared) {
        throw "Install latest cloudflared: winget install --id Cloudflare.cloudflared -e"
    }
    $helpText = (& cloudflared tunnel --help 2>&1 | Out-String)
    if ($helpText -notmatch 'allowed-mail') {
        throw "Update cloudflared: --allowed-mail protection is mandatory."
    }

    $python = Get-Command "python" -ErrorAction SilentlyContinue
    $launcher = Get-Command "py" -ErrorAction SilentlyContinue
    if (-not $python -and -not $launcher) {
        throw "Python 3 is needed for local verifier hashing."
    }

    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    if (Test-Path $pidFile) {
        $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
        $old = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        if ($old -and $old.ProcessName -like "*cloudflared*") {
            throw "An existing TradePilot quick tunnel is running; stop it first."
        }
        Remove-Item $pidFile -Force
    }
    Remove-Item $stderr, $stdout -Force -ErrorAction SilentlyContinue

    # Explicit single allowed email; no unrestricted public tunnel.
    $tunnelProcess = Start-Process -FilePath $cloudflared.Source -ArgumentList @(
        "tunnel", "--url", "http://127.0.0.1:8765", "--allowed-mail", $AllowedEmail
    ) -RedirectStandardError $stderr -RedirectStandardOutput $stdout -PassThru -WindowStyle Hidden

    $origin = $null
    $deadline = (Get-Date).AddSeconds(80)
    while ((Get-Date) -lt $deadline) {
        if ($tunnelProcess.HasExited) {
            throw "Cloudflared exited. Inspect the local .local-tradepilot error log."
        }
        foreach ($path in @($stderr, $stdout)) {
            if (Test-Path $path) {
                $data = Get-Content -Raw $path -ErrorAction SilentlyContinue
                if ($data -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
                    $origin = $Matches[0]
                    break
                }
            }
        }
        if ($origin) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $origin) { throw "Protected Quick Tunnel did not provide a hostname." }
    $hostName = ([uri]$origin).Host

    # Preserve the existing private DB and both password hashes on restarts;
    # only update the dynamic public origin and matching trusted host.
    if ($launcher) {
        & py -3 scripts/prepare_quick_preview.py --origin $origin --file $envFile
    } else {
        & python scripts/prepare_quick_preview.py --origin $origin --file $envFile
    }
    if ($LASTEXITCODE -ne 0) { throw "Preview credentials preparation failed." }

    # Isolated project; no changes to local dev Compose, ports or databases.
    & docker compose -p tradepilot_quick --env-file $envFile -f compose.preview.yaml --profile preview config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Private preview Compose validation failed." }
    & docker compose -p tradepilot_quick --env-file $envFile -f compose.preview.yaml --profile preview up --build -d
    if ($LASTEXITCODE -ne 0) { throw "Private preview containers did not start." }

    $ready = $false
    for ($i = 0; $i -lt 50; $i++) {
        try {
            $localPage = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8765/" -Headers @{ Host = $hostName } -TimeoutSec 3
            if ($localPage.StatusCode -eq 200 -and $localPage.Content -match "TradePilot") {
                $ready = $true
                break
            }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw "Local preview not ready. Do not share this URL." }

    Set-Content -Path $pidFile -Value $tunnelProcess.Id -NoNewline
    $started = $true
    Write-Host ""
    Write-Host "TradePilot PAPER preview running with email PIN gate." -ForegroundColor Green
    Write-Host ("Private URL: " + $origin)
    Write-Host ("Allowed email: " + $AllowedEmail)
    Write-Host "Open URL, complete email PIN, then sign in as viewer."
    Write-Host "Your PC and Docker must remain on. URL changes on next start."
    Write-Host "Stop with: .\scripts\Stop-TradePilotQuickPreview.ps1"
} finally {
    if (-not $started -and $tunnelProcess -and -not $tunnelProcess.HasExited) {
        Stop-Process -Id $tunnelProcess.Id -ErrorAction SilentlyContinue
    }
    Pop-Location
}
