param([string]$Distro = 'Ubuntu-24.04', [int]$Port = 8765)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
if ($Port -ne 8765) { throw 'This installer is scoped to the inference gateway port 8765.' }
$tailscale = (Get-Command tailscale.exe -ErrorAction Stop).Source
$folder = Join-Path $env:LOCALAPPDATA 'AIInferNode'
New-Item -ItemType Directory -Force -Path $folder | Out-Null
$state = (& $tailscale status --json | ConvertFrom-Json)
if ($state.BackendState -ne 'Running') { throw 'Connect Windows Tailscale before configuring the gateway.' }
$tailnetIP = @($state.TailscaleIPs | Where-Object { $_ -match '^100\.' })[0]
if (-not $tailnetIP) { throw 'Windows has no Tailnet IPv4 address.' }
$before = (& $tailscale serve status --json)
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect existing Tailscale Serve configuration.' }
$before | Set-Content -Encoding UTF8 (Join-Path $folder ('serve-backup-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.json'))
$existing = $before | ConvertFrom-Json
if ($existing.TCP -and $existing.TCP.PSObject.Properties['8765']) {
    $forward = $existing.TCP.'8765'.TCPForward
    if ($forward -ne '127.0.0.1:8765') { throw 'Tailnet port 8765 is already used for another service; no configuration was overwritten.' }
}
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/v1/health' -TimeoutSec 2 | Out-Null
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
}
if (-not $ready) { throw 'Windows localhost:8765 cannot reach the WSL bearer-protected gateway; fix the backend first.' }
& $tailscale serve --bg --yes --tcp=8765 tcp://127.0.0.1:8765
if ($LASTEXITCODE -ne 0) { throw 'Tailscale Serve was rejected; no firewall or access protections were bypassed.' }
$after = (& $tailscale serve status --json | ConvertFrom-Json)
if ($after.TCP.'8765'.TCPForward -ne '127.0.0.1:8765') { throw 'Serve did not retain the requested TCP mapping.' }
if ($after.AllowFunnel -and ($after.AllowFunnel.PSObject.Properties | Where-Object { $_.Value -eq $true })) {
    throw 'Existing Funnel configuration detected. Inspect it separately; this installer never enables Funnel.'
}
[pscustomobject]@{ endpoint="http://${tailnetIP}:8765"; backend='127.0.0.1:8765'; mode='native_tailscale_serve_tcp'; background=$true } |
    ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $folder 'tailnet-entry.json')
Write-Output "Ready: http://${tailnetIP}:8765 (Bearer token required). Official persistent Tailnet TCP Serve; no SSH or LAN listener."
