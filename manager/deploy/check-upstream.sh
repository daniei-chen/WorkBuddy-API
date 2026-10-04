#!/usr/bin/env bash
# ============================================================
# 发版前检查上游是否有新提交
#
# 为什么要这个脚本：上游改动多数**不会让管理端报错**，只会让某处行为悄悄
# 失配（参数位置、请求头形态、默认值翻转、错误码语义）。靠人记得去查不可靠，
# 所以做成一条命令：有更新就以非 0 退出，逼你在发版前处理。
#
# 用法：
#   bash deploy/check-upstream.sh            # 只检查
#   bash deploy/check-upstream.sh <ref>      # 指定「上次适配到的上游提交」
#
# 退出码：0 = 无更新（可以发版）；1 = 有更新（先读改动再决定）；2 = 检查失败
# ============================================================
set -euo pipefail

UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/Sliverkiss/workbuddy2api.git}"
PINNED="${1:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "[·] 拉取上游仓库（浅克隆，不取文件内容）…"
if ! CLONE_OUT="$(git clone --quiet --filter=blob:none --no-checkout "$UPSTREAM_REPO" "$WORK/up" 2>&1)"; then
  # 分两种：仓库没了 vs 网络/代理问题。前者不是故障，别再让人去查代理。
  # 判据取 git/GitHub 的原始报文：`Repository not found` / `repository '…' not found`
  # / `404`。不加裸的 "not found"——别的失败里也可能出现这两个词（如 DNS 报错），
  # 误判成「仓库没了」会让人跳过本该做的检查。
  case "$CLONE_OUT" in
    *Repositor*"not found"* | *404*)
      echo "[—] 上游仓库不可访问（$UPSTREAM_REPO）——自 2026-09-23 起该地址不再可用。"
      echo "    这一步从此不再需要：上游已停更，不必再核对新提交，直接发版即可。"
      echo "    （若你已把上游迁到自己的副本，可用 UPSTREAM_REPO=<地址> 指过来，"
      echo "      本脚本会继续按老规矩检查。）"
      exit 0 ;;
  esac
  echo "[✗] 无法访问上游仓库（网络或代理问题）" >&2
  echo "    git 的报错原文：${CLONE_OUT##*$'\n'}" >&2
  echo "    提示：本机代理可能是 http://127.0.0.1:7890，可用" >&2
  echo "    git -c http.proxy=... 或先设置 https_proxy 环境变量" >&2
  exit 2
fi

cd "$WORK/up"
HEAD_SHA="$(git rev-parse --short HEAD)"
HEAD_MSG="$(git log -1 --pretty=%s)"

if [ -z "$PINNED" ]; then
  echo "[·] 上游最新：$HEAD_SHA  $HEAD_MSG"
  echo
  echo "[!] 未指定「上次适配到的提交」，无法判断是否有更新。"
  echo "    请查看最近的 adapt(upstream) 提交，把那里的上游提交号作为参数："
  echo "      git log --oneline --grep='adapt(upstream)' -1"
  echo "      bash deploy/check-upstream.sh <该提交号>"
  exit 2
fi

if ! git cat-file -e "$PINNED^{commit}" 2>/dev/null; then
  echo "[!] 找不到提交 $PINNED（可能被上游 rebase 掉了）" >&2
  echo "    上游最新：$HEAD_SHA  $HEAD_MSG" >&2
  exit 2
fi

if [ "$(git rev-parse "$PINNED")" = "$(git rev-parse HEAD)" ]; then
  echo "[✓] 上游无更新（仍是你适配过的 $PINNED）——可以发版"
  exit 0
fi

COUNT="$(git rev-list --count "$PINNED"..HEAD)"
echo "[!] 上游有 $COUNT 个新提交（$PINNED → $HEAD_SHA），**先适配再发版**："
echo
git log --oneline --no-decorate "$PINNED"..HEAD | sed 's/^/    /'
echo
echo "    逐个读改动：git log --stat $PINNED..HEAD"
echo "    重点看两类："
echo "      1) 入站协议/响应形状是否变了（影响我们的转发路径）"
echo "      2) 直连腾讯那批（登录/签到/积分/注册/探测）的参照实现是否变了"
echo "         —— 上游改了**不会**自动惠及我们这条路"
exit 1
