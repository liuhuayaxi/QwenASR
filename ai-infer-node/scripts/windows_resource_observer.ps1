param([string]$Distro = 'Ubuntu-24.04', [int]$DurationSeconds = 300, [int]$IntervalSeconds = 10,
      [string]$Output = (Join-Path $env:LOCALAPPDATA 'AIInferNode\resource-observations.jsonl'))
# Read-only. Never shutdown/restart WSL, stop a service, change Serve, or read a token.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
if ($DurationSeconds -lt 0 -or $DurationSeconds -gt 3600 -or $IntervalSeconds -lt 1) { throw 'Invalid observation window' }
$directory = Split-Path $Output
New-Item -ItemType Directory -Force -Path $directory | Out-Null
$started = Get-Date
while ($true) {
    $vm = @(Get-Process -Name vmmemWSL -ErrorAction SilentlyContinue)
    $hostMetrics = @($vm | ForEach-Object { @{pid=$_.Id; WorkingSet64=$_.WorkingSet64; PrivateMemorySize64=$_.PrivateMemorySize64} })
    $linux = $null
    # --exec only reads the existing node. It does not alter configuration.
    try {
        $linux = (& "$env:WINDIR\System32\wsl.exe" -d $Distro --exec /bin/cat /proc/meminfo) -join "`n"
        if ($LASTEXITCODE -ne 0) { $linux = $null }
    } catch { $linux = $null }
    $row = @{ timestamp=(Get-Date).ToString('o'); elapsedSeconds=((Get-Date)-$started).TotalSeconds;
        windowsHostState=$(if ($vm.Count) {'observed'} else {'unknown'}); vmmemWSL=$hostMetrics;
        linuxState=$(if ($linux) {'observed'} else {'unknown'}); linuxMeminfo=$linux }
    $row | ConvertTo-Json -Compress -Depth 5 | Add-Content -Encoding UTF8 $Output
    if (((Get-Date)-$started).TotalSeconds -ge $DurationSeconds) { break }
    Start-Sleep -Seconds $IntervalSeconds
}
Write-Output "Read-only measurements saved: $Output"
