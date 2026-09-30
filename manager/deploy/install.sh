#!/usr/bin/env bash
# ============================================================
# WorkBuddy Manager 一键部署脚本
#
# 本项目管理端依赖上游 workbuddy2api（提供账号池与 OpenAI 兼容接口）。
# 单独 clone 本仓库是跑不起来的 —— 本脚本会自动检测并安装上游，
# 因此在一台干净机器上执行即可完成整套部署。
#
# 用法：
#   sudo bash deploy/install.sh                # 完整安装（含上游）
#   sudo bash deploy/install.sh --skip-upstream  # 已自备 workbuddy2api
#   sudo bash APP_DIR=/opt/custom bash deploy/install.sh
#
# 说明：workbuddy2api 的账号登录（扫码）是交互式的，无法自动化；
#       安装完成后通过本管理端的「添加账号」扫码即可。
# ============================================================
set -euo pipefail

# ── 可配置项（均可用环境变量覆盖）─────────────────────────
APP_DIR="${APP_DIR:-/opt/workbuddy-manager}"
UPSTREAM_DIR="${UPSTREAM_DIR:-/opt/workbuddy2api}"
UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/daniei-chen/WorkBuddy-API.git}"
UPSTREAM_PORT="${UPSTREAM_PORT:-7863}"
MANAGER_PORT="${MANAGER_PORT:-7864}"
PY="${PY:-/usr/bin/python3}"
SKIP_UPSTREAM=0

for arg in "$@"; do
  case "$arg" in
    --skip-upstream) SKIP_UPSTREAM=1 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

