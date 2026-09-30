#!/usr/bin/env bash
# 服务启动时重施本地补丁（管理端更新会覆盖 server/，靠它自动恢复）。
# 设计：任何失败都不阻塞服务启动（记日志、exit 0）；补丁本身幂等。
set -u
LOG=/opt/workbuddy-manager/data/local-patches.log
PY=/opt/workbuddy-manager/venv/bin/python
DIR=/opt/workbuddy-manager/local-patches
{
  echo "[$(date '+%F %T')] apply_patches start"
  "$PY" "$DIR/patch_session_inject.py" /opt/workbuddy-manager/server/routers/gateway.py 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_cache_tokens.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_viewer_perms.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_viewer_open.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_usage_cache.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_stats_cache.py" 2>&1 | sed 's/^/  /'
"$PY" "$DIR/patch_usage_endpoint.py" 2>&1 | sed 's/^/  /'
"$PY" "$DIR/patch_keysvc_models.py" 2>&1 | sed 's/^/  /'
"$PY" "$DIR/patch_bump_cache.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_zhihui_ui.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_login_loop.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_upstream_retry_after.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_upstream_retry_body.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_upstream_timeout.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_upstream_repo.py" 2>&1 | sed 's/^/  /'
  "$PY" "$DIR/patch_usage_by_model.py" 2>&1 | sed 's/^/  /'
} >> "$LOG" 2>&1
exit 0
