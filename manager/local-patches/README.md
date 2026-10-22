# 本地补丁（管理端 + 关联组件）

> 2026-09-20：管理端已升级至 **v1.0.57**（原 1.0.37）。cache / 用量相关的三个补丁
> 已适配双代码世代（1.0.37 与 1.0.57+ 的重构结构），升级重启时全部自动重施成功
> （见 `data/local-patches.log` 中 00:27 的批次）。

## 补丁清单
| 补丁 | 目标文件 | 作用 |
|---|---|---|
| `patch_session_inject.py` | server/routers/gateway.py | 注入 x-session-id 为 conversation_id，稳定上游缓存命中 |
| `patch_cache_tokens.py` | server/routers/gateway.py + server/db.py | 采集 prompt_cache_hit/miss_tokens 落库（**双版本兼容**：1.0.37 内联 SQL / 1.0.57+ 的 add_request_log 结构） |
| `patch_viewer_perms.py` | server/routers/accounts.py | 只读账号可用实时积分查询与添加账号（4 个端点 require_admin→current_user） |
| `patch_viewer_open.py` | server/routers/playground.py + server/routers/settings.py | 只读角色开放聊天测试台（`chat` require_admin→current_user）；错误文案「至少保留一个管理员」→「最高管理员」 |
| `patch_usage_cache.py` | server/db.py | 修复 backfill / rebuild 从日志重建用量时**丢失缓存列**的问题（**双版本兼容**，含 1.0.57 的事务化 executemany 分支） |
| `patch_stats_cache.py` | server/routers/stats.py | `/api/stats/daily`、`/by-model`、`/by-key` 增加 cache_hit/miss 字段（**双版本兼容**，daily 返回块含 failed 的新旧两式） |
| `patch_bump_cache.py` | server/db.py + server/routers/gateway.py | usage_daily 聚合层补缓存列迁移 + bump_usage 写入链 + 官方 1.0.71 rebuild 参数顺序 bug 修正（防命中率冻结/错位） |
| `patch_keysvc_models.py` | server/keysvc.py | 模型白名单归一化：全角/半角逗号统一拆分（防"模型A，模型B"整体入库导致白名单拒一切） |
| `patch_usage_endpoint.py` | server/routers/gateway.py + server/tests/ | 新增 `GET /v1/usage` 按密钥用量查询（当天 / 近 7 天 / 近 30 天：请求数、Token、缓存命中率；不含实付；配额用尽的密钥也可查） |
| `patch_zhihui_ui.py` | /opt/zhihui-manager-ui（前端产物） | 角色显示名改名（简繁双语）+ 测试台界面解锁 + 用量统计页（tooltip 命中率、「近 1 天」、按模型/按密钥表格命中率列） |
| `../quota-dashboard/patch_cachehit.py` | /opt/quota-dashboard/app.py | 看板显示「今日缓存命中率」（一次性；dashboard 无自动更新） |

## 一次性脚本
| 脚本 | 作用 | 备注 |
|---|---|---|
| `fix_cache_history.py` | 按 request_logs 重算 usage_daily 的缓存列（修复历史归零数据） | 幂等；执行前自动 VACUUM INTO 备份整库 |

## 自动重施
- manager 侧：systemd drop-in `workbuddy-web.service.d/local-patches.conf` 的
  `ExecStartPre` 在服务启动前执行 `apply_patches.sh`；管理端更新会重启服务，因此
  server/ 被覆盖后能自动恢复。cache/用量三个补丁按代码特征自动选择锚点集
  （`db.add_request_log(` / `def add_request_log(` 判定 1.0.57+；否则 1.0.37），
  缺失的可选锚点跳过、计数异常则中止不写（fail-closed）。
- `patch_zhihui_ui.py`（前端产物）同样挂在补丁链上，文件按通配定位
  （`613-*.js`、`stats/page-*.js` 等）——前端重新构建导致文件名 hash 变化后依然可用；
  若文件整个缺失会报 ERROR 并跳过（不写坏文件）。
- wb2api 源码补丁：`/opt/workbuddy2api/local-patches/`（apply-patches.sh +
  patches/*.patch），由 deploy/update.py 的 `apply_source_patches` 在构建前调用；
  失败会**中止更新**（fail-closed）。
- quota-dashboard：无自动更新机制，补丁为一次性施加。

## 回退
```
# manager 补丁（backup/ 内为各补丁首次施加前的原文件）
cp backup/gateway.py.orig-* server/routers/gateway.py
cp backup/db.py.orig-cachehit server/db.py
cp backup/db.py.orig-usage-cache-* server/db.py
cp backup/accounts.py.orig-viewer-perms-* server/routers/accounts.py
cp backup/playground.py.orig-viewer-open-* server/routers/playground.py
cp backup/settings.py.orig-role-rename-* server/routers/settings.py
cp backup/stats.py.orig-daily-cache-* server/routers/stats.py
cp backup/stats.py.orig-cache-v2-* server/routers/stats.py
cp backup/gateway.py.orig-usage-endpoint-* server/routers/gateway.py
# 前端：用对应 chunk 旁的 .bak-* 备份覆盖回去，或直接重新部署 UI
# 数据：manager.db.bak-cachefix-*（fix_cache_history 执行前的整库备份）
# 管理端整体回滚：/opt/workbuddy-manager/backup-20260920-002755（升级前的 1.0.37）
# wb2api 源码补丁：移除对应 .patch 文件后重建容器
# dashboard
cp app.py.bak-cachehit-* app.py && systemctl restart quota-dashboard
```
