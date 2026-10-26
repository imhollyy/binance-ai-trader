# Safely reconnect ONLY the existing BT-027 owner-review Docker project.
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$project = "tradepilot_bt027_review"
$dbName = "$project-preview-db-1"
$envFile = Join-Path $root ".env.preview.review"
$logDir = Join-Path $root ".local-tradepilot-review"
$pidFile = Join-Path $logDir "cloudflared.pid"
$stderr = Join-Path $logDir "cloudflared.stderr.log"
$stdout = Join-Path $logDir "cloudflared.stdout.log"
$tunnel = $null
$completed = $false

function Get-PlainSecureInput([Security.SecureString]$Value) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Push-Location $root
try {
    foreach ($path in @($envFile, $logDir)) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Existing isolated review configuration missing; no replacement will be created."
        }
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Private preview path must not be a symlink."
        }
    }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw "Existing review env file is not a regular file."
    }
    $expectedContainers = @(
        "$project-preview-db-1", "$project-preview-api-1",
        "$project-preview-web-1", "$project-preview-ingress-1"
    )
    $actual = @(docker ps -a --filter "label=com.docker.compose.project=$project" --format "{{.Names}}")
    if ($LASTEXITCODE -ne 0 -or
        @(Compare-Object -ReferenceObject $expectedContainers -DifferenceObject $actual).Count -ne 0) {
        throw "Expected exactly four existing dedicated review containers."
    }
    $db = (docker inspect $dbName | ConvertFrom-Json)[0]
    if ($LASTEXITCODE -ne 0 -or $null -eq $db -or
        $db.Config.Labels.'com.docker.compose.project' -ne $project) {
        throw "Existing database container identity is not trusted."
    }
    $expectedVolume = $project + "_preview_postgres_data"
    $mounts = @($db.Mounts | Where-Object {
        $_.Type -eq "volume" -and $_.Destination -eq "/var/lib/postgresql/data" -and
        $_.Name -eq $expectedVolume
    })
    if ($mounts.Count -ne 1) { throw "Existing review database volume cannot be verified." }
    if (Test-Path -LiteralPath $pidFile) {
        $pidText = (Get-Content -Raw -LiteralPath $pidFile).Trim()
        $savedPid = 0
        if (-not [int]::TryParse($pidText,[ref]$savedPid)) {
            throw "Unexpected saved tunnel metadata; inspect locally."
        }
        if (Get-Process -Id $savedPid -ErrorAction SilentlyContinue) {
            throw "Saved tunnel PID refers to a running process; refusing replacement."
        }
        Remove-Item -LiteralPath $pidFile -Force
    }
    $existingTunnel = @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
        Where-Object {
            $_.CommandLine -match '127\.0\.0\.1:8877' -and
            $_.CommandLine -match '--allowed-mail'
        })
    if ($existingTunnel.Count -gt 0) { throw "An owner-review tunnel is already running." }

    $cfCommand = Get-Command cloudflared -ErrorAction SilentlyContinue
    $cf = if ($cfCommand) { $cfCommand.Source } else { $null }
    if (-not $cf) {
        $candidate = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe"
        if (Test-Path -LiteralPath $candidate) { $cf = $candidate }
    }
    if (-not $cf) {
        $packageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
        $package = Get-ChildItem -LiteralPath $packageRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object Name -like "Cloudflare.cloudflared*" | Select-Object -First 1
        if ($package) {
            $exe = Get-ChildItem -LiteralPath $package.FullName -Filter "cloudflared.exe" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($exe) { $cf = $exe.FullName }
        }
    }
    if (-not $cf -or -not (Test-Path -LiteralPath $cf)) {
        throw "Current cloudflared executable not found."
    }
    $helpText = (& $cf tunnel --help 2>&1 | Out-String)
    if ($helpText -notmatch 'allowed-mail') { throw "PIN-gated tunnel mode is unavailable." }
    if (-not (Get-Command python -ErrorAction SilentlyContinue) -or
        -not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
        throw "Python 3 and curl.exe are required."
    }

    if (-not $db.State.Running) {
        docker start $dbName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not start the existing review database." }
    }
    $dbHealthy = $false
    for ($i = 0; $i -lt 30; $i++) {
        $health = docker inspect $dbName --format '{{.State.Health.Status}}'
        if ($LASTEXITCODE -ne 0 -or $health -eq "unhealthy") {
            throw "Existing review database is unhealthy."
        }
        if ($health -eq "healthy") { $dbHealthy = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $dbHealthy) { throw "Existing review DB was not verified healthy." }

    $secureEmail = Read-Host "Email allowed to receive Cloudflare PIN" -AsSecureString
    $email = Get-PlainSecureInput $secureEmail
    $secureEmail.Dispose()
    if ($email -notmatch '^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$') {
        throw "Invalid allowed-email format."
    }
    $tunnel = Start-Process -FilePath $cf -ArgumentList @(
        "tunnel", "--url", "http://127.0.0.1:8877", "--allowed-mail", $email
    ) -RedirectStandardError $stderr -RedirectStandardOutput $stdout -PassThru -WindowStyle Hidden
    $email = $null
    $origin = $null
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if ($tunnel.HasExited) { throw "Protected tunnel ended without a usable origin." }
        foreach ($path in @($stderr, $stdout)) {
            if (-not (Test-Path -LiteralPath $path)) { continue }
            $log = Get-Content -Raw -LiteralPath $path -ErrorAction SilentlyContinue
            if ($log -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
                $origin = $Matches[0]
                break
            }
        }
        if ($origin) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $origin) { throw "Protected tunnel origin missing." }

    # prepare() preserves the existing DB password and both hashed role verifiers.
    & python scripts/prepare_review_preview.py --origin $origin --file .env.preview.review
    if ($LASTEXITCODE -ne 0) { throw "Existing isolated preview config validation failed." }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $envFile /inheritance:r /grant:r "$($identity):(F)" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Review env ACL could not be restricted." }
    $compose = @(
        "-p", $project, "--env-file", ".env.preview.review",
        "-f", "compose.preview.yaml", "-f", "compose.preview.review.yaml",
        "--profile", "review"
    )
    & docker compose @compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Isolated review Compose validation failed." }

    # Rebuild/recreate only API/Web/Ingress; never recreate or reset PAPER DB.
    & docker compose @compose up --build --no-deps -d preview-api preview-web preview-ingress
    if ($LASTEXITCODE -ne 0) { throw "Isolated review services could not update." }
    $hostName = ([uri]$origin).Host
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $page = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8877/" -Headers @{Host=$hostName} -TimeoutSec 3 -ErrorAction Stop
            if ($page.StatusCode -ne 200 -or $page.Content -notmatch "TradePilot") {
                Start-Sleep -Seconds 2
                continue
            }
        } catch {
            Start-Sleep -Seconds 2
            continue
        }
        # A valid proxy-HTTPS session without a cookie returns 401, not 403.
        $sessionStatus = & curl.exe --silent --show-error --max-time 5 --output NUL --write-out '%{http_code}' --header "Host: $hostName" --header "Origin: $origin" "http://127.0.0.1:8877/api/v1/auth/session"
        if ($LASTEXITCODE -eq 0 -and "$sessionStatus" -eq "401") {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw "Viewer or HTTPS session path did not pass local checks." }
    $headers = & curl.exe --silent --show-error --head --max-redirs 0 $origin 2>$null
    if ($LASTEXITCODE -ne 0) { throw "External HTTPS certificate/edge check failed." }
    $statusLine = ($headers | Where-Object { $_ -match '^HTTP/' } | Select-Object -Last 1)
    $edge = (($headers -join [Environment]::NewLine) -match '(?im)^server:\s*cloudflare\s*$') -or
            (($headers -join [Environment]::NewLine) -match '(?im)^cf-ray:')
    if (-not $edge -or $statusLine -notmatch '^HTTP/\S+\s+(302|303|307|308|401|403)\b') {
        throw "External email-PIN gate could not be corroborated."
    }
    Set-Content -LiteralPath $pidFile -Value $tunnel.Id -NoNewline
    $completed = $true
    Write-Host "Existing review resumed with exact Host and HTTPS session check."
    Write-Host ("External URL: " + $origin)
    Write-Host "Owner must still perform Cloudflare PIN, viewer login and logout."
    Write-Host "Automatic trading, production PAPER BUY and Binance orders remain disabled."
} finally {
    if (-not $completed -and $tunnel -and -not $tunnel.HasExited) {
        Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue
    }
    Pop-Location
}
