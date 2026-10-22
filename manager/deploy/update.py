#!/usr/bin/env python3
"""WorkBuddy Manager 一键更新 worker。

由管理端后台调用（脱离父进程运行，因此重启服务不会中断更新流程），
也可单独在命令行执行：

    python3 deploy/update.py --target both
    python3 deploy/update.py --target upstream
    python3 deploy/update.py --target manager

设计要点
--------
1. **自包含**：仅用标准库，避免「更新过程中依赖被替换」导致脚本自身失败。
2. **状态外置**：进度写入 JSON 文件，管理端读取该文件展示实时日志。
   （更新会重启管理端，若用 HTTP 流式返回会被中断）
3. **幂等与安全**：上游更新会保留账号文件与配置；并**强制把端口绑定收敛为
   127.0.0.1**，避免 upstream 仓库里的 `7863:7863` 覆盖我们的安全加固。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── 运行环境（与 server/config.py 保持一致的默认值）──────────
INSTALL_DIR = Path(os.environ.get('WB_INSTALL_DIR') or Path(__file__).resolve().parent.parent)
UPSTREAM_DIR = Path(os.environ.get('WB_UPSTREAM_DIR') or '/opt/workbuddy2api')
UPSTREAM_PORT = int(os.environ.get('WB_UPSTREAM_PORT') or 7863)
MANAGER_PORT = int(os.environ.get('WB_MANAGER_PORT') or 7864)
MANAGER_REPO = os.environ.get('WB_MANAGER_REPO') or 'ithtelab/workbuddy-manager'
UPSTREAM_REPO = os.environ.get('WB_UPSTREAM_REPO') or 'https://github.com/daniei-chen/WorkBuddy-API.git'
SERVICE_NAME = os.environ.get('WB_SERVICE_NAME') or 'workbuddy-web'
DATA_DIR = Path(os.environ.get('WB_DATA_DIR') or INSTALL_DIR / 'data')
STATUS_FILE = Path(os.environ.get('WB_UPDATE_STATUS') or DATA_DIR / 'update-status.json')
# 运行形态：systemd（宿主机安装）或 docker（容器内运行）。
#
# 为什么必须区分「重启方式」：容器里没有 systemd，`systemctl restart` 只会失败。
# 容器自身的重启必须由**外部**（compose 的 restart 策略）完成——容器里无法重启
# 自己，这是 Docker 的模型决定的，不是缺功能。
# 取 auto：能探测到容器就按容器处理，否则按 systemd。
RUN_MODE = (os.environ.get('WB_RUN_MODE') or 'auto').strip().lower()
# 上游版本固定：写入提交号/标签后，上游更新会检出该版本而不是跟随分支。
# 用途：上游某个提交自身有问题（如 Dockerfile 引用了已删除的文件）时，
# 可以固定回上一个可用提交，避免「一更就坏、且没有退路」。
UPSTREAM_REF_FILE = Path(os.environ.get('WB_UPSTREAM_REF_FILE') or DATA_DIR / 'upstream-ref.txt')

STEP_TIMEOUT = int(os.environ.get('WB_UPDATE_STEP_TIMEOUT') or 900)

# ── 发布包签名校验 ───────────────────────────────────────
# 为什么必须有：一键更新会把 Release 产物以 **root** 直接落盘并重启服务。
# 而「能发 Release」的门槛比想象中低——能合并 PR 的协作者、或被钓鱼的维护者
# 账号都能发版。于是「合并恶意 PR → 发版 → 用户点更新」是一条完整链路，
# 一次得手就是**所有部署同时沦陷**（SolarWinds / event-stream 的形态）。
#
# 签名把「能改代码」与「能发布可信产物」变成两件事：私钥离线保管、不进仓库
# 也不进 CI（进了 CI 的话，恶意 PR 可以改 workflow 把密钥偷走，签名就白做了）。
# 攻击者即使拿到合并权限发了版，**签不出名，所有用户的更新会中止**。
#
# 用 OpenSSH 自带的 ssh-keygen（8.0+，服务器上必有），不引入新依赖。
# 公钥**内嵌在代码里**而不是读文件：文件可能被一并替换，那信任锚就没了。
# 对应私钥 ~/.ssh/workbuddy-release 仅存在于维护者本机（绝不进仓库、不进 CI）。
RELEASE_PUBKEY = os.environ.get('WB_RELEASE_PUBKEY') or (
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHEGhxZQjEEK/RbtgcRLuuWji0fVB4E2dVKMnhtLlCkx '
    'workbuddy release signing'
)
# 签名者身份（allowed_signers 的第一列），仅作标识，不参与信任判断
RELEASE_SIGNER_ID = os.environ.get('WB_RELEASE_SIGNER') or 'release'
# 置 1 可跳过验签，供更换密钥等紧急情况使用；会在日志里显式告警
SKIP_SIGNATURE = os.environ.get('WB_SKIP_SIGNATURE') == '1'


# ── 状态写入 ─────────────────────────────────────────────
class Reporter:
    def __init__(self, target: str) -> None:
        self.start = time.time()
        self.state: dict = {
            'running': True,
            'ok': None,
            'target': target,
            'step': '初始化',
            'logs': [],
            'started_at': int(self.start),
            'finished_at': None,
            'duration': 0,
            'pid': os.getpid(),
        }
        self.flush()

    def log(self, message: str, level: str = 'info') -> None:
        line = f'[{time.strftime("%H:%M:%S")}] {message}'
        self.state['logs'].append({'ts': int(time.time()), 'level': level, 'text': message})
        # 终端输出便于命令行单独运行
        print(line, flush=True)
        self.flush()

    def step(self, name: str) -> None:
        self.state['step'] = name
        self.log(f'== {name} ==')
        self.flush()

    def finish(self, ok: bool) -> None:
        self.state['running'] = False
        self.state['ok'] = ok
        self.state['finished_at'] = int(time.time())
        self.state['duration'] = round(time.time() - self.start, 1)
        self.flush()

    def set_target_version(self, tag: str) -> None:
        """记录本次要更新到的版本。

        管理端重启会把本进程一并终止（见 update_manager 的说明），
        管理端读到「运行中但进程已不在」时，用这个版本号核对代码是否已就位，
        从而区分「更新成功、只是被重启带走」与「真的崩了」。
        """
        self.state['target_version'] = str(tag or '').strip().lstrip('vV')
        self.flush()

    def set_signature(self, status: str, detail: str = '') -> None:
        """记录签名校验结果，供界面显示「已验签 / 未验签」。

        status: verified（验签通过）/ skipped（走了绕过开关）/ none（未执行）
        界面据此给出安心的绿色标记或醒目告警 —— 这是「本次更新是否经过
        完整性验证」唯一的用户可见信号，不能只留在日志里。
        """
        self.state['signature'] = {'status': status, 'detail': detail}
        self.flush()

    def flush(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
            tmp.replace(STATUS_FILE)
        except Exception:  # noqa: BLE001
            pass


# ── 命令执行 ─────────────────────────────────────────────
def run(cmd: list[str], cwd: Path | None = None, rep: Reporter | None = None,
        timeout: int = STEP_TIMEOUT, check: bool = True) -> tuple[int, str]:
    if rep:
        rep.log(f'$ {" ".join(cmd)}')
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        if rep:
            rep.log(f'命令不存在: {exc}', 'error')
        if check:
            raise
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        if rep:
            rep.log(f'命令超时（{timeout}s）', 'error')
        if check:
            raise
        return 124, 'timeout'

    out = (proc.stdout or '') + (proc.stderr or '')
    if rep and out.strip():
        for line in out.strip().splitlines()[-30:]:
            rep.log(f'  {line}')
    if proc.returncode != 0 and check:
        raise RuntimeError(f'命令失败（exit {proc.returncode}）: {" ".join(cmd)}')
    return proc.returncode, out


def http_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'workbuddy-manager-updater',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


# ── 下载通道适配（本机部署补丁，2026-09-16）────────────────
# 原因：本机直连 github.com 下载 Release 产物实测极慢/超时（8.9MB 需 224s，
#      294B 签名文件 60s 超时导致 v1.0.35 更新失败）。本机一贯使用 gh-proxy
#      镜像通道（见上游 git remote origin）。
# 范围：仅将「下载 URL 前缀」映射到镜像；验签逻辑、公钥、安装流程均未改动
#      ——镜像即使被投毒，没有签名私钥仍会验签失败、安装被拒。
# 关闭：WB_DISABLE_MIRROR=1 可恢复直连。
MIRROR_PREFIX = os.environ.get('WB_DOWNLOAD_MIRROR') or 'https://gh-proxy.com/'


def mirror_url(url: str) -> str:
    """把 github.com 的下载地址映射到镜像通道。"""
    if os.environ.get('WB_DISABLE_MIRROR') == '1':
        return url
    prefix = 'https://github.com/'
    return MIRROR_PREFIX + url if url.startswith(prefix) else url


def download(url: str, dest: Path, rep: Reporter) -> None:
    rep.log(f'下载 {url}')
    req = urllib.request.Request(url, headers={'User-Agent': 'workbuddy-manager-updater'})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, 'wb') as fh:
        shutil.copyfileobj(resp, fh)
    size = dest.stat().st_size
    rep.log(f'  完成（{size / 1024 / 1024:.2f} MB）')
    if size < 100_000:
        raise RuntimeError('下载内容异常偏小，可能是错误页面')


def check_signature(archive: Path, sig_path: Path, rep: Reporter) -> None:
    """用内置公钥校验 `archive` 的签名文件 `sig_path`；不通过即抛错。

    拆成独立函数是刻意的：「验签」是这条信任链的核心，必须是一个**只依赖
    两个文件 + 内置公钥**的纯函数，便于测试直接调用（不必去 mock 网络层——
    曾经因为 mock 标准库导致测试之间互相污染，那种脆弱性本身也是风险）。
    """
    if SKIP_SIGNATURE:
        rep.log('⚠️ 已通过 WB_SKIP_SIGNATURE 跳过签名校验——'
                '只有在更换签名密钥等紧急情况下才应这样做，本次更新未经验证', 'warn')
        rep.set_signature('skipped', '已手动跳过（WB_SKIP_SIGNATURE=1）')
        return

    if 'AAAA_REPLACE_ME' in RELEASE_PUBKEY:
        raise RuntimeError(
            '发布包签名公钥未配置（仍是占位值），已拒绝自动更新。\n'
            '  这是故意的：没有可信公钥时，任何"从 GitHub 下载的包"都可能是'
            '被篡改的产物。\n'
            '  请把维护者提供的公钥写入 deploy/update.py 的 RELEASE_PUBKEY，'
            '或设 WB_RELEASE_PUBKEY 环境变量；'
            '确需临时跳过可设 WB_SKIP_SIGNATURE=1（不推荐）。'
        )

    if not sig_path.is_file():
        raise RuntimeError(
            '该 Release 没有可用的签名文件，已拒绝安装。\n'
            '  正常发布流程会附带 .tar.gz.sig；缺失说明发布流程可能被改动。'
        )

    # ssh-keygen 要求 allowed_signers 格式（纯 .pub 文件不接受）
    signers = archive.parent / 'allowed_signers'
    signers.write_text(f'{RELEASE_SIGNER_ID} {RELEASE_PUBKEY.strip()}\n', encoding='utf-8')

    rep.log('校验发布包签名…')
    cmd = ['ssh-keygen', '-Y', 'verify',
           '-f', str(signers),
           '-I', RELEASE_SIGNER_ID,
           '-n', 'file',
           '-s', str(sig_path)]
    try:
        with open(archive, 'rb') as fh:
            proc = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=60)
    except FileNotFoundError as exc:
        raise RuntimeError(
            '系统缺少 ssh-keygen，无法校验发布包签名（OpenSSH 8.0+ 自带）。'
            '请安装 openssh-client 后重试。'
        ) from exc
    out = ((proc.stdout or b'') + (proc.stderr or b'')).decode('utf-8', errors='replace').strip()
    if proc.returncode != 0 or 'Good' not in out:
        # 区分「验签不通过」与「工具太老不会验」：两者都拒绝（fail-closed），
        # 但成因不同——旧版 ssh-keygen 会把用法错误也报成失败，若不点明，
        # 使用者会以为包被篡改，白白排查半天。
        if 'invalid option' in out.lower() or 'unknown option' in out.lower():
            raise RuntimeError(
                '❌ 系统 ssh-keygen 过旧，不支持 -Y 验签（需要 OpenSSH 8.0+）。\n'
                f'  ssh-keygen: {out[:300]}\n'
                '  请升级 openssh-client（Debian/Ubuntu：apt install --only-upgrade openssh-client）。'
            )
        raise RuntimeError(
            '❌ 发布包签名校验失败，已拒绝安装（可能是发布流程被篡改或产物被替换）。\n'
            f'  ssh-keygen: {out[:300]}\n'
            '  如果你的部署确实需要跳过，可临时设 WB_SKIP_SIGNATURE=1，'
            '但那等于放弃这道防线，请先确认包来源。'
        )
    rep.log(f'✓ 签名校验通过（{out[:80]}）')
    rep.set_signature('verified', out[:120])


def download_signature(sig_url: str, archive: Path, rep: Reporter) -> Path:
    """下载签名文件到包旁边，返回其路径。失败时抛错（不返回"空签名"）。"""
    sig_path = archive.with_suffix(archive.suffix + '.sig')
    if not sig_url:
        return sig_path  # 让 check_signature 报「缺少签名文件」并给出清晰指引
    rep.log('下载签名文件…')
    try:
        req = urllib.request.Request(sig_url, headers={'User-Agent': 'workbuddy-manager-updater'})
        with urllib.request.urlopen(req, timeout=60) as resp, open(sig_path, 'wb') as fh:
            shutil.copyfileobj(resp, fh)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f'该 Release 没有可用的签名文件，已拒绝安装：{exc}\n'
            '  正常发布流程会附带 .tar.gz.sig；缺失说明发布流程可能被改动。'
        ) from exc
    return sig_path


def verify_release_signature(archive: Path, sig_url: str, rep: Reporter) -> None:
    """校验发布包的签名；不通过就抛错中止（绝不继续安装）。

    这是「包本身可信吗」的唯一防线——路径校验（_safe_extract）只能保证
    「包里的路径不逃逸」，防不了「包整体就是恶意的」。两者缺一不可。
    签名覆盖整个 tar.gz，因此包内容被改一个字节都会验签失败。
    """
    sig_path = download_signature(sig_url, archive, rep)
    check_signature(archive, sig_path, rep)


# ── 上游更新 ─────────────────────────────────────────────
def enforce_local_bind(rep: Reporter) -> None:
    """把 compose 的端口绑定收敛为仅本机。

    upstream 仓库里是 `7863:7863`（公网可达）；我们的安全基线要求
    `127.0.0.1:7863:7863`。每次更新后都要重新施加，否则会悄悄回退。
    """
    compose = UPSTREAM_DIR / 'docker-compose.yml'
    if not compose.is_file():
        rep.log('未找到 docker-compose.yml，跳过端口收敛', 'warn')
        return
    text = compose.read_text(encoding='utf-8')
    wide = f'"{UPSTREAM_PORT}:{UPSTREAM_PORT}"'
    bound = f'"127.0.0.1:{UPSTREAM_PORT}:{UPSTREAM_PORT}"'
    if bound in text:
        rep.log('端口绑定已是仅本机（127.0.0.1）')
        return
    if wide in text:
        compose.write_text(text.replace(wide, bound), encoding='utf-8')
        rep.log(f'已将端口绑定收敛为 {bound}（安全基线）')
    else:
        rep.log('未匹配到端口绑定行，请人工确认 compose 配置', 'warn')


def restore_local_dockerfile(rep: Reporter) -> None:
    """把上游覆盖掉的本地 Dockerfile 定制装回去。

    为什么需要：本机无法直连 proxy.golang.org，Dockerfile 里的
    GOPROXY=goproxy.cn 与清华 apk 源是构建可用的前提。上游仓库没有这些
    定制，而每次更新都会先 `git checkout -- .` 丢弃本地改动——不装回来，
    下一次「更新上游」就会因为拉不到 Go 模块而构建失败（2026-09-16 实际
    发生过一次）。定制来源：仓库内 .local-config/Dockerfile（手工维护）。

    注意：不要给这两个文件设 skip-worktree / assume-unchanged 标记——
    那会让 `git reset --hard` 报 "Entry ... not uptodate" 并阻断更新
    （2026-09-16 的另一处故障根因，已清除）。
    """
    local = UPSTREAM_DIR / '.local-config' / 'Dockerfile'
    work = UPSTREAM_DIR / 'Dockerfile'
    if not local.is_file():
        rep.log('未找到 .local-config/Dockerfile，跳过本地 Dockerfile 定制恢复', 'warn')
        return
    try:
        want = local.read_text(encoding='utf-8')
        cur = work.read_text(encoding='utf-8') if work.is_file() else ''
    except OSError as exc:
        rep.log(f'本地 Dockerfile 定制恢复失败：{exc}', 'warn')
        return
    if cur == want:
        rep.log('Dockerfile 本地定制已是目标状态')
        return
    work.write_text(want, encoding='utf-8')
    rep.log('已恢复 Dockerfile 本地定制（GOPROXY / apk 镜像源）')


def apply_source_patches(rep: Reporter) -> None:
    """重施 Go 源码本地补丁（新会话轮转 / 额度耗尽跳过 等）。

    为什么需要：`git checkout -- .` 与 reset 会丢弃所有本地改动，补丁必须
    在构建前装回去。补丁文件维护在 UPSTREAM_DIR/local-patches/patches/，
    由 apply-patches.sh 逐条应用（幂等：已应用的自动跳过）。

    fail-closed：补丁失败（上游改了同区域代码导致冲突）→ 中止更新并保留
    旧容器继续服务。不做"打不上就算了"的降级——静默少了本地能力比更新
    失败更危险（行为与预期不符且难以发现）。
    """
    script = UPSTREAM_DIR / 'local-patches' / 'apply-patches.sh'
    if not script.is_file():
        rep.log('未找到 local-patches/apply-patches.sh，跳过源码补丁重施', 'warn')
        return
    rc, out = run(['bash', str(script)], rep=rep, check=False)
    if rc != 0:
        rep.log('源码补丁重施失败（上游可能改了同区域代码）', 'error')
        rep.log('  处理指引：对照 UPSTREAM_DIR/local-patches/backup/ 修订补丁，'
                '或临时把冲突补丁移出 patches/ 后重试。', 'error')
        raise RuntimeError('本地源码补丁无法应用，更新已中止（旧容器继续服务）')
    rep.log('本地源码补丁已就绪')


def update_upstream(rep: Reporter) -> None:
    rep.step('更新上游 WorkBuddy API')

    if not (UPSTREAM_DIR / '.git').is_dir():
        rep.log(f'{UPSTREAM_DIR} 不是 git 仓库，跳过上游更新', 'warn')
        return

    which = shutil.which('git')
    if not which:
        raise RuntimeError('未安装 git，无法更新上游')

    # 1) 若存在本地改动，先备份再暂存，避免 pull 冲突
    rc, status = run(['git', 'status', '--porcelain'], cwd=UPSTREAM_DIR, rep=rep, check=False)
    dirty = [l for l in status.splitlines() if l.strip()]
    if dirty:
        rep.log(f'检测到 {len(dirty)} 处本地改动，先备份')
        backup = DATA_DIR / 'upstream-local-changes.patch'
        rc, diff = run(['git', 'diff'], cwd=UPSTREAM_DIR, rep=rep, check=False)
        if diff.strip():
            backup.write_text(diff, encoding='utf-8')
            rep.log(f'  已备份到 {backup}')
        # 本地化模式（2026-09-26）：定制一律以本地为准——有未提交改动时不丢弃，直接中止
        rep.log('检测到未提交的本地改动（已备份 patch）。为保护定制，本次上游更新中止；'
                '请先提交到 local-merged 分支并推送备份仓库后再更新', 'warn')
        return

    # 2) 拉取（或检出被固定的版本）
    pinned = _read_upstream_ref()
    before = ''
    rc, out = run(['git', 'rev-parse', 'HEAD'], cwd=UPSTREAM_DIR, rep=rep, check=False)
    if rc == 0:
        before = out.strip()[:8]

    if pinned:
        rep.log(f'已固定上游版本：{pinned}（不跟随分支）')
        rc, out = run(['git', 'fetch', '--depth', '1', 'origin', pinned],
                      cwd=UPSTREAM_DIR, rep=rep, check=False)
        if rc != 0:
            # 浅克隆有时取不到任意提交，退回完整 fetch 再试
            rep.log('按提交直接拉取失败，尝试完整拉取…', 'warn')
            rc, out = run(['git', 'fetch', 'origin'], cwd=UPSTREAM_DIR, rep=rep, check=False)
        if rc != 0:
            raise RuntimeError(f'拉取上游 {pinned} 失败（提交/标签是否存在？网络是否正常？）')
        rc, out = run(['git', 'reset', '--hard', 'FETCH_HEAD'], cwd=UPSTREAM_DIR, rep=rep, check=False)
        if rc != 0:
            rc, out = run(['git', 'reset', '--hard', pinned], cwd=UPSTREAM_DIR, rep=rep, check=False)
        if rc != 0:
            raise RuntimeError(f'检出上游 {pinned} 失败')
    else:
        rep.log('拉取上游最新代码…')
        rc, out = run(['git', 'fetch', '--depth', '1', 'origin'], cwd=UPSTREAM_DIR, rep=rep, check=False)
        if rc != 0:
            rep.log('git fetch 失败（网络问题？）', 'warn')
        # 本地化模式：远端 main 是单提交快照（含全部定制）；更新 = 本地对齐快照。
        # 安全网：重置前先把当前 HEAD 打成备份分支，任何丢失都能找回。
        rc, out = run(['git', 'rev-parse', 'HEAD'], cwd=UPSTREAM_DIR, rep=rep, check=False)
        if rc == 0:
            run(['git', 'branch', '-f', f'backup/before-upstream-update-{time.strftime("%Y%m%d-%H%M%S")}', out.strip()],
                cwd=UPSTREAM_DIR, rep=rep, check=False)
        rc, out = run(['git', 'fetch', '--depth', '1', 'origin', 'main'], cwd=UPSTREAM_DIR, rep=rep, check=False)
        if rc != 0:
            raise RuntimeError('拉取备份仓库 main 失败（网络或仓库可见性？）')
        rc, out = run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=UPSTREAM_DIR, rep=rep, check=False)
        cur = out.strip() if rc == 0 else ''
        if rc == 0 and cur and cur != 'HEAD':
            run(['git', 'reset', '--hard', 'FETCH_HEAD'], cwd=UPSTREAM_DIR, rep=rep, check=False)
        else:
            run(['git', 'checkout', '-f', '-B', 'main', 'origin/main'], cwd=UPSTREAM_DIR, rep=rep, check=False)

    after = ''
    rc, out = run(['git', 'rev-parse', 'HEAD'], cwd=UPSTREAM_DIR, rep=rep, check=False)
    if rc == 0:
        after = out.strip()[:8]
    if before and after and before == after:
        rep.log(f'上游已是最新（{after}）')
    else:
        rep.log(f'上游代码更新：{before or "?"} → {after or "?"}')

    # 3) 恢复安全基线
    enforce_local_bind(rep)
    restore_local_dockerfile(rep)
    apply_source_patches(rep)

    # 4) 构建前预检：上游偶尔会漏改 Dockerfile（删了文件却仍在 COPY），
    #    提前查出来，避免只看到 docker 那串难懂的报错
    missing = _missing_copy_sources()
    if missing:
        rep.log('构建预检未通过：Dockerfile 引用了不存在的文件', 'error')
        for m in missing:
            rep.log(f'  缺少 {m}', 'error')
        rep.log('这是上游代码本身的问题（不是你配置的问题）。'
                '可在「设置 → 系统更新」把上游固定到上一个可用提交，或等待上游修复。', 'error')
        raise RuntimeError('上游 Dockerfile 引用了不存在的文件：' + ', '.join(missing))

    # 5) 重建并启动
    rep.log('重建并启动上游容器（首次可能需数分钟）…')
    compose_cmd = ['docker', 'compose'] if _has_compose_v2() else ['docker-compose']
    rc, out = run(compose_cmd + ['up', '-d', '--build'], cwd=UPSTREAM_DIR, rep=rep, check=False)
    if rc != 0:
        hint = _diagnose_build_failure(out)
        rep.log(f'重建失败（exit {rc}）', 'error')
        if hint:
            rep.log(f'原因判断：{hint}', 'error')
        # 构建失败时 compose 不会动已在运行的容器，明确说明当前服务状态
        _report_service_state(rep)
        raise RuntimeError('上游重建失败' + (f'：{hint}' if hint else '，请查看上方日志'))

    # 6) 等待就绪
    rep.log('等待上游就绪…')
    if wait_health(f'http://127.0.0.1:{UPSTREAM_PORT}/healthz', 90, rep):
        rep.log('上游已就绪')
    else:
        rep.log('上游未在预期时间内就绪，请查看容器日志', 'warn')

    # 7) 清版本检测缓存
    #
    # 缓存里存的是「更新前」查到的远端最新提交；不清的话，界面会把**已经装好的
    # 这个版本**当成新版本继续提示「上游有更新」，一直到缓存 6 小时过期为止
    # （用户报过这个问题：明明更新到最新了，面板还是一直说有更新）。
    # 管理端更新那条路径早就清了，上游这条一直漏着。
    _clear_version_cache(rep)


def _clear_version_cache(rep: Reporter) -> None:
    """清掉版本检测缓存，使界面立即重新判断「有没有更新」。"""
    try:
        (DATA_DIR / 'version-check.json').unlink(missing_ok=True)
        rep.log('已清除版本检测缓存')
    except Exception:  # noqa: BLE001
        pass


# 安全关键文件：它们承载验签逻辑与信任锚，改了必须人工审查
_TRUST_ANCHOR_FILES = ('update.py', 'release-signing-key.pub')


def _explain_deploy_risk(rep: Reporter, modified: list[str], added: list[str],
                         here: Path) -> None:
    """把 deploy/ 差异翻译成「要不要紧张、下一步做什么」。

    为什么要单独一段说明：早先只说「已跳过同步」，管理员看到一排文件名
    无法判断严重性——实测有用户专门来问「这要不要紧」。差异本身分三类，
    处理方式完全不同：

      1. 只新增了工具脚本（如 check-upstream.sh）→ **无需任何操作**
      2. 修改了非信任锚文件（如 systemd 单元）→ 看一眼即可，想要新功能就覆盖
      3. 修改了信任锚（update.py / 公钥）→ **必须人工比对**，确认是官方改动
         而非被替换，再覆盖；这是整条供应链防护的最后一关
    """
    anchors = [f for f in modified if Path(f).name in _TRUST_ANCHOR_FILES]
    others = [f for f in modified if Path(f).name not in _TRUST_ANCHOR_FILES]

    if anchors:
        rep.log('   ⚠️ 其中包含**验签相关文件**：' + '、'.join(anchors), 'warn')
        rep.log('      这类文件决定「更新包是否可信」，改动必须人工确认后才覆盖，'
                '否则供应链防护可能被绕过。', 'warn')
    elif others:
        rep.log('   这些是普通脚本/配置（不含验签逻辑），看过差异后按需覆盖即可。')
    elif added:
        rep.log('   仅新增文件、未改动现有内容 —— 不影响本次更新，可以不处理。')

    if modified:
        rep.log(f'   比对方法：diff {here.parent}/<文件名> <新包目录>/deploy/<文件名>')
    rep.log(f'   覆盖位置：{here.parent}（本次未改动任何文件）')


def _read_upstream_ref() -> str:
    """要固定的上游版本（提交号/标签）。环境变量优先，其次本地文件；空 = 跟随分支。"""
    env = (os.environ.get('WB_UPSTREAM_REF') or '').strip()
    if env:
        return env
    try:
        return UPSTREAM_REF_FILE.read_text(encoding='utf-8').strip()
    except Exception:  # noqa: BLE001
        return ''


def _missing_copy_sources() -> list[str]:
    """列出 Dockerfile 里 COPY 引用了、但仓库中并不存在的本地文件。

    上游曾出现「删了脚本却漏改 Dockerfile」导致镜像构建失败。这类问题
    docker 的报错（failed to calculate checksum ... not found）不容易读懂，
    这里提前查出来给出明确结论。

    只判定**确凿**的情况：跳过 --from=（阶段拷贝）、URL、含通配符的源，
    避免误报把正常更新挡下来。
    """
    dockerfile = UPSTREAM_DIR / 'Dockerfile'
    if not dockerfile.is_file():
        return []
    missing: list[str] = []
    try:
        lines = dockerfile.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception:  # noqa: BLE001
        return []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != 'COPY':
            continue
        tokens = parts[1:]
        if any(t.startswith('--from=') for t in tokens):
            continue  # 从构建阶段拷贝，不是仓库文件
        # 去掉 --chown= 之类的选项
        srcs = [t for t in tokens[:-1] if not t.startswith('--')]
        for src in srcs:
            if any(ch in src for ch in '*?['):
                continue  # 通配符交给 docker 自己解析
            if src.startswith(('http://', 'https://')):
                continue
            if not (UPSTREAM_DIR / src).exists():
                missing.append(src)
    return missing


def _diagnose_build_failure(out: str) -> str:
    """把 docker 构建失败的长日志归纳成一句人话。"""
    low = (out or '').lower()
    if 'not found' in low and ('checksum' in low or 'copy' in low):
        return ('上游 Dockerfile 引用了仓库里不存在的文件——这是上游代码的问题，'
                '不是你配置的问题。可把它固定到上一个可用提交后重试')
    if 'no space left' in low:
        return '磁盘空间不足，请清理后重试'
    if any(k in low for k in ('dial tcp', 'i/o timeout', 'temporary failure', 'connection refused')):
        return '网络问题（拉取基础镜像或依赖失败），稍后重试'
    if 'permission denied' in low:
        return '权限不足，请确认以 root 或具备 docker 权限的用户执行'
    return ''


def _health_ok(url: str, timeout: int = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _report_service_state(rep: Reporter) -> None:
    """构建失败后说明当前服务是否还活着。

    构建失败时 compose 不会动已在运行的容器，因此旧版本通常仍在提供服务；
    明确告诉用户这一点，避免误以为「更新失败=服务挂了」而做多余操作。
    """
    if _health_ok(f'http://127.0.0.1:{UPSTREAM_PORT}/healthz'):
        rep.log('注意：本次重建失败，但检测到上游仍在响应——旧容器未被影响，服务正常', 'warn')
    else:
        rep.log('警告：上游健康检查未通过，请检查容器状态（docker ps / docker logs）', 'error')


def _has_compose_v2() -> bool:
    try:
        return subprocess.run(['docker', 'compose', 'version'],
                              capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def wait_health(url: str, tries: int, rep: Reporter) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


# ── 管理端更新 ───────────────────────────────────────────
def update_manager(rep: Reporter) -> None:
    rep.step('更新管理端')

    # 1) 取最新 Release
    try:
        rel = http_json(f'https://api.github.com/repos/{MANAGER_REPO}/releases/latest')
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'获取 Release 失败：HTTP {exc.code}') from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f'无法访问 GitHub API：{exc}') from exc

    tag = rel.get('tag_name') or ''
    rep.log(f'最新版本：{tag or "(未知)"}')
    rep.set_target_version(tag)

    current = read_local_version()
    if current and tag and current == tag.lstrip('v'):
        rep.log(f'当前已是最新版本（{tag}）')
        return

    assets = rel.get('assets') or []
    pkg = next((a for a in assets if str(a.get('name', '')).endswith('.tar.gz')), None)
    if not pkg:
        raise RuntimeError('该 Release 未提供 .tar.gz 产物')
    sig = next((a for a in assets
                if str(a.get('name', '')).endswith('.tar.gz.sig')), None)

    # 2) 下载并解压到临时目录
    with tempfile.TemporaryDirectory(prefix='wbm-update-') as tmpdir:
        tmp = Path(tmpdir)
        # 只用文件名部分：pkg['name'] 来自网络响应，虽然 GitHub 不允许名字含
        # 路径分隔符，但"用远端数据拼本地路径"与早前的路径穿越是同一类错误，
        # 这里按同名原则做净化（Path(...).name 会丢掉任何目录成分）
        archive = tmp / Path(str(pkg.get('name') or 'pkg.tar.gz')).name
        download(mirror_url(pkg['browser_download_url']), archive, rep)

        # 验签必须在下解压之前：先确认「这个包是你签的」，再谈包里的内容
        verify_release_signature(
            archive,
            mirror_url(str(sig.get('browser_download_url') or '')) if sig else '',
            rep,
        )

        rep.log('解压…')
        with tarfile.open(archive, 'r:gz') as tf:
            _safe_extract(tf, tmp)
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise RuntimeError('压缩包结构异常（应含单一顶层目录）')
        new_root = roots[0]

        new_server = new_root / 'server'
        new_web_out = new_root / 'web' / 'out'
        if not (new_server / 'main.py').is_file():
            raise RuntimeError('新包缺少 server/main.py，中止更新')
        if not (new_web_out / 'index.html').is_file():
            raise RuntimeError('新包缺少 web/out/index.html，中止更新')

        # 3) 备份当前代码后替换
        ts = time.strftime('%Y%m%d-%H%M%S')
        backup = INSTALL_DIR / f'backup-{ts}'
        backup.mkdir(parents=True, exist_ok=True)
        rep.log(f'备份当前版本到 {backup}')
        shutil.copytree(INSTALL_DIR / 'server', backup / 'server', dirs_exist_ok=True)
        if (INSTALL_DIR / 'web' / 'out').is_dir():
            shutil.copytree(INSTALL_DIR / 'web' / 'out', backup / 'web-out', dirs_exist_ok=True)

        rep.log('替换 server/')
        shutil.rmtree(INSTALL_DIR / 'server', ignore_errors=True)
        shutil.copytree(new_server, INSTALL_DIR / 'server')
        # 清掉旧字节码，避免加载到过期模块
        for pc in (INSTALL_DIR / 'server').rglob('__pycache__'):
            shutil.rmtree(pc, ignore_errors=True)

        rep.log('替换 web/out/')
        target_web = INSTALL_DIR / 'web' / 'out'
        if target_web.is_dir():
            shutil.rmtree(target_web, ignore_errors=True)
        shutil.copytree(new_web_out, target_web)

        # deploy/ 里的脚本**不自动替换**——它含本次更新用的验签逻辑与公钥，
        # 是整条信任链的锚点。若允许包内 deploy/ 覆盖它，攻击者只要在某次
        # 更新里带一个改过的 update.py，之后所有更新就都不验签了
        # （等于一次得手、永久失效）。
        #
        # 因此：包内带了 deploy/ 就在日志里提示差异，由管理员**手动**决定是否
        # 覆盖（例如 systemd 单元确实需要更新时）。正常发版不会改这里。
        #
        # 提示要**分清轻重**：早先无论什么差异都只说「已跳过同步」，于是
        # 「新增了一个无害的检查脚本」与「验签逻辑被改」看起来一模一样，
        # 管理员无从判断该紧张还是该忽略（实测：有用户为此专门来问）。
        # 现在逐文件标注新增/修改，并把安全关键文件单独点出来。
        new_deploy = new_root / 'deploy'
        if new_deploy.is_dir():
            here = Path(__file__).resolve()
            added: list[str] = []
            modified: list[str] = []
            for src in sorted(new_deploy.rglob('*')):
                if not src.is_file():
                    continue
                rel = src.relative_to(new_deploy)
                dst = INSTALL_DIR / 'deploy' / rel
                try:
                    if not dst.is_file():
                        added.append(str(rel))
                    elif dst.read_bytes() != src.read_bytes():
                        modified.append(str(rel))
                except OSError:
                    modified.append(str(rel))
            if added or modified:
                rep.log('⚠️ 新包内的 deploy/ 与本地不同，**已跳过同步**'
                        '（deploy/ 含验签逻辑，是信任锚，不能随包替换）', 'warn')
                if modified:
                    rep.log('   修改（需人工确认）：' + '、'.join(modified[:8])
                            + ('…' if len(modified) > 8 else ''), 'warn')
                if added:
                    rep.log('   新增（本地没有，多半无害）：' + '、'.join(added[:8])
                            + ('…' if len(added) > 8 else ''), 'warn')
                _explain_deploy_risk(rep, modified, added, here)
            else:
                rep.log('deploy/ 与包内一致，无需同步')

        # 版本标记：界面「当前版本」与更新提醒都以它为准，必须一并替换，
        # 否则更新后仍显示旧版本，并一直提示「发现新版本可用」
        new_marker = new_root / '.version'
        if new_marker.is_file():
            shutil.copyfile(new_marker, INSTALL_DIR / '.version')
            rep.log(f'更新版本标记：{new_marker.read_text(encoding="utf-8").strip()}')

        # 文档同步：界面「更新日志」直接读 CHANGELOG.md，若不同步会一直停在旧版内容
        for name in ('CHANGELOG.md', 'README.md', 'README.en.md'):
            src = new_root / name
            if src.is_file():
                shutil.copyfile(src, INSTALL_DIR / name)
                rep.log(f'同步 {name}')

    # 4) 依赖有变化则重装
    req = INSTALL_DIR / 'server' / 'requirements.txt'
    if req.is_file():
        py = sys.executable
        rep.log('检查 / 安装 Python 依赖…')
        run([py, '-m', 'pip', 'install', '-q', '-r', str(req)], rep=rep, check=False)

    rep.log(f'管理端已更新到 {tag}，重启服务以生效')

    # 清掉版本检测缓存：缓存里存的是「更新前」查到的 latest，留着会让界面
    # 拿旧 latest 跟新版本比较，出现「v1.0.5 → v1.0.4」这类把降级当更新的提示，
    # 也会让刚发布的新版本最长 6 小时才被发现。
    _clear_version_cache(rep)

    # 先把终态落盘，再重启：systemd 默认 KillMode=control-group，restart 会连同
    # 本进程一起终止（start_new_session 只脱离终端会话，并未脱离 service 的 cgroup），
    # 若等重启之后再写状态就永远写不到了。容器形态同样会终止本进程（交给编排层拉起）。
    rep.finish(True)
    if in_container():
        # 容器：置退出标记后结束自己，由 compose 的 restart 策略用新代码拉起。
        # 不能只 return——那时更新进程会正常退出，而**管理端主进程仍在跑旧代码**
        # （它是另一个进程），结果是「更新完成」但界面还是旧版。
        _exit_for_restart(rep)
    restart_service(rep)


def in_container() -> bool:
    """是否运行在容器里（Docker/K8s 等）。

    判据用标准做法：`/.dockerenv` 存在，或 cgroup 里出现容器运行时标识。
    这是**探测**而不是配置项，减少用户配错的概率；`WB_RUN_MODE` 可显式覆盖。
    """
    if RUN_MODE in ('docker', 'container'):
        return True
    if RUN_MODE in ('systemd', 'host'):
        return False
    if Path('/.dockerenv').exists():
        return True
    try:
        for line in Path('/proc/1/cgroup').read_text(encoding='utf-8').splitlines():
            if any(k in line for k in ('docker', 'containerd', 'kubepods', 'podman')):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def restart_service(rep: Reporter) -> None:
    """重启使新代码生效。

    两种运行形态的重启方式**本质不同**，必须分开处理：

      * systemd（宿主机安装）：`systemctl restart`。注意该操作会连同本更新进程
        一起终止（systemd 默认 KillMode=control-group），所以**必须先写好终态**
        （调用方已 rep.finish(True) 在前）。
      * 容器：**无法自我重启**——容器里没有 systemd，也不能重启自己所在的容器
        （除非挂载了 docker.sock，而那等于把宿主 root 权限交给容器，是更糟的
        选择）。容器由 compose 的 `restart: unless-stopped` 策略在进程退出后
        自动拉起，因此这里的做法是**主动结束进程**，让编排层把新代码加载起来。

    所以容器形态下这里不是"失败"，而是"交给外部"——日志要写清楚，否则用户会
    以为更新没生效。
    """
    if in_container():
        rep.log('检测到容器环境：新代码已就位，正在退出进程以便容器重启策略拉起…')
        rep.log('（容器无法自我重启；compose 的 restart 策略会在进程退出后')
        rep.log('  用新代码重新启动。若长时间未恢复，请在宿主机执行：')
        rep.log('  docker compose restart workbuddy-manager）')
        return
    rc, _ = run(['systemctl', 'restart', SERVICE_NAME], rep=rep, check=False)
    if rc != 0:
        raise RuntimeError(f'重启服务失败（systemctl 返回 {rc}），请手动执行 systemctl status {SERVICE_NAME}')
    rep.log('服务已重启')


def _exit_for_restart(rep: Reporter) -> None:
    """容器形态下：结束**整个容器**使新代码生效。

    为什么不是「结束更新进程自己就完事」：更新进程是管理端拉起的子进程，它退出
    后管理端主进程仍在跑**旧代码**（新文件已落盘，但 Python 已把旧模块载入内存）。
    容器里没有办法从内部重启 PID 1 之外的主进程，所以正确做法是结束整个容器，
    由 compose 的 `restart: unless-stopped` 用新代码重新拉起。

    实现：给 PID 1 发 SIGTERM（容器里 PID 1 就是主进程，uvicorn）。
    compose 收到容器退出后会按 restart 策略重启它。
    """
    try:
        os.kill(1, signal.SIGTERM)
        rep.log('已向主进程发送终止信号，容器即将在新代码下重启')
    except Exception as exc:  # noqa: BLE001
        rep.log(f'发送终止信号失败（{exc}）—— 请手动重启容器：'
                'docker compose restart workbuddy-manager', 'warn')
        return
    # 给接收方一点时间优雅退出；随后本进程也退出（容器本该随之结束）
    time.sleep(2)


def read_local_version() -> str:
    """读取当前部署版本（优先取部署时留下的标记，否则读代码里的版本号）。"""
    marker = INSTALL_DIR / '.version'
    if marker.is_file():
        return marker.read_text(encoding='utf-8').strip().lstrip('v')
    main_py = INSTALL_DIR / 'server' / 'main.py'
    if main_py.is_file():
        for line in main_py.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('version='):
                return line.split('=', 1)[1].strip().strip("'\"")
    return ''


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """解压发布包，严格限制在 dest 之内。

    安全（曾有两个可复现的逃逸，属供应链风险）：更新器以 root 运行、包来自
    Release，一旦有人能控制该 Release，包里的路径逃逸就等于 **root 任意文件写入**
    ——实测可覆盖 /root/.ssh/authorized_keys 直接拿到服务器 SSH 登录。
    原实现有两个绕过：

      1. `str(target).startswith(str(base))` 是**字符串前缀**匹配：
         `/tmp/wbm-update-abc` 与 `/tmp/wbm-update-abc-sibling` 是不同目录，
         但前缀相同 → 成员名 `../wbm-update-abc-sibling/x` 可逃逸。
         （本机实测确实写出了 dest 之外的文件。）
      2. **符号链接成员未校验**：检查阶段链接还没创建，`resolve()` 看不穿它；
         解压时先建 `link -> /任意/目录`，再写 `link/x` 就落到了外面。
         另外 Python 3.12 的 `extractall` 默认 `filter=None`（等价 fully_trusted），
         还会按包内 mode 执行 chmod（含 setuid 位）。

    修法（三条同时做，互为纵深）：
      - 拒绝一切链接类成员（symlink/hardlink）——正常发布包不需要它们
      - 用 `Path.is_relative_to` 取代字符串前缀比较
      - 优先用 `filter='data'`（Python 3.12+）由标准库再兜一层；
        老版本没有该参数，则退化为已做的逐成员校验
    """
    base = dest.resolve()
    for member in tf.getmembers():
        # 1) 链接成员一律拒绝：它们是逃逸的载体，发布包没有理由包含
        if member.issym() or member.islnk():
            raise RuntimeError(f'压缩包含链接成员，已拒绝：{member.name}')
        name = member.name
        # 2) 绝对路径与 .. 直接拒绝（比"算出来再比较"更早、更明确）
        if name.startswith('/') or name.startswith('\\') or '..' in Path(name).parts:
            raise RuntimeError(f'压缩包含非法路径：{name}')
        target = (base / name).resolve()
        # 3) 包含性判断用 is_relative_to，不用字符串前缀
        if target != base and not target.is_relative_to(base):
            raise RuntimeError(f'压缩包含非法路径：{name}')

    try:
        tf.extractall(dest, filter='data')  # Python 3.12+：再由标准库校验一遍
    except TypeError:
        # 老版本 Python 无 filter 参数；上面的逐成员校验已是等价防护
        tf.extractall(dest)
    # 兜底：清掉可能被带进来的特殊权限位（filter='data' 已处理，这里防老版本）
    for root, _dirs, files in os.walk(dest):
        for fn in files:
            p = Path(root) / fn
            try:
                mode = p.stat().st_mode
                if mode & (stat.S_ISUID | stat.S_ISGID):
                    os.chmod(p, stat.S_IMODE(mode) & ~(stat.S_ISUID | stat.S_ISGID))
            except OSError:
                pass


# ── 入口 ─────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description='WorkBuddy Manager 更新')
    parser.add_argument('--target', choices=['manager', 'upstream', 'both'], default='both')
    args = parser.parse_args()

    rep = Reporter(args.target)
    rep.log(f'开始更新（target={args.target}，安装目录={INSTALL_DIR}）')

    ok = True
    try:
        if args.target in ('upstream', 'both'):
            update_upstream(rep)
        if args.target in ('manager', 'both'):
            update_manager(rep)
    except Exception as exc:  # noqa: BLE001
        rep.log(f'更新失败：{exc}', 'error')
        ok = False

    rep.log('更新完成' if ok else '更新未完成，请检查上方日志')
    rep.finish(ok)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
