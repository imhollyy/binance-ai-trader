# Stop only the dedicated review project; never remove named volumes.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$project = "tradepilot_bt027_review"
$baseCompose = Join-Path $root "compose.preview.yaml"
$reviewCompose = Join-Path $root "compose.preview.review.yaml"
$envFile = Join-Path $root ".env.preview.review"
$pidFile = Join-Path $root ".local-tradepilot-review\cloudflared.pid"

Push-Location $root
try {
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw "Dedicated review environment file is missing; refusing to infer a target project."
    }
    if (Test-Path -LiteralPath $pidFile) {
        $raw = (Get-Content -Raw -LiteralPath $pidFile).Trim()
        $processId = 0
        if ([int]::TryParse($raw,[ref]$processId)) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
            if ($process -and $process.Name -match '^cloudflared(\.exe)?$' -and
                $process.CommandLine -match '127\.0\.0\.1:8877' -and
                $process.CommandLine -match '--allowed-mail') {
                Stop-Process -Id $processId -ErrorAction Stop
            }
        }
        Remove-Item -LiteralPath $pidFile -Force
    }
    & docker compose -p $project --env-file $envFile -f $baseCompose -f $reviewCompose --profile review stop
    if ($LASTEXITCODE -ne 0) { throw "Could not stop the isolated review services. Volumes remain untouched." }
    Write-Host "Review tunnel stopped and only the dedicated review services stopped. Volumes were preserved."
} finally {
    Pop-Location
}
