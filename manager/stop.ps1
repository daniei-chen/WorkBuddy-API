# WorkBuddy Manager —— 停止本机服务（Windows / PowerShell）
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $root 'service-tools.ps1') stop

$upstreamStop = Join-Path $root 'upstream\stop-workbuddy2api.cmd'
if (Test-Path $upstreamStop) {
    & cmd.exe /c $upstreamStop
}
Write-Host "已停止所有 WorkBuddy 服务。"
