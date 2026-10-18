# WorkBuddy Manager —— 本机启动脚本（Windows / PowerShell）
$ErrorActionPreference = 'Stop'
# 脚本住在 windows\ 子目录里：项目根（server/、web/out、.env、upstream/ 所在处）
# 是它的上一级。拼项目内的路径一律用 $root；只有找**同一目录里的兄弟脚本**才用 $scriptDir。
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

# 1. 环境准备（.env、上游配置、目录、Python 环境、前端产物；幂等，与桌面启动器共用）
& (Join-Path $scriptDir 'prepare.ps1')

# 2. 解析 Python 解释器（预置运行时优先，uv 虚拟环境兜底）
. (Join-Path $scriptDir 'python-env.ps1')
$python = Get-WbPython -Root $root
if (-not $python) {
    Write-Error "未找到可用的 Python 环境（.runtime\python 或 .venv）。请先运行 windows\prepare.ps1 完成准备。"
    exit 1
}

# 3. 编码设置与参数解析
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$bindHost = '127.0.0.1'
$bindPort = '7864'
$envPath = Join-Path $root '.env'
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*WB_MANAGER_HOST\s*=\s*(\S+)') { $bindHost = $Matches[1] }
        elseif ($line -match '^\s*WB_MANAGER_PORT\s*=\s*(\S+)') { $bindPort = $Matches[1] }
    }
}

# 4. 联动启动上游服务
$upstreamScript = Join-Path $root 'upstream\start-workbuddy2api.cmd'
$upstreamStopScript = Join-Path $root 'upstream\stop-workbuddy2api.cmd'
if (Test-Path $upstreamScript) {
    Write-Host "[INFO] 正在启动上游 workbuddy2api 网关服务..." -ForegroundColor Cyan
    & cmd.exe /c $upstreamScript
}

# 5. 启动主控面板并唤起浏览器
Write-Host "[INFO] WorkBuddy Manager 已就绪: http://${bindHost}:${bindPort}（按 Ctrl+C 退出）" -ForegroundColor Green
Start-Process "http://${bindHost}:${bindPort}"

try {
    & $python -m uvicorn server.main:app --env-file .env --host $bindHost --port $bindPort
} finally {
    if (Test-Path $upstreamStopScript) {
        & cmd.exe /c $upstreamStopScript
    }
}