# ── 输出helpers ───────────────────────────────────────────
info()  { printf '\033[1;34m[·]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }
step()  { printf '\n\033[1;36m══ %s\033[0m\n' "$*"; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 0. 预检 ───────────────────────────────────────────────
step "0/4 环境预检"

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo bash deploy/install.sh）"

command -v "$PY" >/dev/null 2>&1 || die "未找到 python3（$PY）"
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
"$PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' \
  || die "Python 版本过低（需 ≥3.9，当前 ${PYVER}）"
ok "Python ${PYVER}"

# 签名一致性自检（能查则查，不阻断）─────────────────────────
# 注意本脚本运行时包**已经在磁盘上**了，所以它拦不住"解压了恶意包"——
# 真正的前置防线是解压前执行 deploy/verify-release.sh。
# 这里做的是"矛盾状态检查"：同一目录里既放 .sig 又放 release-signing-key.pub
# 却验不过，说明产物被动过或公钥过期，必须让人看见。
if command -v ssh-keygen >/dev/null 2>&1; then
  _sig="$(find . -maxdepth 1 -name '*.tar.gz.sig' -print -quit 2>/dev/null || true)"
  _pub="${SRC_DIR}/deploy/release-signing-key.pub"
  if [ -n "${_sig}" ] && [ -f "${_pub}" ] && ! grep -q 'AAAA_REPLACE_ME' "${_pub}"; then
    _pkg="${_sig%.sig}"
    _signers="$(mktemp)"
    printf 'release %s\n' "$(cat "${_pub}")" > "${_signers}"
    if [ -f "${_pkg}" ] && ! ssh-keygen -Y verify -f "${_signers}" -I release -n file -s "${_sig}" < "${_pkg}" >/dev/null 2>&1; then
      rm -f "${_signers}"
      die "签名校验失败：${_pkg} 与签名/公钥不匹配 —— 产物可能被替换，停止安装"
    fi
    rm -f "${_signers}"
  fi
fi

HAVE_DOCKER=0
if command -v docker >/dev/null 2>&1; then
  HAVE_DOCKER=1
  DOCKER_VER="$(docker --version | awk '{print $3}' | tr -d ,)"
  ok "Docker ${DOCKER_VER}"
  # 优先 compose v2，退化到 docker-compose v1
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    die "未找到 docker compose（请安装 docker-compose-plugin）"
  fi
  ok "Compose: ${COMPOSE}"
else
  warn "未检测到 Docker"
fi

check_port() {
  local port="$1" name="$2"
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${port}\b"; then
    warn "端口 ${port}（${name}）已被占用，稍后可能冲突"
  fi
}
check_port "$UPSTREAM_PORT" "上游 workbuddy2api"
check_port "$MANAGER_PORT" "管理端"

# ── 1. 安装上游 workbuddy2api ─────────────────────────────
step "1/4 上游 workbuddy2api"

if [ "$SKIP_UPSTREAM" -eq 1 ]; then
  info "按参数要求跳过（--skip-upstream）"
elif [ "$HAVE_DOCKER" -eq 0 ]; then
  warn "无 Docker，跳过上游安装。请自行部署 workbuddy2api 于 :${UPSTREAM_PORT}"
elif [ -f "${UPSTREAM_DIR}/config.json" ]; then
  ok "已存在上游部署（${UPSTREAM_DIR}），保留现有配置与账号"
  if [ -d "${UPSTREAM_DIR}/.git" ]; then
    info "如需升级上游：cd ${UPSTREAM_DIR} && git pull && ${COMPOSE} up -d --build"
  fi
  # 确保容器在运行
  ( cd "$UPSTREAM_DIR" && $COMPOSE up -d ) >/dev/null 2>&1 || \
    warn "上游容器启动失败，请检查：cd ${UPSTREAM_DIR} && ${COMPOSE} logs"
else
  info "未检测到上游部署，开始安装到 ${UPSTREAM_DIR}"

  command -v git >/dev/null 2>&1 || die "需要 git 来克隆上游仓库"

  if [ -d "${UPSTREAM_DIR}/.git" ]; then
    info "目录已存在，拉取最新代码"
    ( cd "$UPSTREAM_DIR" && git pull --ff-only ) || warn "git pull 失败，沿用现有代码"
  else
    info "克隆 ${UPSTREAM_REPO}"
    git clone --depth 1 "$UPSTREAM_REPO" "$UPSTREAM_DIR"
  fi

  cd "$UPSTREAM_DIR"

  # 生成配置：绝不覆盖已有 config.json（含 api_key 与账号状态）
  if [ ! -f config.json ]; then
    if [ -f config.example.json ]; then
      cp config.example.json config.json
    else
      die "缺少 config.example.json，无法生成配置"
    fi
    # 生成强随机 api_key
    API_KEY="$("$PY" -c 'import secrets;print(secrets.token_hex(16))')"
    "$PY" - "$API_KEY" <<'PYEOF'
import json, sys
p = 'config.json'
cfg = json.load(open(p, encoding='utf-8'))
cfg['api_key'] = sys.argv[1]
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
PYEOF
    ok "已生成配置（api_key 随机）"
  else
    info "已存在 config.json，保留不动"
  fi

  # 容器内以 uid 10001 运行，bind-mount 的目录需归属该 uid，否则无写权限
  mkdir -p auths data
  chown -R 10001:10001 auths data
  ok "账号目录已就绪（属主 10001）"

  info "构建并启动上游容器（首次构建需数分钟）"
  $COMPOSE up -d --build

  info "等待上游就绪…"
  for i in $(seq 1 60); do
    if curl -sf -m 3 "http://127.0.0.1:${UPSTREAM_PORT}/healthz" >/dev/null 2>&1; then
      ok "上游已就绪（http://127.0.0.1:${UPSTREAM_PORT}）"
      break
    fi
    [ "$i" -eq 60 ] && warn "等待超时，请查看：cd ${UPSTREAM_DIR} && ${COMPOSE} logs"
    sleep 2
  done
fi

# ── 2. 安装管理端 ─────────────────────────────────────────
step "2/4 管理端 WorkBuddy Manager"

mkdir -p "${APP_DIR}/data"

if [ "${SRC_DIR}" != "${APP_DIR}" ]; then
  info "同步代码到 ${APP_DIR}"
  cp -r "${SRC_DIR}/server" "${APP_DIR}/"
  cp -r "${SRC_DIR}/deploy" "${APP_DIR}/"
  cp "${SRC_DIR}/.env.example" "${APP_DIR}/" 2>/dev/null || true
  # 文档：界面的「更新日志」页直接读它（server/ 内另有一份副本兜底）
  cp "${SRC_DIR}/CHANGELOG.md" "${APP_DIR}/" 2>/dev/null || true
  cp "${SRC_DIR}/README.md" "${APP_DIR}/" 2>/dev/null || true
fi

# 静态前端：优先用发布包内的 web/out；否则尝试本地构建
mkdir -p "${APP_DIR}/web"
if [ -f "${APP_DIR}/web/out/index.html" ]; then
  ok "已包含构建好的前端（web/out）"
elif [ -d "${SRC_DIR}/web/out" ]; then
  cp -r "${SRC_DIR}/web/out" "${APP_DIR}/web/"
  ok "已复制前端产物"
elif [ -f "${SRC_DIR}/web/package.json" ]; then
  warn "未找到前端产物，尝试本地构建"
  if command -v npm >/dev/null 2>&1; then
    ( cd "${SRC_DIR}/web" && npm ci && npm run build:export )
    cp -r "${SRC_DIR}/web/out" "${APP_DIR}/web/"
    ok "前端构建完成"
  else
    die "需要 Node.js 构建前端；或下载 Release 包（内含已构建的 web/out）"
  fi
else
  die "缺少前端产物 web/out，请使用 Release 包或先执行 npm run build:export"
fi

# 写入版本标记（供界面显示当前版本）
if [ -f "${APP_DIR}/.version" ]; then
  ok "版本标记：$(cat "${APP_DIR}/.version")"
elif [ -f "${SRC_DIR}/.version" ]; then
  cp "${SRC_DIR}/.version" "${APP_DIR}/.version"
fi

info "安装 Python 依赖"
"$PY" -m venv "${APP_DIR}/venv" 2>/dev/null || true
if [ -x "${APP_DIR}/venv/bin/pip" ]; then
  "${APP_DIR}/venv/bin/pip" install -q --upgrade pip
  "${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/server/requirements.txt"
  RT_PY="${APP_DIR}/venv/bin/python"
  ok "依赖已装入 venv"
else
  "$PY" -m pip install -q --break-system-packages -r "${APP_DIR}/server/requirements.txt" 2>/dev/null \
    || "$PY" -m pip install -q -r "${APP_DIR}/server/requirements.txt"
  RT_PY="$PY"
  ok "依赖已装入系统 Python"
fi

info "注册 systemd 服务"
sed -e "s#/opt/workbuddy-manager#${APP_DIR}#g" \
    -e "s#--port 7864#--port ${MANAGER_PORT}#g" \
    -e "s#WB_MANAGER_PORT=7864#WB_MANAGER_PORT=${MANAGER_PORT}#g" \
    -e "s#WB2API_BASE=http://127.0.0.1:7863#WB2API_BASE=http://127.0.0.1:${UPSTREAM_PORT}#g" \
    -e "s#WB_AUTH_DIR=/opt/workbuddy2api/auths#WB_AUTH_DIR=${UPSTREAM_DIR}/auths#g" \
    -e "s#WB_UPSTREAM_CONFIG=/opt/workbuddy2api/config.json#WB_UPSTREAM_CONFIG=${UPSTREAM_DIR}/config.json#g" \
    "${APP_DIR}/deploy/workbuddy-web.service" > /etc/systemd/system/workbuddy-web.service

# 若 venv 可用，改写 ExecStart 指向 venv
if [ "$RT_PY" != "$PY" ]; then
  sed -i "s#^ExecStart=.*#ExecStart=${APP_DIR}/venv/bin/python -m uvicorn server.main:app --host 0.0.0.0 --port ${MANAGER_PORT}#" \
    /etc/systemd/system/workbuddy-web.service
fi

systemctl daemon-reload
systemctl enable --now workbuddy-web >/dev/null 2>&1
sleep 3
systemctl is-active --quiet workbuddy-web && ok "服务已启动" || {
  warn "服务未正常运行，最近日志："
  journalctl -u workbuddy-web -n 20 --no-pager || true
}

# ── 3. 验证 ───────────────────────────────────────────────
step "3/4 验证"

if curl -sf -m 5 "http://127.0.0.1:${MANAGER_PORT}/api/healthz" >/dev/null 2>&1; then
  ok "管理端 http://127.0.0.1:${MANAGER_PORT} 可访问"
else
  warn "管理端未响应，请查看：journalctl -u workbuddy-web -n 50"
fi

if curl -sf -m 5 "http://127.0.0.1:${MANAGER_PORT}/healthz" >/dev/null 2>&1; then
  ok "已连通上游（$(curl -s -m 5 http://127.0.0.1:${MANAGER_PORT}/healthz)）"
else
  warn "管理端暂时读不到上游状态；确认上游容器在运行后刷新页面即可"
fi

# ── 4. 完成 ───────────────────────────────────────────────
step "4/4 完成"

ADMIN_PWD_LINE="$(journalctl -u workbuddy-web -n 200 --no-pager 2>/dev/null | grep -A3 '初始管理员' | tail -n +2 | head -3 || true)"

cat <<EOF

  ┌──────────────────────────────────────────────────────┐
  │  部署完成                                            │
  └──────────────────────────────────────────────────────┘

  管理端      http://127.0.0.1:${MANAGER_PORT}
  上游        http://127.0.0.1:${UPSTREAM_PORT}   （workbuddy2api，仅本机）
  配置目录    ${APP_DIR}
  账号目录    ${UPSTREAM_DIR}/auths
EOF

if [ -n "$ADMIN_PWD_LINE" ]; then
  echo ""
  echo "  初始管理员密码（仅首次生成）："
  echo "$ADMIN_PWD_LINE" | sed 's/^/    /'
else
  echo ""
  echo "  登录账号：admin / 首次启动时随机生成（见下方命令）"
  echo "    journalctl -u workbuddy-web | grep -A3 '初始管理员'"
fi

cat <<EOF

  下一步：
    1. 浏览器打开管理端（公网访问请先配置 HTTPS 反向代理，见 deploy/README.md）
    2. 登录后进入「账号」→「添加账号」，用微信 / QQ 扫码授权
    3. 授权后自动签到并纳管，即可在「密钥」页创建调用密钥

  常用命令：
    journalctl -u workbuddy-web -f          # 管理端日志
    cd ${UPSTREAM_DIR} && ${COMPOSE} logs -f  # 上游日志
    systemctl restart workbuddy-web         # 重启管理端
    cd ${UPSTREAM_DIR} && ${COMPOSE} restart  # 重启上游

EOF
