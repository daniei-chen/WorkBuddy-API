#!/usr/bin/env bash
# 构建并签名一个受控维护版本的发布树（与现网 source_preflight.py 校验的布局一致）。
set -euo pipefail

REPO="${REPO:-/home/agentuser/wbfork/repo}"
OUT="${OUT:-/home/agentuser/wbfork/stage}"
VERSION="${VERSION:-v2.4.0+local.$(date +%Y%m%d).1}"
KEY="${KEY:-/root/wb-release/release-signing.key}"
PUB="${PUB:-/root/wb-release/release-signing.pub}"

ROOT="$OUT/$VERSION"
echo "==> 构建 $ROOT"
rm -rf "$ROOT"
mkdir -p "$ROOT"

echo "==> server/"
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
      --exclude='.pytest_cache' "$REPO/server/" "$ROOT/server/"

echo "==> web/out/"
mkdir -p "$ROOT/web/out"
rsync -a --exclude='__pycache__' "$REPO/web/out/" "$ROOT/web/out/"

echo "==> deploy/"
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='managed-release.json' --exclude='managed-release.sig' \
      "$REPO/deploy/" "$ROOT/deploy/"

echo "==> local-patches/"
mkdir -p "$ROOT/local-patches"
cat > "$ROOT/local-patches/apply_patches.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
exec /opt/workbuddy-manager/deploy/source_preflight.py
SH

echo "==> .version"
printf '%s\n' "$VERSION" > "$ROOT/.version"

echo "==> 权限规范化"
find "$ROOT" -type d -exec chmod 755 {} +
find "$ROOT" -type f -exec chmod 644 {} +
for f in deploy/check-upstream.sh deploy/install.sh deploy/purge_credentials.py \
         deploy/source_preflight.py deploy/update.py \
         deploy/update.py.pre-srcpatch-20260916-153909 deploy/verify-release.sh \
         local-patches/apply_patches.sh server/backup_application.py; do
  [ -f "$ROOT/$f" ] && chmod 755 "$ROOT/$f"
done

echo "==> data -> /opt/workbuddy-manager/data"
ln -sfn /opt/workbuddy-manager/data "$ROOT/data"

echo "==> 签名"
python3 /home/agentuser/wbfork/tools/sign_release.py \
  --root "$ROOT" --key "$KEY" --pub "$PUB" --version "$VERSION"

echo "==> done: $ROOT"
