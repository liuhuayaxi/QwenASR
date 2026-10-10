param([string]$Distro = "Ubuntu-24.04", [switch]$BootBeforeLogin)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($identity -match '\\SYSTEM$') { throw 'Run as the Windows user who owns this WSL distribution.' }
$taskName = 'AI Infer WSL Gateway'
$folder = Join-Path $env:LOCALAPPDATA 'AIInferNode'
New-Item -ItemType Directory -Force -Path $folder | Out-Null
$keepalive = Join-Path $folder 'keepalive.ps1'
@"
`$ErrorActionPreference = 'Stop'
while (`$true) {
    & "`$env:WINDIR\System32\wsl.exe" -d "$Distro" --exec /usr/bin/sleep infinity
    Start-Sleep -Seconds 5
}
"@ | Set-Content -Encoding UTF8 $keepalive
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$keepalive`""
$triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $identity))
$logon = 'Interactive'
if ($BootBeforeLogin) {
    # S4U uses the actual owner, not SYSTEM, and never stores a password.
    # Registration can require elevation. Do not bypass UAC when denied.
    $triggers += New-ScheduledTaskTrigger -AtStartup
    $logon = 'S4U'
}
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType $logon -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName $taskName | Set-Content -Encoding UTF8 (Join-Path $folder ('task-backup-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.xml')) }
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State,Principal,Triggers
Get-ScheduledTaskInfo -TaskName $taskName
