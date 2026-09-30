#!/usr/bin/env bash
# ============================================================
# 校验发布包签名
#
# 为什么需要：本项目的一键更新以 root 把发布包直接落盘并重启服务。
# 而「能发 Release」的门槛比想象中低——能合并 PR 的协作者、或被钓鱼的
# 维护者账号都能发版。签名把「能改代码」与「能发布可信产物」分开：
# 私钥离线保管、不进仓库也不进 CI，攻击者签不出名。
#
# 一键更新已内置同样的校验（见 deploy/update.py）；本脚本供**手动安装**时
# 在解压前自行校验，避免「下载即信任」。
#
# 用法：
#   bash deploy/verify-release.sh workbuddy-manager-v1.0.24.tar.gz
#   bash deploy/verify-release.sh workbuddy-manager-v1.0.24.tar.gz custom.pub
#
# 退出码：0 = 验签通过；非 0 = 失败（**不要解压安装**）
# ============================================================
set -euo pipefail

ARCHIVE="${1:-}"
PUBKEY_FILE="${2:-}"

if [ -z "$ARCHIVE" ]; then
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi

[ -f "$ARCHIVE" ] || { echo "✗ 找不到文件：$ARCHIVE" >&2; exit 2; }
command -v ssh-keygen >/dev/null 2>&1 || { echo "✗ 需要 ssh-keygen（OpenSSH 8.0+）" >&2; exit 2; }

SIG="${ARCHIVE}.sig"
[ -f "$SIG" ] || {
  echo "✗ 找不到签名文件：$SIG" >&2
  echo "  正常发布流程会同时提供 .tar.gz 与 .tar.gz.sig；缺失说明产物不完整，请勿安装。" >&2
  exit 3
}

# 公钥：优先用传入的文件，其次用仓库内的 release-signing-key.pub
if [ -z "$PUBKEY_FILE" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  PUBKEY_FILE="${HERE}/release-signing-key.pub"
fi
[ -f "$PUBKEY_FILE" ] || { echo "✗ 找不到公钥文件：$PUBKEY_FILE" >&2; exit 2; }

SIGNER_ID="release"
TMP_SIGNERS="$(mktemp)"
trap 'rm -f "$TMP_SIGNERS"' EXIT
# ssh-keygen 要求 allowed_signers 格式（纯 .pub 文件不行），这里按约定拼一行
printf '%s %s\n' "$SIGNER_ID" "$(cat "$PUBKEY_FILE")" > "$TMP_SIGNERS"

if ssh-keygen -Y verify -f "$TMP_SIGNERS" -I "$SIGNER_ID" -n file -s "$SIG" < "$ARCHIVE"; then
  echo "✓ 签名校验通过，可以解压安装"
else
  cat >&2 <<'EOF'

✗ 签名校验失败 —— 请勿解压安装！

  可能原因：
    1. 产物被替换或篡改（这正是校验要拦的情况）
    2. 签名文件与产物不匹配（下载不完整，可重新下载再试）
    3. 公钥不是发布者的（如换了密钥，请用最新的 release-signing-key.pub）

  确认包来源可信之前，不要执行解压与安装。
EOF
  exit 1
fi
