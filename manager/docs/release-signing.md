# 发布包签名：发版流程与密钥管理

一键更新（`deploy/update.py`）以 **root** 把 Release 产物直接落盘并重启服务。
而「能发 Release」的门槛比想象中低——**能合并 PR 的协作者、或被钓鱼的维护者
账号都能发版**。于是「合并恶意 PR → 发版 → 用户点更新」是一条完整链路，
且一次得手就是**所有部署同时沦陷**（SolarWinds / event-stream 的形态）。

签名把「能改代码」与「能发布可信产物」变成两件事：

```
下载包 + .sig  →  用内置公钥验签  →  验不过 → 中止（不落盘、不替换、不重启）
                                    →  验过  → 解压 → 替换 → 重启
```

攻击者即使拿到合并权限并发布了恶意 Release，**他签不出名**，所有用户的更新
都会中止并报「签名校验失败」。防线就这么简单，也因此必须严格。

---

## 一、密钥配置（已完成，以下为存档与轮换参考）

当前使用的签名公钥（已内嵌进 `deploy/update.py` 并写入
`deploy/release-signing-key.pub`）：

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHEGhxZQjEEK/RbtgcRLuuWji0fVB4E2dVKMnhtLlCkx workbuddy release signing
指纹：SHA256:xmHLJDKH/vYtAp59XwXPVE4A/CwXAOpxTlYh7KC677Y
```

**每次签名前用指纹核对一次**（确认你没拿错密钥）：

```bash
ssh-keygen -lf ~/.ssh/workbuddy-release.pub
# 应输出上面的 SHA256 指纹
```

私钥位于 `~/.ssh/workbuddy-release`，**只在本机**（不进仓库、不进 CI）。

### 轮换密钥时（或首次在新机器上配置）

```bash
# 1) 生成新密钥
ssh-keygen -t ed25519 -f ~/.ssh/workbuddy-release -C "workbuddy release signing"

# 2) 公钥要更新**两处**（内容必须一致，有测试盯着这件事）
#    - deploy/update.py → RELEASE_PUBKEY 常量（更新器实际使用的信任锚）
#    - deploy/release-signing-key.pub    （CI 与人工核对时读的）
cat ~/.ssh/workbuddy-release.pub

# 3) 提交后，通知用户手动更新一次（他们手上的旧公钥验不过新签名）
```

> 给私钥加/改口令（**不改变公钥**，因此不需要动仓库、不需要重新签名）：
> ```bash
> ssh-keygen -p -f ~/.ssh/workbuddy-release
> ```
> 当前密钥未设口令。加了之后每次签名会提示输入口令，也可以配合
> ssh-agent 缓存（`ssh-add ~/.ssh/workbuddy-release`）。这是本机的安全
> 权衡：不设口令则「偷到文件即可签名」，设了则多一道。

> **首次启用有鸡生蛋问题**：已经部署的实例还没有验签能力，需要**手动更新
> 一次**到带验签的版本（或用 `WB_SKIP_SIGNATURE=1` 走一次）。从那之后的
> 自动更新才受保护。已验证 v1.0.23 的更新器会同步 `deploy/`，所以那次
> 手动更新后验签逻辑确实会就位。

---

## 二、每次发版（四步）

```bash
# 1) 推送 tag，让 CI 构建并创建**草稿** Release（CI 不签名、也不发布）
git tag v1.0.24 && git push origin v1.0.24

# 2) 等 CI 跑完后，在本机下载**最终产物**并签名
gh release download v1.0.24 --pattern '*.tar.gz'
ssh-keygen -Y sign -f ~/.ssh/workbuddy-release -n file workbuddy-manager-v1.0.24.tar.gz
#    → 生成 workbuddy-manager-v1.0.24.tar.gz.sig

# 3) 上传签名
gh release upload v1.0.24 workbuddy-manager-v1.0.24.tar.gz.sig

