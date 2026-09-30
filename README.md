<p align="center">
  <img src="https://raw.githubusercontent.com/DGZSbot/ai-icon/refs/heads/main/WorkBuddy.png" alt="WorkBuddy API" width="120">
</p>

<h1 align="center">WorkBuddy API</h1>

<p align="center">
  <b>把 CodeBuddy 账号变成 OpenAI 兼容 API 的多账号网关</b><br>
  OAuth 登录 · 账号池轮转 · 会话粘性 · 额度治理 · 成本择优
</p>

<p align="center">
  <img alt="Go" src="https://img.shields.io/badge/Go-1.26-00ADD8?logo=go&logoColor=white&style=flat-square">
  <img alt="API" src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square">
  <img alt="Deploy" src="https://img.shields.io/badge/Deploy-Docker_Compose-2496ED?logo=docker&logoColor=white&style=flat-square">
  <img alt="Transport" src="https://img.shields.io/badge/Transport-SSE_Streaming-0DBD8B?style=flat-square">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green?style=flat-square">
</p>

---

## 项目简介

自托管的 **OpenAI 兼容上游网关**：把 CodeBuddy 账号包装为统一的 `/v1/chat/completions` 服务。

- 通过 **OAuth 设备授权**（`login.sh`）获取账号凭证，网关侧完成 token 自动刷新、账号池调度与流量治理；
- 面向 **个人多账号** 场景：多账号共享、单号故障自动换号、冷却 / 熔断防雪崩、会话粘性保证多轮上下文不跳号；
- 对客户端只暴露 OpenAI 兼容接口，现有 SDK / 前端 / 工具 **零改造接入**。

> ⚠️ **合规须知**：本项目为**非官方**网关，仅限本人授权账号、本机 / 私有环境使用。

## 核心能力

### 🔐 账号池治理

- **OAuth 设备授权登录** — `login.sh` 一条命令：授权 URL → 浏览器登录 → token 轮询 → 凭证落盘 → 热加载生效，无需重启
- **三因子加权选号** — `credits 占比 ×10 + 快过期积分 ×8 + 闲置补偿` 加权取 Top-5 短名单再抽签，积分多、快过期、闲置久的号优先；防惊群窗口避免多账号待命时打爆同一台
- **在途租约** — 单账号最大在途请求数（`pool.max_in_flight`）分域分档控制，占满的号不参与选号
- **账本择优** — 按 `usage.credit` 折算 (账号, 模型) 单价账本，免费 / 便宜的号优先；EMA 平滑、随 state.json 落盘，重启不丢
- **成本分层探索** — 免费层垄断时自动向未知号搭车改道探索，成功毕业 / 失败走既有冷却，零额外上游请求
- **额度耗尽跳过** — 以签到余额的权威观测为准（`pool.skip_exhausted`），余额耗尽的号自动退出选号 / 粘性分配 / 探活，恢复后自动回归

### 🚦 流量治理

- **冷却 / 熔断 / 连败降权** 状态机，单号故障不扩散
- **会话粘性** — 带会话键的请求粘住同一账号，多轮上下文与提示词缓存不跳号
- **WAF 观测** — 403 拦截页 / 空体的 WAF 命中单独归类与记录，不与业务错误混算
- **账号热加载** — `auths/` 目录监听，新增账号落盘即生效

### 🛡️ 传输与稳定

- **出站传输加固** — 真正禁用 h2、TCP keepalive 15s 探测（NAT 黑洞快速识别）、TLS 握手上限 10s、空闲连接 15s 回收、响应头 60s 硬顶
- **流中空闲监控** — SSE 静默超阈值才断流，活跃输出自动续命
- **失败自愈** — 传输层失败即清空闲连接池 + 轮转换号重试

### 📊 观测与统计

- `GET /v1/stats` — 请求 / token / 扣费聚合，支持按模型透出上游积分倍率
- `GET /status` — 池状态、冷却与限流台账、模型成本台账（`model_costs`）、成本探索台账（`cost_explore`）
- `cmd/stats` — 终端用量表格，按模型聚合、支持 `-sort credits`

