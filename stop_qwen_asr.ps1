$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir
$Python = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (!(Test-Path $Python)) { throw "未找到 .venv，请先运行 start_qwen_asr.ps1。" }
& $Python -m qwen_asr_app.service_control stop
if ($LASTEXITCODE -ne 0) { throw "停止服务失败。" }