# 4) 重新下载核对（sha256 + 验签 + 解开看版本号）之后，发布草稿
gh release edit v1.0.24 --draft=false
```

**顺序很重要**：必须先有 CI 产出的 tar.gz，再对它签名。若签名后才重新打包，
字节流一变签名就失效，用户侧会全部拒绝安装。

**草稿与发布**：CI 建的 Release 是草稿，`releases/latest` 会跳过它 —— 于是
「构建完、还没签名」的这段时间用户看不到这个版本，不会出现「点了更新却被
拒绝：没有可用的签名文件」（issue #127/#129）。签名核对无误后再执行上面第 4
步；忘了发布，用户就永远看不到这个版本。

工作流已针对这点加了保护：**一旦 Release 上出现 `.sig`，就不再覆盖 tar.gz**。

---

## 三、验证签名（可选，但建议）

```bash
# 手动安装前先验，通过再解压
bash deploy/verify-release.sh workbuddy-manager-v1.0.24.tar.gz
```

一键更新无需手工操作——它内置同样的校验（`deploy/update.py` 的
`check_signature`），失败即中止。

---

## 四、为什么私钥不能进 CI

直觉上「放 GitHub Secrets 让 CI 自动签名」更方便，但这会让整条防线失效：

- 能合并 PR 的攻击者可以**改 workflow 的任意步骤**，加一行把密钥打印/外传；
- 密钥一旦进了 CI，攻击面就从「你的笔记本」扩大到「GitHub Actions 的
  每一个环节 + 所有能触发/修改 workflow 的人」；
- 而签名的全部价值来自「私钥只有你知道」这一条。

所以设计是：**CI 只构建，签名只在本机**。

---

## 五、紧急情况

| 场景 | 做法 |
|---|---|
| 怀疑私钥泄露 | 立刻生成新密钥 → 更新 `RELEASE_PUBKEY` 与 `.pub` → 提交 → 通知用户手动更新一次 |
| 换密钥过渡期 | 用户侧可临时设 `WB_SKIP_SIGNATURE=1` 更新，但日志会**显式告警** |
| 已发出错误产物 | 删除整个 Release（含 `.sig`）→ 重跑 CI → 重新签名上传 |
| 某个旧版本需要回退 | 同样要对回退包的 tar.gz 有有效签名，否则会被拒绝 |

跳过开关 `WB_SKIP_SIGNATURE=1` 会在日志中留下 `[warn]` 级记录——它是逃生门，
不是日常选项。

---

## 六、诚实的边界

- **挡不住「你本人被钓鱼」**：攻击者若拿到你的私钥（连同口令），仍能签出
  有效包。但它把门槛从「合并一个 PR」抬高到「偷到你离线保管的密钥」，
  难度差一个量级。
- **签名只覆盖来源与完整性**，不覆盖「代码本身是否有漏洞」——那是代码审计的事。
- **`deploy/` 默认随包同步**（`WB_SYNC_DEPLOY=0` 可关掉）。为什么可以这么做：
  验签发生在**解压之前**，用的是**本地**这一份公钥 —— 只有验签通过了才会走到
  「替换文件」那一步，所以从**已验签的包**里覆盖 `deploy/`，与覆盖 `server/` 是
  同一性质，不会让信任链降级。
  反过来不同步的代价很实在：**更新器本身就在 `deploy/` 里**，不换它意味着以后
  每次都还用旧逻辑，它修过的问题永远到不了用户手上（#28 修过的 compose 探测就是
  这样丢的，#55 又报了同一个毛病）。
  手工维护 `deploy/`（例如自己改过 systemd 单元）的部署：设 `WB_SYNC_DEPLOY=0`
  即保持原样；那时更新日志会列出包内差异并说明代价，由人工比对后决定。
- **新更新器从下一次生效**：若某次更新同步了 `update.py` 自己，本次仍跑在旧代码上
  （日志里有这句说明）。
