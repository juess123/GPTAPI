[CmdletBinding()]
param([string]$Python = "C:\Users\QRT\AppData\Local\Programs\Python\Python312\python.exe")
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ProjectRoot = $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "找不到基础 Python：$Python" }
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "创建项目 Python 环境 .venv ..."
    & $Python -m venv (Join-Path $ProjectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "创建 .venv 失败" }
}
Write-Host "安装并核对普通 Python 与 Blender 的锁定依赖 ..."
& $VenvPython (Join-Path $ProjectRoot "pipeline\dependency_manager.py") --ensure
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
Write-Host ""
Write-Host "环境准备完成。启动命令："
Write-Host "& `"$VenvPython`" webapp\server.py"
