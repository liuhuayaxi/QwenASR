$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir
if (-not $env:QWEN_ASR_CLASSROOM_EDITION) { $env:QWEN_ASR_CLASSROOM_EDITION = "1" }
$Port = if ($env:QWEN_ASR_PORT) { $env:QWEN_ASR_PORT } else { "8765" }
$Python = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (!(Test-Path $Python)) {
  if (!(Get-Command uv -ErrorAction SilentlyContinue)) { throw "未找到 uv。请先安装 uv。" }
  & (Get-Command uv).Source venv --python 3.12 .venv
}
$UV = (Get-Command uv).Source
$Requirements = & $Python -m qwen_asr_app.model_setup requirements
if ($LASTEXITCODE -ne 0) { $Requirements = Join-Path $ScriptDir "requirements-windows.txt" }
& $UV pip install --python $Python -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败。" }
& $Python -m qwen_asr_app.model_setup check
if ($LASTEXITCODE -ne 0) { throw "环境检查失败。" }
& $Python -m qwen_asr_app.model_setup prepare
if ($LASTEXITCODE -ne 0) { throw "模型准备失败。" }
& $Python -m qwen_asr_app.service_control start
if ($LASTEXITCODE -ne 0) { throw "QwenASR 启动失败，请检查 server.log。" }
Start-Process "http://127.0.0.1:$Port"
Write-Host "QwenASR 已启动。停止请运行 stop_qwen_asr.ps1。"
