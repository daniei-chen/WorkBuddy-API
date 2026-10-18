# 本地 fork 说明（v2.4.0 + 本地定制）

> 生成时间：2026-10-18。这份文件**不在**签名清单（`deploy/managed-release.json`）里，
> 可以直接编辑；改它不会影响 `ExecStartPre` 的托管校验。

## 现在跑的是什么

| 项 | 值 |
|---|---|
| 发布目录 | `/opt/workbuddy-manager/releases/v2.4.0+local.20261018.1`（`releases/current` 指向它） |
| 版本标记 | `/opt/workbuddy-manager/.version` = `v2.4.0+local.20261018.1` |
| 上游基线 | `ithtelab/workbuddy-manager` v2.4.0（2026-10-18 发布） |
| 托管文件 | 488 个（server / web/out / deploy / local-patches），逐文件 sha256 + 权限位 |
| 本地源码仓库 | `/home/agentuser/wbfork/repo`（git，分支 `master`） |

## 源码仓库怎么用

```
git -C /home/agentuser/wbfork/repo log --oneline
upstream-v1.0.79         官方 1.0.79 源码（基线）
local-closure-20261005.7 合并前现网源码（本地定制的完整快照）
upstream-v2.4.0          官方 2.4.0 源码
master                   合并结果（部署用的就是这棵树）
```

以后升级：`git fetch → git merge upstream-vX.Y.Z → 跑测试 → build_release.sh → 切换 releases/current`。

## 签名与发布（重要）

现网改成**由本机自签**的托管发布：

* 信任锚：`/opt/workbuddy-manager/release-trust.pem`（本次已替换为 fork 公钥）
* 私钥：`/root/wb-release/release-signing.key`（`0400 root`）
  **请把它离线保存（U 盘 / 密码管理器），不要留在服务器上。**
  丢了它就签不出新发布包，只能再换一次信任锚。
* 旧的信任锚备份：`/opt/workbuddy-manager/backup-pre-20261018-1835/release-trust.pem.orig`

发布流程：

```
# 1) 构建 + 自动签名（产出 /home/agentuser/wbfork/stage/<版本>）
sudo /opt/workbuddy-manager/fork-tools/build_release.sh
# 2) 离线自检（验签 + 逐文件哈希/权限，等价于 source_preflight 的逻辑）
sudo python3 /opt/workbuddy-manager/fork-tools/verify_tree.py <发布树> /root/wb-release/release-signing.pub
# 3) 备份 → 切 current → 重启
```

## 回滚（3 分钟）

```
BK=/opt/workbuddy-manager/backup-pre-20261018-1835
ln -sfn "$(cat $BK/previous-release.txt)" /opt/workbuddy-manager/releases/current
cp -a "$BK/release-trust.pem.orig" /opt/workbuddy-manager/release-trust.pem
cp -a "$BK/.version.orig" /opt/workbuddy-manager/.version
systemctl restart workbuddy-web
```

若数据库迁移出问题：先 `systemctl stop workbuddy-web`，再把 `$BK/manager.db` 拷回
`/opt/workbuddy-manager/data/manager.db`（同时删掉 `-wal`/`-shm`），然后按上面回滚代码。

## 保留的本地定制（合并时逐项确认过）

* 运行时覆盖：`local-overrides/sitecustomize.py`（全局准入 60 / 每密钥 50 / 未结算 50；
  托管源码保持原样，靠 systemd drop-in 的 `PYTHONPATH` 注入）
* 新增模块：`bodyguard.py`（管理面 body 守卫）、`budget*.py` + `ledger.py`（额度租约与账本）、
  `observability.py`（请求审计/流式审计/准入）、`pooledhttp.py`、`upstreamschema.py` +
  `upstream-schema.json`（2api 配置写入白名单）、`operational.py`、`offsitebackup.py`、
  `routers/budgets.py`、`backup_application.py`
* 网关侧：会话键注入（x-session-id → conversation_id）、只读角色放开、缓存命中字段统计、
  `/v1/usage` 用量自查、按上游身份分池的出站连接、502 抖动提示
  （`Retry-After` + `X-Upstream-Blip` + 错误体 `retry_after_seconds`）、max_tokens 钳制
* 面板侧：托管发布/验签链路（`deploy/managed-release.json` + `source_preflight.py`）、
  网页更新入口按受控维护版本封禁、`deploy/` 作为信任锚不同步
* systemd drop-in 与 nginx 配置均未改动

## 吸收的上游改动（v1.0.79 → v2.4.0 摘要）

* 缓存命中 token 统计、`/v1/usage` 配额自查（**已改挂 `/v1/key`**）・模型白名单归一
* 出站客户端池（按事件循环/超时/代理/lane 复用 + 内网直连判断）
* PostgreSQL 备份/恢复重写（先写临时副本、毫秒级原子替换，恢复期间不阻塞）
* 新的管理面：API Token、多上游、代理池、Windows 原生脚本、离线升级包
* 数据库保留期策略、配置原子写 + 并发锁、可访问性与错误提示改进
* 前端整体升级到 v2.4.0（本地 UI 定制在源码层合并后重新构建）

## 与上游不同的三处（有意为之）

1. **`/v1/usage` 保留本地的“按密钥用量统计”**（today/7d/30d、缓存命中率、by_model、live 速度）；
   上游 issue #137 的“配额自查”改挂 **`/v1/key`**。
2. **请求体上限：采信上游 `config.json` 的 `server.max_body_mb`（本机 2api = 1024MB），
   但网关侧**保留硬顶** `WB_GATEWAY_MAX_BODY_CEILING_MB`，现网设为 **512MB**。
   取值链：上游 `server.max_body_mb` → 本端 `WB_GATEWAY_MAX_BODY_MB`（仅上游无该键时）
   → 硬顶 512MB。生产实测 `source=upstream+ceiling`、生效 512MB。
   上限始终由网关这一跳执行：本端要把完整 body 缓冲进内存再 `json.loads`
   （峰值约体积的 2~3 倍），而机器只有 3.7GB 内存，1024MB 等于允许一个请求吃穿整机。
   硬顶配置在 `/etc/systemd/system/workbuddy-web.service.d/zz-body-ceiling.conf`，
   要调大：改这一行 → `systemctl daemon-reload && systemctl restart workbuddy-web`。
   nginx 侧 `client_max_body_size` 是 1024m（外层更宽，真正的闸在网关）。
3. **网页一键更新保持封禁**（`deploy/managed-release.json` 存在即拒绝），升级只走签名发布流程。

## 测试状态

`python -m unittest discover -s server/tests`：2597 个测试全部通过（3 个 skipped）。
