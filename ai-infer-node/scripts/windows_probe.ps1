$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$task = Get-ScheduledTask -TaskName 'AI Infer WSL Gateway' -ErrorAction SilentlyContinue
$vm = Get-Process -Name vmmemWSL -ErrorAction SilentlyContinue
[pscustomobject]@{
    task = $task.TaskName
    state = [string]$task.State
    user = $task.Principal.UserId
    logonType = [string]$task.Principal.LogonType
    triggers = @($task.Triggers | ForEach-Object { $_.CimClass.CimClassName })
    wslWorkingSetBytes = $vm.WorkingSet64
    wslPrivateMemoryBytes = $vm.PrivateMemorySize64
} | ConvertTo-Json -Compress