### 🌐 双域与多模型

- **cn / global 双域**：按账号与模型名前缀路由到不同账号池，`global:` 前缀即走国际版
- **40+ 模型目录**：deepseek / glm / kimi / minimax / hunyuan 等，`reasoning_effort` 思考档位（low / high / max）按模型能力自动降级对齐
- **双传输模式**：exec / shell 兼容直连与跳板机场景

### 🧰 工具集

| 命令 | 用途 |
|---|---|
| `login.sh` / `cmd/login` | OAuth 设备授权添加账号 |
| `cmd/signin` | 每日签到与余额刷新 |
| `cmd/credit` | 积分查询 |
| `cmd/trial` / `cmd/activity` | 体验与活跃任务 |
| `cmd/acct` | 账号批量管理 |
| `cmd/stats` | 用量统计（终端表格） |

## 快速开始

```bash
git clone https://github.com/daniei-chen/WorkBuddy-API.git
cd WorkBuddy-API
cp config.example.json config.json   # 按需修改
docker compose build && docker compose up -d
bash login.sh                        # 添加账号
curl http://127.0.0.1:7863/v1/models
```

默认监听 `127.0.0.1:7863`，Bearer 鉴权（`config.json` 的 `api_key`）。镜像构建内置国内加速（goproxy.cn / 清华源）。

## API 一览

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容对话（流式 / 非流式） |
| `GET /v1/models` | 模型列表（含 reasoning 档位与上下文能力） |
| `GET /v1/stats` | 用量统计（支持按模型透出积分倍率） |
| `GET /status` | 池状态：账号 / 冷却 / 成本台账 / 探索台账 |
| `POST /admin/accounts/{uid}/…` | 账号启用 / 停用 / 复活（需鉴权） |
| `GET /healthz` | 健康检查 |

## 管理控制台（Manager）

[`manager/`](manager/) 内置 Web 管理控制台：API 密钥签发与配额、调用记录、用量 / 命中率统计、账号池与额度观测、在线更新。FastAPI 后端 + Next.js 前端，前端已在 `manager/web/out` 预构建，**无需 Node 环境**。

```bash
git clone https://github.com/daniei-chen/WorkBuddy-API.git
cd WorkBuddy-API/manager
sudo bash deploy/install.sh   # 一键部署：装依赖、注册 systemd 服务并启动（网关自动从本仓库拉取）
```

- 安装后浏览器打开 `http://127.0.0.1:7864`；管理员密码用 `WB_ADMIN_PASSWORD` 设置，留空则首次启动随机生成并打印到服务日志
- 环境变量全集见 [`manager/.env.example`](manager/.env.example)；数据目录 `manager/data/` 首次启动自动建库，不入版本库
- 已自备网关时加 `--skip-upstream` 参数
- `local-patches/` 为幂等增强补丁（用量接口、缓存统计、品牌文案等），服务启动时自动应用

## 配置

全部键位与默认值见 [`config.example.json`](config.example.json)，常用项：

| 键 | 说明 |
|---|---|
| `api_key` | 客户端 Bearer 鉴权密钥（空 = 不鉴权） |
| `pool.max_in_flight` | 单账号最大在途请求数（分域分档） |
| `pool.skip_exhausted` | 额度耗尽自动跳过（默认开） |
| `session_sticky` | 会话粘性 / 轮换策略 |
| `global.enabled` | 国际版账号池开关 |
| `upstream.header_timeout_seconds` | 响应头硬顶 |
| `upstream.attempt_timeout_seconds` | 聊天单次尝试看门狗（默认 60，防上游晾连接吃满外层预算） |

## 安全与合规

- 端口默认绑定 `127.0.0.1`，仅建议内网 / 反代后使用
- 凭证（`auths/`）、状态（`data/`）与配置不入版本库
- 本项目是**非官方**工具，请仅使用**本人授权**的账号，遵守上游服务条款；使用者自行承担使用责任

## License

[MIT](LICENSE)
