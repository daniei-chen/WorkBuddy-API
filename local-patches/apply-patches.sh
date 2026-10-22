#!/usr/bin/env bash
# apply-patches.sh — 在 wb2api 源码树上重施本地补丁（README.md 有完整原理说明）。
#
# 由 update.py 在「更新流程」的构建前调用（见 deploy/update.py 的 update_upstream：
# git checkout/reset 会丢弃本地改动，此脚本把它们装回来）。也可手工执行。
#
# 设计约束：
#   1. 幂等：已应用的补丁跳过（git apply --reverse --check 判定）。
#   2. 清理遗留：补丁会新建测试文件；git checkout/reset **不清理未跟踪文件**，
#      上一轮应用留下、且此后被 checkout 还原的"半应用"状态会挡住 git apply
#      （报 already exists）。因此重新应用前，把该补丁将要创建、且未被 git
#      跟踪的同名文件删掉（只碰"本补丁创建 + 未被跟踪"的交集，绝不删已跟踪文件）。
#   3. fail-closed：任何补丁失败 → 非 0 退出 → 调用方中止更新（旧容器继续服务）。
#      源码打不上意味着构建产物缺本地能力，静默继续比中止更危险。
#   4. 只依赖 git；补丁来自本目录 patches/，按文件名排序应用。
set -uo pipefail

REPO=${WB2A_REPO:-/opt/workbuddy2api}
DIR="$(cd "$(dirname "$0")" && pwd)"
PATCHES="$DIR/patches"
LOG="$DIR/apply.log"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

cd "$REPO" || { log "✗ 仓库目录不存在: $REPO"; exit 1; }
[ -d "$PATCHES" ] || { log "✗ 补丁目录不存在: $PATCHES"; exit 1; }

shopt -s nullglob
files=("$PATCHES"/*.patch)
if [ ${#files[@]} -eq 0 ]; then
  log "无补丁文件，跳过"
  exit 0
fi

rc=0
for p in "${files[@]}"; do
  name="$(basename "$p")"
  if git apply --reverse --check "$p" >/dev/null 2>&1; then
    log "skip $name（已应用）"
    continue
  fi
  # 清理上一轮遗留：本补丁将要创建、但当前未被 git 跟踪的文件。
  while IFS= read -r created; do
    [ -n "$created" ] || continue
    if [ -e "$created" ] && ! git ls-files --error-unmatch "$created" >/dev/null 2>&1; then
      rm -f "$created"
      log "  cleanup $name：移除残留未跟踪文件 $created"
    fi
  done < <(git apply --summary "$p" 2>/dev/null | awk '/^ create mode/ {print $NF}')
  if git apply --check "$p" >/dev/null 2>&1; then
    if git apply "$p" >/dev/null 2>&1; then
      log "OK   $name 已应用"
    else
      log "✗ $name 应用失败（apply 报错）"; rc=1
    fi
  else
    log "✗ $name 无法应用：源码与补丁基线不匹配（上游可能改了同区域代码）"
    log "    人工处理：对照 $DIR/backup/ 与上游 diff 后修订补丁；"
    log "    或临时把该补丁移出 patches/ 再重试（放弃对应本地能力）。"
    rc=1
  fi
done
exit $rc
