# Start a completely separate owner-review preview. The external email-PIN
# Quick Tunnel is not started until the owner explicitly approves this script.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$project = "tradepilot_bt027_review"
$baseCompose = Join-Path $root "compose.preview.yaml"
$reviewCompose = Join-Path $root "compose.preview.review.yaml"
$envFile = Join-Path $root ".env.preview.review"
$logDir = Join-Path $root ".local-tradepilot-review"
$pidFile = Join-Path $logDir "cloudflared.pid"
$stderr = Join-Path $logDir "cloudflared.stderr.log"
$stdout = Join-Path $logDir "cloudflared.stdout.log"
$email = $null
$tunnel = $null
$localReady = $false
$started = $false

function ConvertFrom-SecureInput([Security.SecureString]$Value) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function ConvertTo-IPv4Range([string]$Cidr) {
    $parts = $Cidr.Split('/')
    if ($parts.Count -ne 2) { return $null }
    $address = $null
    $prefix = 0
    if (-not [Net.IPAddress]::TryParse($parts[0], [ref]$address) -or
        $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        -not [int]::TryParse($parts[1], [ref]$prefix) -or $prefix -lt 0 -or $prefix -gt 32) {
        return $null
    }
    $bytes = $address.GetAddressBytes()
    $value = [uint64]$bytes[0] * 16777216 + [uint64]$bytes[1] * 65536 +
        [uint64]$bytes[2] * 256 + [uint64]$bytes[3]
    $block = [uint64][Math]::Pow(2, 32 - $prefix)
    $start = [uint64]([Math]::Floor($value / $block) * $block)
    return @{Start=$start; End=($start + $block - 1)}
}

function Test-IPv4CidrOverlap([string]$Left, [string]$Right) {
    $a = ConvertTo-IPv4Range $Left
    $b = ConvertTo-IPv4Range $Right
    return $null -ne $a -and $null -ne $b -and $a.Start -le $b.End -and $b.Start -le $a.End
}

