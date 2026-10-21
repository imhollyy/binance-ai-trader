# Stop ONLY TradePilot's recorded cloudflared process and local preview containers.
# Intentionally keeps all PostgreSQL volumes and stored PAPER data.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidFile = Join-Path $root ".local-tradepilot\quick-cloudflared.pid"
$envFile = Join-Path $root ".env.preview.quick"

Push-Location $root
try {
    if (Test-Path $pidFile) {
        $recordedPid = Get-Content -Raw $pidFile
        if ($recordedPid -match '^[0-9]+$') {
            $process = Get-Process -Id ([int]$recordedPid) -ErrorAction SilentlyContinue
            if ($process -and $process.ProcessName -like "*cloudflared*") {
                Stop-Process -Id $process.Id -ErrorAction Stop
            }
        }
        Remove-Item -Force $pidFile
    }
    if (Test-Path $envFile) {
        & docker compose -p tradepilot_quick --env-file $envFile -f compose.preview.yaml --profile preview down --remove-orphans
        if ($LASTEXITCODE -ne 0) { throw "Preview containers failed to stop cleanly." }
    }
    Write-Host "Quick Tunnel stopped. Isolated PAPER database volume preserved." -ForegroundColor Green
} finally {
    Pop-Location
}
