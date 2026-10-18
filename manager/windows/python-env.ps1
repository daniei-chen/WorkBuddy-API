# WorkBuddy Manager —— Python 解释器解析（供其他脚本 dot-source 使用）
# 预置运行时（.runtime\python，随安装包分发）优先；uv 建的虚拟环境（.venv）兜底。

function Get-WbPython {
    param([Parameter(Mandatory = $true)][string]$Root)
    foreach ($rel in @('.runtime\python\python.exe', '.venv\Scripts\python.exe')) {
        $path = Join-Path $Root $rel
        if (Test-Path $path) { return $path }
    }
    return $null
}
