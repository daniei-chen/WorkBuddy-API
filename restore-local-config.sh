#!/usr/bin/env bash
# 恢复本地定制（更新后若构建/配置异常，跑这个）
set -u
cd /opt/workbuddy2api
S=.local-config
[ -d "$S" ] || { echo "✗ 找不到 $S 备份"; exit 1; }
cp -a "$S/Dockerfile" Dockerfile
cp -a "$S/docker-compose.yml" docker-compose.yml
[ -f "$S/docker-compose.override.yml" ] && cp -a "$S/docker-compose.override.yml" docker-compose.override.yml
git update-index --assume-unchanged Dockerfile docker-compose.yml 2>/dev/null
echo "✓ 本地配置已恢复"
grep -q GOPROXY Dockerfile && echo "  GOPROXY ✓"
grep -q "127.0.0.1:7863" docker-compose.yml && echo "  端口收敛 ✓"
grep -q WB2A_MAX_BODY_MB docker-compose.override.yml && echo "  WB2A_* ✓"