Push-Location $root
try {
    if ((Test-Path $envFile) -and (Test-Path $pidFile)) {
        throw "Review project already has local tunnel metadata; use its dedicated stop/status instructions."
    }
    foreach ($localPath in @($envFile, $logDir)) {
        if (Test-Path -LiteralPath $localPath) {
            $item = Get-Item -LiteralPath $localPath -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Review preview private path must not be a symlink or reparse point."
            }
        }
    }
    $containers = @(docker ps -aq --filter "label=com.docker.compose.project=$project")
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect Docker project state." }
    $volumes = @(docker volume ls -q --filter "name=$project")
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect isolated review volume labels." }
    if ($containers.Count -gt 0 -or $volumes.Count -gt 0) {
        throw "The isolated review project already exists. Refusing to reuse or alter it automatically."
    }
    if (Get-NetTCPConnection -State Listen -LocalPort 8877 -ErrorAction SilentlyContinue) {
        throw "Loopback port 8877 is already in use."
    }
    $conflicts = @(docker network ls -q | ForEach-Object {
        docker network inspect $_ --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}'
    } | Where-Object { $_ -and (Test-IPv4CidrOverlap $_ '172.30.220.0/24') })
    $hostRouteConflicts = @(Get-NetRoute -ErrorAction SilentlyContinue |
        Where-Object { $_.DestinationPrefix -like '172.30.*' -and
            (Test-IPv4CidrOverlap $_.DestinationPrefix '172.30.220.0/24') })
    if ($conflicts.Count -gt 0 -or $hostRouteConflicts.Count -gt 0) {
        throw "Review subnet 172.30.220.0/24 overlaps an existing Docker network or host route."
    }

    & docker info --format "{{.ServerVersion}}" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop engine is not available." }
    $cloudflaredCommand = Get-Command "cloudflared" -ErrorAction SilentlyContinue
    $cloudflaredPath = if ($cloudflaredCommand) { $cloudflaredCommand.Source } else { $null }
    if (-not $cloudflaredPath) {
        $linkPath = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe"
        if (Test-Path -LiteralPath $linkPath) { $cloudflaredPath = $linkPath }
    }
    if (-not $cloudflaredPath) {
        $packageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
        $package = Get-ChildItem -LiteralPath $packageRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object Name -like "Cloudflare.cloudflared*" | Select-Object -First 1
        if ($package) {
            $binary = Get-ChildItem -LiteralPath $package.FullName -Filter "cloudflared.exe" `
                -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($binary) { $cloudflaredPath = $binary.FullName }
        }
    }
    if (-not $cloudflaredPath) { throw "Install the current cloudflared client first." }
    $helpText = (& $cloudflaredPath tunnel --help 2>&1 | Out-String)
    if ($helpText -notmatch "allowed-mail") { throw "This cloudflared version lacks the required email-PIN gate." }
    $python = Get-Command "python" -ErrorAction SilentlyContinue
    if (-not $python) { throw "Python 3 is required to hash the viewer password locally." }
    if (-not (Get-Command "curl.exe" -ErrorAction SilentlyContinue)) {
        throw "curl.exe is required for certificate-validated tunnel verification."
    }

    $secureEmail = Read-Host "Email allowed to receive Cloudflare PIN" -AsSecureString
    $email = ConvertFrom-SecureInput $secureEmail
    $secureEmail.Dispose()
    if ($email -notmatch '^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$') {
        throw "Allowed email format is invalid."
    }
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $tunnel = Start-Process -FilePath $cloudflaredPath -ArgumentList @(
        "tunnel", "--url", "http://127.0.0.1:8877", "--allowed-mail", $email
    ) -RedirectStandardError $stderr -RedirectStandardOutput $stdout -PassThru -WindowStyle Hidden

    $origin = $null
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if ($tunnel.HasExited) { throw "Protected Quick Tunnel process exited before publishing an origin." }
        foreach ($path in @($stderr, $stdout)) {
            if (Test-Path $path) {
                $text = Get-Content -Raw -LiteralPath $path -ErrorAction SilentlyContinue
                if ($text -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
                    $origin = $Matches[0]
                    break
                }
            }
        }
        if ($origin) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $origin) { throw "Protected Quick Tunnel did not provide an HTTPS host." }

    & python scripts/prepare_review_preview.py --origin $origin --file .env.preview.review
    if ($LASTEXITCODE -ne 0) { throw "Local preview configuration was rejected." }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $envFile /inheritance:r /grant:r "$($identity):(F)" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict the ignored preview environment file ACL." }

    & docker compose -p $project --env-file $envFile -f $baseCompose -f $reviewCompose --profile review config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Isolated review Compose validation failed." }
    & docker compose -p $project --env-file $envFile -f $baseCompose -f $reviewCompose --profile review up --build -d
    if ($LASTEXITCODE -ne 0) { throw "Isolated review services did not start. Volumes are preserved." }

    $hostName = ([uri]$origin).Host
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $page = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8877/" `
                -Headers @{Host=$hostName} -TimeoutSec 3 -ErrorAction Stop
            if ($page.StatusCode -eq 200 -and $page.Content -match "TradePilot") {
                $localReady = $true
                break
            }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $localReady) { throw "Local review viewer did not pass its exact-host readiness check." }

    # Verify HTTPS/TLS and the Cloudflare edge without following redirects or
    # printing response headers, cookies, email, or local credential material.
    $headers = & curl.exe --silent --show-error --head --max-redirs 0 $origin 2>$null
    if ($LASTEXITCODE -ne 0) { throw "External HTTPS/TLS probe failed; keep the preview credentials private." }
    $response = ($headers | Where-Object { $_ -match '^HTTP/' } | Select-Object -Last 1)
    $edge = (($headers -join "`n") -match '(?im)^server:\s*cloudflare\s*$') -or
        (($headers -join "`n") -match '(?im)^cf-ray:')
    if (-not $response -or -not $edge) { throw "Quick Tunnel HTTPS edge could not be verified." }

    Set-Content -LiteralPath $pidFile -Value $tunnel.Id -NoNewline
    $started = $true
    Write-Host "Review preview passed local Host and external HTTPS/TLS checks."
    Write-Host ("External URL: " + $origin)
    Write-Host "Open it, complete the Cloudflare PIN for the entered email, then sign in as viewer."
    Write-Host "No risk profile is installed; risk selection stays disabled. Automated trading is off."
    Write-Host "Stop only this isolated project with scripts/Stop-TradePilotReviewPreview.ps1."
} finally {
    $email = $null
    if (-not $started -and $tunnel -and -not $tunnel.HasExited) {
        Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue
    }
    Pop-Location
}
