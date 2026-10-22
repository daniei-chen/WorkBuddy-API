"""容器部署形态的能力边界（issue #7：Docker 部署方案）。

容器部署不是"把宿主安装塞进镜像"就完事——有几处**本质差异**，若处理不当
会出现「界面说成功、实际没生效」这种最难排查的故障：

  1. **重启方式不同**。宿主用 `systemctl restart`；容器里没有 systemd，
     而且容器**无法重启自己**（除非挂 /var/run/docker.sock，那等于把宿主 root
     权限交给容器内进程——可挂载宿主根目录，比"少一个功能"危险得多，故刻意不做）。
     容器形态的正确做法是：替换代码 → 结束容器 → 由 compose 的 restart 策略
     用新代码拉起。

  2. **不支持更新上游**。重建上游容器需要 docker CLI。所以容器形态下该选项
     必须在**界面层就禁用并说明**，而不是让用户点了跑到一半才失败。

  3. **更新进程退出 ≠ 管理端重启**。更新进程是管理端拉起的子进程；它退出后
     管理端主进程仍在跑旧代码。容器形态必须结束整个容器，否则界面还是旧版。

本文件锁住这些判定，避免"改着改着把容器路径改成宿主假设"。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


class _Rep:
    """轻量 Reporter：只收集日志，不写状态文件。

    真正更新流程里用 deploy/update.py 的 Reporter（会往 DATA_DIR 落盘）；
    这些测试只关心「过程里记了什么、文件最终是什么样」，用这个更干净。
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.state: dict = {}

    def log(self, msg: str, level: str = 'info') -> None:
        self.lines.append((level, msg))

    def step(self, name: str) -> None:
        self.lines.append(('info', f'== {name} =='))

    def set_target_version(self, tag: str) -> None:
        self.state['target_version'] = tag

    def set_signature(self, status: str, detail: str = '') -> None:
        self.state['signature'] = {'status': status, 'detail': detail}

    def finish(self, ok: bool) -> None:
        self.state['ok'] = ok

    def text(self) -> str:
        return '\n'.join(m for _, m in self.lines)

    def errors(self) -> list[str]:
        return [m for lvl, m in self.lines if lvl == 'error']


def _load_update_mod(**env):
    keys = ['WB_RUN_MODE', *env.keys()]
    old = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update({k: str(v) for k, v in env.items()})
        spec = importlib.util.spec_from_file_location('upd_docker',
                                                      str(_ROOT / 'deploy' / 'update.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


class ContainerDetectionTest(unittest.TestCase):
    """运行形态判定：显式配置优先，否则探测。"""

    def test_explicit_docker(self) -> None:
        self.assertTrue(_load_update_mod(WB_RUN_MODE='docker').in_container())

    def test_explicit_systemd(self) -> None:
        self.assertFalse(_load_update_mod(WB_RUN_MODE='systemd').in_container())

    def test_auto_detects_via_dockerenv(self) -> None:
        mod = _load_update_mod(WB_RUN_MODE='auto')
        with mock.patch.object(mod.Path, 'exists', return_value=True):
            self.assertTrue(mod.in_container(), '/.dockerenv 存在时应判定为容器')

    def test_auto_detects_via_cgroup(self) -> None:
        mod = _load_update_mod(WB_RUN_MODE='auto')
        with mock.patch.object(mod.Path, 'exists', return_value=False), \
                mock.patch.object(mod.Path, 'read_text',
                                  return_value='0::/docker/abc123\n'):
            self.assertTrue(mod.in_container(), 'cgroup 含 docker 时应判定为容器')

    def test_auto_plain_host(self) -> None:
        mod = _load_update_mod(WB_RUN_MODE='auto')
        with mock.patch.object(mod.Path, 'exists', return_value=False), \
                mock.patch.object(mod.Path, 'read_text', return_value='0::/\n'):
            self.assertFalse(mod.in_container())


class RestartBehaviorTest(unittest.TestCase):
    """重启方式：宿主走 systemctl，容器交给编排层。"""

    def test_container_restart_does_not_call_systemctl(self) -> None:
        """容器里**绝不能**调用 systemctl —— 那必然失败。"""
        mod = _load_update_mod(WB_RUN_MODE='docker')
        calls: list[list[str]] = []

        class Rep:
            def __init__(self): self.lines = []
            def log(self, m, level='info'): self.lines.append((level, m))

        with mock.patch.object(mod, 'run', side_effect=lambda cmd, **k: (calls.append(cmd), (0, ''))[1]):
            mod.restart_service(Rep())
        self.assertEqual(calls, [], f'容器形态不应执行任何命令，实际：{calls}')

    def test_host_restart_calls_systemctl(self) -> None:
        mod = _load_update_mod(WB_RUN_MODE='systemd')
        calls: list[list[str]] = []

        class Rep:
            def __init__(self): self.lines = []
            def log(self, m, level='info'): self.lines.append((level, m))

        with mock.patch.object(mod, 'run', side_effect=lambda cmd, **k: (calls.append(cmd), (0, ''))[1]):
            mod.restart_service(Rep())
        self.assertTrue(any('systemctl' in c for c in calls),
                        f'宿主形态应调用 systemctl，实际：{calls}')

    def test_exit_for_restart_signals_pid1(self) -> None:
        """容器形态：向 PID 1 发 SIGTERM（让编排层用新代码拉起整个容器）。

        只结束更新进程是不够的——管理端主进程仍在跑旧代码，界面还是旧版。
        """
        import signal as _signal
        mod = _load_update_mod(WB_RUN_MODE='docker')
        killed: list[tuple[int, int]] = []

        class Rep:
            def __init__(self): self.lines = []
            def log(self, m, level='info'): self.lines.append((level, m))

        with mock.patch.object(mod.os, 'kill',
                               side_effect=lambda pid, sig: killed.append((pid, sig))), \
                mock.patch.object(mod.time, 'sleep'):
            mod._exit_for_restart(Rep())
        self.assertEqual(killed, [(1, _signal.SIGTERM)],
                         f'应向 PID 1 发 SIGTERM，实际：{killed}')


class UpstreamUpdateGuardTest(unittest.TestCase):
    """「能否更新上游」按**实际能力**判定，不按是否容器。

    这是本项目的一处**判断修正**，值得写下来：
      初版按「在容器里就不允许更新上游」实现，理由写成"挂 docker.sock 等于把
      宿主 root 交给容器，比少一个功能危险得多"。这个理由**不成立**——宿主
      部署时本服务本来就是 root 运行（systemd 单元无 User=、安装脚本要求 root），
      而 root 进程本来就能 `docker run -v /:/host` 拿到宿主文件系统。也就是说
      宿主部署的权限**已经等价于**挂 docker.sock。

    结论：按能力判定才正确 —— 容器挂了 socket 就能（与宿主部署对齐），宿主没装
    docker 反而不能。按「是否容器」判会把可用场景误判为不可用。
    """

    def _start(self, target: str, docker_ok: bool):
        from server.services import updater
        with mock.patch.object(updater, 'can_control_docker', return_value=docker_ok),                 mock.patch.object(updater, '_lock_active', return_value=True):
            return updater.start_update(target)

    def test_no_docker_rejects_upstream(self) -> None:
        ok, msg = self._start('upstream', False)
        self.assertFalse(ok, '没有 docker 能力时不应允许更新上游')
        self.assertIn('docker', msg, '应说明原因并给出替代做法')
        self.assertIn('docker compose', msg)

    def test_no_docker_rejects_both(self) -> None:
        ok, msg = self._start('both', False)
        self.assertFalse(ok)
        self.assertIn('docker compose', msg)

    def test_docker_available_allows_upstream(self) -> None:
        """有 docker 能力时（含挂了 socket 的容器）应放行到下一步。"""
        ok, msg = self._start('upstream', True)
        # 走到「已有更新任务」说明前置校验放行了
        self.assertFalse(ok)
        self.assertIn('已有更新任务', msg, '有 docker 能力却被前置校验挡住了')

    def test_manager_always_allowed(self) -> None:
        """仅更新管理端不需要 docker —— 任何形态都应放行。"""
        ok, msg = self._start('manager', False)
        self.assertFalse(ok)
        self.assertIn('已有更新任务', msg)

    def test_status_reports_capability(self) -> None:
        """能力标志要透出给界面（界面据此禁用/提示）。"""
        from server.services import updater
        for docker_ok in (False, True):
            with mock.patch.object(updater, 'can_control_docker', return_value=docker_ok):
                st = updater.read_status()
            self.assertEqual(st['can_update_upstream'], docker_ok)

    def test_capability_not_derived_from_container(self) -> None:
        """关键断言：能力**不看**是否容器。

        容器挂了 socket 就能更新上游；按容器判定会把这种（我们推荐的默认
        配置）误判为不可用。
        """
        from server.services import updater
        with mock.patch.object(updater, 'in_container', return_value=True),                 mock.patch.object(updater, 'can_control_docker', return_value=True):
            self.assertTrue(updater.read_status()['can_update_upstream'],
                            '容器 + 有 docker 能力时应可用 —— 不要按容器判定')
        with mock.patch.object(updater, 'in_container', return_value=False),                 mock.patch.object(updater, 'can_control_docker', return_value=False):
            self.assertFalse(updater.read_status()['can_update_upstream'],
                             '宿主但没装 docker 时应不可用')

    def test_frontend_uses_capability_flag(self) -> None:
        src = (_ROOT / 'web' / 'components' / 'common' / 'settings'
               / 'UpdatePanel.tsx').read_text(encoding='utf-8')
        self.assertIn('can_update_upstream', src,
                      '更新面板没读能力标志 —— 用户会点到不支持的操作')


class ComposeCommandTest(unittest.TestCase):
    """issue #28-1：容器内必须真的能调起 compose。

    报障现象：一键更新上游时 `[Errno 2] No such file or directory: 'docker-compose'`
    （exit 127），更新做到一半失败。

    根因是**两条独立的错**叠在一起：
      1. 镜像里的 docker CLI 来自官方静态包，而那个包**不含 compose 插件**
         （实测 `tar -tzf docker-27.3.1.tgz` 只有 docker/dockerd/ctr/containerd*）；
      2. update.py 的判据是「docker compose 不可用就退回 docker-compose」，
         但容器里两个都没有 —— 于是必然走进退回分支、必然报错。
    """

    def setUp(self) -> None:
        self.mod = _load_update_mod()

    def test_dockerfile_installs_compose_plugin(self) -> None:
        """镜像必须自己装 compose 插件（静态包里没有）。"""
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('cli-plugins', df,
                      'Dockerfile 没装 compose 插件 —— 容器内一键更新上游必然 exit 127')
        self.assertIn('docker compose version', df,
                      '装完没有自检 —— 装错了要到运行时才暴露')
        # 文件名必须是连字符形式，`docker compose` 子命令才认
        self.assertIn('cli-plugins/docker-compose', df,
                      '插件文件名不对（必须是 docker-compose），docker 不会把它当子命令')

    def test_compose_plugin_arch_mapping(self) -> None:
        """compose 的架构名与 Docker 的不完全一样（arm 是 armv7 而非 armhf）。"""
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        block = df[df.find('COMPOSE_VERSION'):]
        block = block[:block.find('docker compose version')]
        self.assertIn('armv7', block, 'armv7l 应映射到 armv7（compose 的命名）')
        self.assertIn('aarch64', block, 'arm64 应映射到 aarch64')

    def test_compose_version_stays_on_v2(self) -> None:
        """compose 必须停在 v2 —— 升到 v5 会让 `up --build` 重新坏掉。

        发版前自审实测（读 compose 源码）：
          · v5.0.0 **移除**了内置 builder，`up --build` 改为调用外部 buildx 插件
            （`pkg/compose/build_bake.go` 里 `exec.CommandContext(ctx, buildx.Path...)`，
            要求 buildx ≥ 0.17，且**没有回退分支**）；
          · 我们的镜像只装 docker CLI + compose，**没有 buildx**；
          · 于是升到 v5 会让「一键更新上游」重新失败在 `up --build` 上 ——
            正是 issue #28 报的那个症状；
          · v2 有 `build_classic.go`（内置 builder）兜底，所以不需要 buildx。

        这条测试不看源码，只钉住版本号：**升级 compose 前请先确认 v2 之外的分支
        是否还需要 buildx 插件**，否则用户的一键更新会再次中断。
        """
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        m = re.search(r'ARG\s+COMPOSE_VERSION=(v[\d.]+)', df)
        self.assertIsNotNone(m, 'Dockerfile 里没找到 COMPOSE_VERSION')
        version = m.group(1)
        major = int(version.lstrip('v').split('.')[0])
        self.assertEqual(
            major, 2,
            f'compose 版本被改成了 {version}。若确实要升到 v3+，请先确认它是否仍'
            f'自带 builder —— v5 起 `up --build` 依赖外部 buildx 插件，而本镜像'
            f'没有装 buildx，会让一键更新上游失败。')

    def test_resolver_tries_both_forms(self) -> None:
        """解析顺序：宿主已有的 v2 → v1 → 都没有返回 None。"""
        m = self.mod
        with mock.patch.object(m, '_has_compose_v2', return_value=True), \
             mock.patch.object(m, '_has_compose_v1', return_value=True):
            self.assertEqual(m._compose_cmd(), ['docker', 'compose'])
        with mock.patch.object(m, '_has_compose_v2', return_value=False), \
             mock.patch.object(m, '_has_compose_v1', return_value=True):
            self.assertEqual(m._compose_cmd(), ['docker-compose'])
        with mock.patch.object(m, '_has_compose_v2', return_value=False), \
             mock.patch.object(m, '_has_compose_v1', return_value=False):
            self.assertIsNone(m._compose_cmd(),
                              '两个都没有时应返回 None，由调用方给出可执行的修法')

    def test_missing_compose_raises_actionable_error(self) -> None:
        """都没有时要报「怎么修」，而不是让用户对着 No such file 发呆。"""
        m = self.mod
        with mock.patch.object(m, '_has_compose_v2', return_value=False), \
             mock.patch.object(m, '_has_compose_v1', return_value=False), \
             mock.patch.object(m, '_read_upstream_ref', return_value=''), \
             mock.patch.object(m, '_compose_looks_customized', return_value=False), \
             mock.patch.object(m, '_missing_copy_sources', return_value=[]):
            (m.UPSTREAM_DIR / '.git').mkdir(parents=True, exist_ok=True)
            rep = _Rep()
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    m.update_upstream(rep)
                msg = str(ctx.exception)
                self.assertIn('compose', msg)
                self.assertIn('Dockerfile', msg, '应给出可执行的修法')
            finally:
                import shutil as _sh
                _sh.rmtree(m.UPSTREAM_DIR / '.git', ignore_errors=True)


class ComposeLocalCustomizationTest(unittest.TestCase):
    """issue #28-2：更新上游不能抹掉用户对 docker-compose.yml 的定制。

    报障现象：1Panel 用户给上游 compose 加了 `networks: 1panel-network`
    （external），一键更新后该文件被 `git reset --hard` 覆盖，容器重建时
    网络配置丢失、直接失联。

    原有的 `.patch` 备份**只是给人看的**，不会自动还原 —— 所以等于配置真的丢了。
    """

    def setUp(self) -> None:
        self.mod = _load_update_mod()
        self.rep = _Rep()

    def test_port_convergence_preserves_other_customizations(self) -> None:
        """端口收敛是纯文本替换，不能顺手动用户加的网络 / 卷。"""
        sample = (
            'services:\n'
            '  wb2api:\n'
            '    ports:\n'
            '      - "7863:7863"\n'
            '    networks:\n'
            '      - 1panel-network\n'
            'networks:\n'
            '  1panel-network:\n'
            '    external: true\n'
        )
        out = self.mod._port_converged(sample)
        self.assertIn('"127.0.0.1:7863:7863"', out, '端口应收敛')
        self.assertNotIn('"7863:7863"', out, '公网绑定不应残留')
        self.assertIn('1panel-network', out, '用户的网络配置被改了')
        self.assertIn('external: true', out, '用户的 external 声明被改了')

    def test_customization_detected_ignoring_our_port_change(self) -> None:
        """只有**用户的**改动才算定制；我们自己做的端口收敛不算。

        否则每次更新都会把旧 compose 原样写回，上游新增的 compose 字段
        永远进不来（把「保护定制」变成「冻结配置」）。
        """
        m = self.mod
        head = 'services:\n  wb2api:\n    ports:\n      - "7863:7863"\n'
        with tempfile.TemporaryDirectory() as td:
            up = Path(td)
            (up / 'docker-compose.yml').write_text(
                m._port_converged(head), encoding='utf-8')
            with mock.patch.object(m, 'UPSTREAM_DIR', up), \
                 mock.patch.object(m, 'run', return_value=(0, head)):
                self.assertFalse(m._compose_looks_customized(self.rep),
                                 '把我们的端口收敛当成了用户定制 —— 会冻结上游 compose 更新')

            # 用户真的改了 → 应识别为定制
            (up / 'docker-compose.yml').write_text(
                m._port_converged(head) + 'networks:\n  x:\n    external: true\n',
                encoding='utf-8')
            with mock.patch.object(m, 'UPSTREAM_DIR', up), \
                 mock.patch.object(m, 'run', return_value=(0, head)):
                self.assertTrue(m._compose_looks_customized(self.rep),
                                '用户的网络定制没被识别出来 —— 更新后会被抹掉')

    def test_customization_restored_after_pull(self) -> None:
        """端到端：更新流程里先留存、后被 git reset 抹掉、最后原样写回。"""
        m = self.mod
        custom = (
            'services:\n'
            '  wb2api:\n'
            '    ports:\n'
            '      - "127.0.0.1:7863:7863"\n'
            '    networks:\n'
            '      - 1panel-network\n'
            'networks:\n'
            '  1panel-network:\n'
            '    external: true\n'
        )
        with tempfile.TemporaryDirectory() as td:
            up = Path(td)
            (up / '.git').mkdir()
            compose = up / 'docker-compose.yml'
            compose.write_text(custom, encoding='utf-8')
            rep = _Rep()

            def fake_run(cmd, cwd=None, rep=None, check=True, **kw):
                if cmd[:2] == ['git', 'status']:
                    return 0, ' M docker-compose.yml\n'
                if cmd[:2] == ['git', 'diff']:
                    return 0, 'diff...'
                if cmd[:2] == ['git', 'rev-parse']:
                    return 0, 'abc12345'
                if cmd[:3] == ['git', 'checkout', '--']:
                    # 模拟丢弃本地改动
                    compose.write_text('services:\n  wb2api:\n    ports:\n      - "7863:7863"\n',
                                       encoding='utf-8')
                    return 0, ''
                if cmd[:2] == ['git', 'fetch'] or cmd[:2] == ['git', 'pull'] or cmd[:2] == ['git', 'reset']:
                    return 0, ''
                if cmd[:2] == ['docker', 'compose']:
                    return 0, 'ok'
                return 0, ''

            with mock.patch.object(m, 'UPSTREAM_DIR', up), \
                 mock.patch.object(m, 'DATA_DIR', up), \
                 mock.patch.object(m, 'run', side_effect=fake_run), \
                 mock.patch.object(m, '_compose_looks_customized', return_value=True), \
                 mock.patch.object(m, '_missing_copy_sources', return_value=[]), \
                 mock.patch.object(m, '_has_compose_v2', return_value=True), \
                 mock.patch.object(m, 'wait_health', return_value=True), \
                 mock.patch.object(m, '_clear_version_cache'):
                m.update_upstream(rep)

            text = compose.read_text(encoding='utf-8')
            self.assertIn('1panel-network', text, '用户的网络定制在更新后丢了')
            self.assertIn('external: true', text, 'external 声明在更新后丢了')
            self.assertIn('127.0.0.1:7863:7863', text, '端口收敛没重新施加')
            self.assertNotIn('"7863:7863"', text, '公网绑定残留')


class DbOpenFailureMessageTest(unittest.TestCase):
    """issue #30：数据库打不开时要说清「哪个路径、为什么、怎么修」。

    报障现象：docker compose 部署后容器起不来，日志里只有一行

        sqlite3.OperationalError: unable to open database file

    ——既不说路径也不说原因。最常见的成因是 **bind mount 的属主不对**：
    容器以 uid 10001 运行，而 docker 首次自动创建 `./data` 时归 root，
    于是容器内的 10001 写不进去。上游自己的 compose 对 `./data` 写了 chown
    提示，我们这边漏了，用户只能靠猜。
    """

    def test_message_names_path_reason_and_fix(self) -> None:
        from server import config, db
        exc = sqlite3.OperationalError('unable to open database file')
        msg = db._explain_db_open_failure(exc)
        self.assertIn(str(config.DB_PATH), msg, '必须指出是哪个库文件')
        self.assertIn('chown', msg, '要给出可直接执行的修法')
        self.assertIn('10001', msg, '要说明是哪个 uid')
        self.assertIn('./data', msg, '要指向宿主机上的那个目录')

    def test_message_mentions_named_volume_fallback(self) -> None:
        """改不了属主的场景（NFS/SMB）要给替代方案，不能只说 chown。"""
        from server import db
        msg = db._explain_db_open_failure(sqlite3.OperationalError('x'))
        self.assertIn('卷', msg, '应提示命名卷作为替代')

    def test_connect_wraps_into_runtime_error(self) -> None:
        """connect() 要把 sqlite 的原始错误换成上面那条说明。

        直接抛 OperationalError 的话，用户看到的还是那句无信息量的原文。
        """
        from server import db
        with mock.patch.object(db.sqlite3, 'connect',
                               side_effect=sqlite3.OperationalError('unable to open database file')):
            with self.assertRaises(RuntimeError) as ctx:
                db.connect()
        self.assertIn('chown', str(ctx.exception))

    def test_failed_connect_leaves_no_half_open_connection(self) -> None:
        """失败后不能留下半开的 `_conn`。

        留着的话，后续调用会拿到一个不可用的连接，报出更莫名的错误
        （例如「attempt to write a readonly database」），把真正的原因埋掉。
        """
        from server import db
        db._conn = None
        with mock.patch.object(db, 'SCHEMA', 'CREATE TABLE t(a); SELECT bogus syntax'):
            with self.assertRaises(Exception):
                db.connect()
        self.assertIsNone(db._conn, 'failed connect 后应把 _conn 复位')

    def test_compose_documents_data_ownership(self) -> None:
        """compose 里 `./data` 那条必须带权限提示（上游 compose 就有）。"""
        compose = (_ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        idx = compose.find('- ./data:/app/data')
        self.assertGreater(idx, 0, '没找到 data 挂载')
        # 该行上方 200 字符内应有 chown 提示
        above = compose[max(0, idx - 400):idx]
        self.assertIn('chown', above, './data 挂载缺少属主提示（issue #30 的成因）')
        self.assertIn('10001', above, '应写明 uid')


class DockerAssetsTest(unittest.TestCase):
    """部署资产存在且关键约定正确（这些错了用户装不起来）。"""

    def test_dockerfile_present_and_non_root(self) -> None:
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('FROM python:3.12', df)
        self.assertIn('USER app', df, '不应以 root 运行容器')
        self.assertIn('WB_RUN_MODE=docker', df,
                      '镜像里没设运行形态 —— 容器内会误判成宿主、去调 systemctl')
        # 前端产物必须来自静态导出（server 构建会产出 server 版，FastAPI 托管不了）。
        #
        # 断言的是**实际生效的 COPY 指令**（以 "COPY " 开头的那一行），不是
        # 文本里出现过 "COPY web/out" —— 注释里提到它也算匹配，那样会变成
        # 假绿（本文件此前正是如此：指令早改成 --from=，断言却还在看注释）。
        self.assertRegex(
            df, r'(?m)^COPY\s+--from=\S+\s+/\S*\s+/app/web/out\s*$',
            '镜像里没有把前端产物拷到 /app/web/out 的 COPY 指令')

    def test_frontend_built_in_container_when_missing(self) -> None:
        """工作区没有 web/out 时，镜像必须能自己构建前端（issue #38）。

        现场：`git clone && docker build .` 报

            ERROR: failed to build: ... "/web/out": not found

        因为 web/out 是构建产物、被 .gitignore 排除，clone 出来的工作区里没有它。
        修法是加一个 Node 阶段在容器内构建 —— 本测试锁住该阶段的存在与关键细节。
        """
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        self.assertRegex(df, r'(?m)^FROM\s+node:', '没有 Node 构建阶段，容器内无法构建前端')

        # 构建前必须判断"已有产物就跳过"：否则发布包/CI 场景会白跑一遍 npm
        self.assertRegex(df, r'if \[ -f /src/out/index\.html \]',
                         '前端阶段没有判断已有产物，发布会重复构建')
        # 静态导出开关：next.config.ts 靠它决定 output:'export'，
        # 漏了会构建出 server 版产物（FastAPI 托管不了，页面 404）
        self.assertIn('NEXT_OUTPUT_EXPORT=1', df,
                      '没有设置 NEXT_OUTPUT_EXPORT —— 会构建出非静态导出产物')
        # 构建完必须校验产物存在，否则失败会延后到运行时（页面白屏）
        self.assertIn('out/index.html', df)

    def test_dockerignore_keeps_frontend_source_and_drops_node_modules(self) -> None:
        """.dockerignore 必须放行前端**源码**、排除 node_modules。

        这一对容易配错且后果对立：
          · 误排 web/ 源码 → git clone 场景构建不出前端（回到 issue #38）；
          · 不排 node_modules → 多架构构建要把几百 MB 送进构建器（很慢）。

        `web/node_modules` 是精确路径，不会连带排除源码 —— 但不能写成
        `web/`（会同时排掉 out/ 与源码）。
        """
        di = (_ROOT / '.dockerignore').read_text(encoding='utf-8')
        lines = [l.strip() for l in di.splitlines()
                 if l.strip() and not l.strip().startswith('#')]
        self.assertIn('web/node_modules', lines, '未排除 node_modules，构建上下文会很大')
        for bad in ('web/', 'web', 'web/*'):
            self.assertNotIn(bad, lines,
                             f'.dockerignore 里的 {bad!r} 会连带排除前端源码与 out/')

    def test_compose_passes_npm_registry_arg(self) -> None:
        """compose 要能传 npm 镜像源（国内构建前端时必需），且与 Dockerfile 对齐。"""
        import yaml
        dc = yaml.safe_load((_ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        args = (dc['services']['workbuddy-manager'].get('build') or {}).get('args') or {}
        self.assertIn('NPM_REGISTRY', args, 'compose 没有暴露 NPM_REGISTRY')
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        self.assertRegex(df, r'(?m)^ARG\s+NPM_REGISTRY',
                         'Dockerfile 未声明 NPM_REGISTRY —— compose 传了也会被忽略')

    def test_compose_passes_mirror_and_base_path_args(self) -> None:
        """镜像里的两个国外下载源、以及子路径前缀，同样要 compose ↔ Dockerfile 对齐。

        这类「可选开关」最容易出的错法不是写坏，而是**只加了一边**：compose 里
        列了参数、Dockerfile 却没声明同名 ARG —— docker 对多余的 build-arg 只是
        忽略，于是用户填了值、构建照样卡在 download.docker.com / GitHub 上，
        而且没有任何报错。这条与上面的 NPM_REGISTRY 同款。
        """
        import yaml
        dc = yaml.safe_load((_ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        args = (dc['services']['workbuddy-manager'].get('build') or {}).get('args') or {}
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        for name in ('DOCKER_CLI_BASE', 'COMPOSE_URL_PREFIX', 'BASE_PATH'):
            self.assertIn(name, args, f'compose 没有暴露 {name}')
            self.assertRegex(df, rf'(?m)^ARG\s+{name}',
                             f'Dockerfile 未声明 {name} —— compose 传了也会被忽略')

    def test_dockerfile_structure_checker_passes(self) -> None:
        """跑一遍 Dockerfile 结构自检（指令拼写 / 阶段引用 / shell 配平）。

        本地与 CI 都不一定随时能跑 `docker build`（本机就没装），而这份
        Dockerfile 改错（阶段引用写错、shell 引号漏了）的代价是**用户装不上**。
        检查器在 dev/check_dockerfile.py，这里把它接进测试，让 CI 也能拦住。
        """
        import subprocess
        r = subprocess.run(
            [sys.executable, str(_ROOT / 'dev' / 'check_dockerfile.py')],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(r.returncode, 0,
                         'Dockerfile 结构自检失败：' + r.stdout + r.stderr)

    def test_dockerfile_build_logic_branches(self) -> None:
        """真跑一遍 Dockerfile 里前端阶段的分支判定（在等价目录上）。

        比读代码可靠：那个 RUN 块是纯 shell，可以在临时目录上执行，确认三种
        输入（有完整产物 / 产物不完整 / 无产物）各自走对分支 —— 这正是 issue #38
        修复的核心。缺 bash 的环境跳过（Windows 上是 Git Bash，CI 上是原生 bash）。
        """
        import subprocess
        import shutil
        if not shutil.which('bash'):
            self.skipTest('本环境没有 bash')
        r = subprocess.run(
            [sys.executable, str(_ROOT / 'dev' / 'check_dockerfile_build.py')],
            capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(r.returncode, 0,
                         '前端阶段分支判定不正确：' + r.stdout + r.stderr)

    def test_compose_has_restart_policy(self) -> None:
        """restart 策略是容器版「一键更新」能生效的前提：
        更新进程结束容器后，靠它用新代码拉起。"""
        import yaml
        dc = yaml.safe_load((_ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        svc = dc['services']['workbuddy-manager']
        self.assertIn(svc.get('restart'), ('unless-stopped', 'always'),
                      'restart 策略缺失 —— 容器更新后将不会自动恢复')
        # 默认只监听本机：管理端持有全部账号凭据，不该直接暴露公网
        ports = svc.get('ports') or []
        self.assertTrue(any('127.0.0.1' in str(p) for p in ports),
                        '端口未绑定到 127.0.0.1 —— 管理端不应默认暴露公网')

    def test_compose_persists_data(self) -> None:
        import yaml
        dc = yaml.safe_load((_ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        vols = dc['services']['workbuddy-manager'].get('volumes') or []
        self.assertTrue(any('/app/data' in str(v) for v in vols),
                        '未持久化 data 卷 —— 重建容器会丢失统计与审计记录')

    def test_upstream_dir_mounted(self) -> None:
        """必须挂载上游仓库目录。

        没挂的话容器内既拿不到上游的 docker-compose.yml（端口收敛无从下手），
        也无法在容器内 git pull —— 「更新上游」直接做不到。
        （这正是初版的问题：只挂了 auths 与 config.json 两条子路径。）
        """
        import yaml
        dc = yaml.safe_load((_ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        vols = [str(v) for v in (dc['services']['workbuddy-manager'].get('volumes') or [])]
        self.assertTrue(any('/opt/workbuddy2api' in v for v in vols),
                        '未挂载上游目录 —— 容器版将无法更新上游')

    def test_docker_socket_mounted_for_full_capability(self) -> None:
        """默认挂载 docker.sock，使容器版与宿主部署能力对齐。

        这是**判断修正**：初版刻意不挂，理由写成"挂了等于把宿主 root 交给容器"。
        但宿主部署本来就是 root（systemd 无 User=），而 root 进程本来就能
        `docker run -v /:/host` —— 两者权限等价，不挂只是让功能残缺。
        若不想要，注释掉即可（功能会自动降级并如实提示）。
        """
        import yaml
        dc = yaml.safe_load((_ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        vols = [str(v) for v in (dc['services']['workbuddy-manager'].get('volumes') or [])]
        self.assertTrue(any('docker.sock' in v for v in vols),
                        '未挂 docker.sock —— 容器版将无法重载/更新上游')


if __name__ == '__main__':
    unittest.main()


class ContainerReloadHintTest(unittest.TestCase):
    """无法自动重载上游时必须**如实告知**（而不是显示"正在自动应用"）。

    上游只在进程启动时读 config.json，改完必须重启上游容器才生效。宿主部署时
    管理端能直接 `docker restart`；但若环境**没有 docker 能力**（宿主没装
    docker，或容器没挂 docker.sock），这一步就做不了。此时若仍显示「正在自动
    应用到上游…」，用户会以为生效了，然后对着不生效的配置排查半天。

    判据同样是**实际能力**，不是"是否容器"。
    """

    @staticmethod
    def _save(docker_ok: bool) -> dict:
        import asyncio
        from server.routers import settings as st
        with mock.patch.object(st.updater, 'can_control_docker', return_value=docker_ok),                 mock.patch.object(st.wb2api, 'save_upstream_config',
                                  return_value={'available': True}),                 mock.patch.object(st.security, 'audit'),                 mock.patch.object(st, 'client_ip', return_value='127.0.0.1'),                 mock.patch.object(st.reload, 'request_restart', return_value=True):
            return asyncio.run(st.save_upstream(
                {'schedule': {'checkin_hours': [9]}}, None,
                {'username': 't', 'role': 'admin'}))

    def test_hint_when_docker_unavailable(self) -> None:
        res = self._save(docker_ok=False)
        self.assertFalse(res['reload_scheduled'], '不应声称已调度重载')
        self.assertIn('reload_hint', res, '必须给出手动重启指引')
        self.assertIn('docker compose', res['reload_hint'])

    def test_no_hint_when_docker_available(self) -> None:
        res = self._save(docker_ok=True)
        self.assertTrue(res['reload_scheduled'], '有 docker 能力时应正常自动重载')
        self.assertNotIn('reload_hint', res)

    def test_frontend_surfaces_hint(self) -> None:
        src = (_ROOT / 'web' / 'app' / '(main)' / 'settings' / 'page.tsx'
               ).read_text(encoding='utf-8')
        self.assertIn('reload_hint', src,
                      '设置页没读 reload_hint —— 用户会以为配置已生效')
        self.assertIn('notify.warn', src, '应以醒目提示（warn）转达')


class ReleasePackageIncludesDockerAssetsTest(unittest.TestCase):
    """发布包必须包含容器部署资产。

    实测漏过：打包步骤只复制了 server/ web/out deploy/ docs/ 与几个文档，
    **Dockerfile 与 docker-compose.yml 没打进去** —— 用户下载发布包后用不了
    容器部署（得回仓库另取这两个文件）。

    这条测试直接断言打包步骤的 cp 列表，防止再次漏掉。
    """

    def test_workflow_packages_docker_assets(self) -> None:
        wf = (_ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
        # 找到「组装发布目录」那一步的内容
        start = wf.find('组装发布目录')
        self.assertGreater(start, 0, '找不到打包步骤')
        end = wf.find('- name:', start + 10)
        block = wf[start:end if end > 0 else len(wf)]
        for asset in ('Dockerfile', 'docker-compose.yml'):
            self.assertIn(asset, block,
                          f'发布包没打进去 {asset} —— 用户拿到包也用不了容器部署')


class MultiArchImageTest(unittest.TestCase):
    """镜像必须同时支持 amd64 与 arm64。

    两处独立的架构缺陷，都实测过：

      1. **发版流程只构建 amd64**。原先用 `docker build`，它只产出 runner 自身的
         架构，于是推上去的镜像没有 arm64 变体 —— ARM 机器（Apple Silicon、ARM
         云主机）拉取时直接报 `no matching manifest for linux/arm64`。
         必须走 buildx 且 `platforms` 里同时列出两个架构。

      2. **Dockerfile 写死 x86_64 二进制**。安装 docker CLI 时下载地址固定为
         `.../static/stable/x86_64/...`。即便镜像变成多架构，容器里的 `docker`
         命令在 ARM 上仍是 x86_64 —— 而**构建期不报错**，要等到真正调用它
         （重载上游、读上游日志）才失败。这类"坏镜像"最难排查，所以在这里钉死。

    第 2 点尤其容易复发：Docker 自己的架构名（amd64/arm64）与官方静态包的目录名
    （x86_64/aarch64）**并不一致**，凭直觉写就会写错。
    """

    def _workflow(self) -> str:
        return (_ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')

    def test_workflow_builds_both_architectures(self) -> None:
        wf = self._workflow()
        self.assertIn('platforms:', wf, '镜像构建没声明 platforms —— 只会产出单架构')
        # 取 platforms 那一行，确认两个架构都在
        line = next((l for l in wf.splitlines() if 'platforms:' in l), '')
        self.assertIn('linux/amd64', line)
        self.assertIn('linux/arm64', line,
                      'arm64 不在 platforms 里 —— ARM 用户拉不到镜像')

    def test_workflow_uses_buildx(self) -> None:
        """多架构必须走 buildx：普通 `docker build` 无法产出 manifest list。"""
        wf = self._workflow()
        self.assertIn('setup-buildx-action', wf)
        self.assertIn('build-push-action', wf)
        self.assertIn('setup-qemu-action', wf,
                      '跨架构构建 arm64 层需要 QEMU（runner 是 amd64）')

    def test_dockerfile_does_not_hardcode_x86_64(self) -> None:
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        # 只检查 docker CLI 下载那一行：别的注释里出现 x86_64 是正常的（解释用）
        dl = [l for l in df.splitlines()
              if 'download.docker.com' in l and not l.lstrip().startswith('#')]
        self.assertTrue(dl, '找不到 docker CLI 下载行')
        for line in dl:
            self.assertNotIn('stable/x86_64', line,
                             'docker CLI 下载地址写死了 x86_64 —— ARM 上装的是跑不起来的二进制')

    def test_dockerfile_maps_arch_names(self) -> None:
        """架构名映射必须把 amd64→x86_64、arm64→aarch64 **映射正确**。

        官方静态包目录名与 Docker 架构名不一致，是本项目踩过的坑；这里锁住映射，
        避免以后有人"顺手简化"成直接用 TARGETARCH。

        注意断言的是 `DOCKER_ARCH=` 的**取值**，而不是"文件里出现过 aarch64"——
        后者是无效断言：`aarch64` 在 case 模式的左侧也出现，把 arm64 错映射成
        x86_64 时它照样通过（本测试初版就是这样漏掉的）。
        """
        df = (_ROOT / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('TARGETARCH', df, '没使用 buildx 注入的 TARGETARCH')

        # 解析 case 分支：`<patterns>)  DOCKER_ARCH=<value>`
        mapping = {}
        for patterns, value in re.findall(
                r'^\s*([\w\s|]+?)\)\s*DOCKER_ARCH=(\w+)', df, re.M):
            for name in patterns.split('|'):
                mapping[name.strip()] = value

        self.assertEqual(mapping.get('amd64'), 'x86_64', f'解析到的映射：{mapping}')
        self.assertEqual(mapping.get('x86_64'), 'x86_64', f'解析到的映射：{mapping}')
        self.assertEqual(mapping.get('arm64'), 'aarch64',
                         f'arm64 没映射到 aarch64 —— ARM 上会装成 x86_64 二进制。'
                         f'解析到的映射：{mapping}')
        self.assertEqual(mapping.get('aarch64'), 'aarch64', f'解析到的映射：{mapping}')
        # 未识别的架构必须构建期失败，而不是产出坏镜像
        self.assertIn('exit 1', df, '不支持的架构应直接失败')


# 模板**随仓库分发**（PR #58）：用户 fork 之后把它复制进自己 fork 的
# `.github/workflows/` 才会生效。这里直接测模板本身，而不是等它在
# `.github/workflows/build-image.yml` 下出现 —— 后者在本仓库永远不会出现
# （我们不跑它：本仓库由发版流程构建镜像），于是下面这几条约束曾经**一直
# 在跳过**，模板写错也无人发现（实测就是这样：模板先落在 deploy/fork-image/，
# 约束测试空转了）。
_FORK_IMAGE_WF = _ROOT / 'deploy' / 'fork-image' / 'build-image.yml'


@unittest.skipUnless(_FORK_IMAGE_WF.is_file(), 'fork 镜像工作流模板不存在，跳过')
class ForkImageWorkflowTest(unittest.TestCase):
    """fork 专用镜像工作流的几条约束（测的是 `deploy/fork-image/` 里的**模板**）。

    该工作流**只构建推送镜像**，不创建 Release、不签名 —— 因为签名信任链只覆盖
    正式发布包，在 fork 上造一个没有 .sig 的 Release 只会产出"看起来能装、实际
    装不上"的东西（用户侧一键更新会拒绝安装）。

    最值得锁的是**镜像名**：原包名 `ghcr.io/<owner>/workbuddy-manager` 在该命名
    空间下已被一个未链接到本仓库的包占用，fork 的 token 对它没有写权限，推送必然
    失败（实测 `denied: permission_denied: write_package`）。换成新包名后推送成功。
    这个坑很容易被"顺手改回原包名"重新踩到。
    """

    def _wf(self) -> str:
        return _FORK_IMAGE_WF.read_text(encoding='utf-8')

    def test_does_not_touch_releases(self) -> None:
        wf = self._wf()
        for forbidden in ('gh release create', 'gh release upload', 'gh release delete'):
            self.assertNotIn(forbidden, wf,
                             f'fork 工作流不该动 Release（{forbidden}）—— 未签名的 Release '
                             f'会被用户的一键更新拒绝安装')

    def test_uses_standalone_package_name(self) -> None:
        wf = self._wf()
        self.assertIn('workbuddy-manager-multiarch', wf,
                      '镜像名被改回原包名了？该包未链接到本仓库，fork 推送会被拒')

    def test_builds_both_architectures(self) -> None:
        wf = self._wf()
        line = next((l for l in wf.splitlines() if 'platforms:' in l), '')
        self.assertIn('linux/amd64', line)
        self.assertIn('linux/arm64', line, 'arm64 不在 platforms 里 —— ARM 用户拉不到镜像')

    def test_declares_packages_write(self) -> None:
        """推 GHCR 必须显式声明 packages: write，只给 contents 会被拒。"""
        wf = self._wf()
        self.assertIn('packages: write', wf)

    def test_image_tags_are_lowercased(self) -> None:
        """镜像名必须转小写。

        GHCR 要求仓库名全小写，而 owner 是 `JinsFoni`（含大写）。直接把
        `github.repository_owner` 拼进 tag 会被拒：

            invalid tag "ghcr.io/JinsFoni/...": repository name must be lowercase

        实测踩过。build-push-action 的 tags 里做不了 `${VAR,,}`，所以必须有一个
        独立步骤先算好；这里断言 tags 引用的是那个算好的输出，而不是原始 owner。
        """
        wf = self._wf()
        self.assertIn('${IMAGE,,}', wf,
                      '没有把小写转换步骤 —— GHCR 会拒收含大写的镜像名')
        # tags 里不能直接出现未转义的 repository_owner
        tags_block = wf[wf.find('tags:'):]
        tags_block = tags_block[:tags_block.find('provenance')]
        self.assertNotIn('github.repository_owner', tags_block,
                         'tags 里直接用了 repository_owner（含大写）—— 应改用转小写后的输出')


class UpstreamSourceFallbackTest(unittest.TestCase):
    """上游仓库没了之后的安装路径（install.sh 的 UPSTREAM_SRC）。

    上游原仓库（Sliverkiss/workbuddy2api）自 2026-09-23 起不可访问，克隆那一步对
    新装用户是死的。脚本因此支持三种本地来源，并且**这是新用户第一次接触本项目
    时会走的路径**，不能悄悄回归。

    两条断言方式并用：
      · 源码级：三种来源分支、同目录短路、失败提示的三条退路都在；
      · 真跑一遍：解压那条分支的参数在真实目录上验证过 ——
        GitHub 导出的包都带顶层目录，少了 `--strip-components=1` 会把文件散到
        上一层，docker compose 找不到 Dockerfile（与 Dockerfile 分支的验法同款）。
    """

    def setUp(self) -> None:
        self.sh = (_ROOT / 'deploy' / 'install.sh').read_text(encoding='utf-8')

    def test_three_source_shapes_supported(self) -> None:
        self.assertIn('UPSTREAM_SRC="${UPSTREAM_SRC:-}"', self.sh, '没有 UPSTREAM_SRC 开关')
        for shape in ('*.tar.gz | *.tgz', '*.zip'):
            self.assertIn(shape, self.sh, f'缺少 {shape} 分支')
        self.assertIn('cp -a "$UPSTREAM_SRC"/. "$UPSTREAM_DIR"/', self.sh,
                      '缺少「本地目录」分支')
        # 这条必须**盯脚本本身**：下面那条真跑用例验的是「这样解压是对的」，
        # 脚本里若是漏了这个参数，只跑自己的命令照样绿（第一版就漏在这）。
        self.assertIn('tar -xzf "$UPSTREAM_SRC" -C "$UPSTREAM_DIR" --strip-components=1',
                      self.sh, '解压分支少了 --strip-components=1（文件会散在上一层）')

    def test_same_directory_is_short_circuited(self) -> None:
        """源码本来就在目标目录时不要自己复制自己（复用 /opt/workbuddy2api 的典型情形）。"""
        self.assertIn('pwd -P', self.sh, '没有做「同一目录」判断')

    def test_bundled_upstream_is_preferred_and_ordered_first(self) -> None:
        """发布包自带 `upstream/` 时优先用它，且顺序必须在 git/克隆之前。

        上游源码随 Release 包分发（公开仓库不放上游代码），所以「包里有就用包里
        的」是**新用户第一次安装**会走的路。顺序错了（比如先试 git clone）会让
        离线机器白等一次超时，最后还可能失败。
        """
        self.assertIn('UPSTREAM_DIR}/docker-compose.yml', self.sh,
                      '没有检测发布包自带的 upstream/')
        i_bundled = self.sh.index('BUNDLED=')
        # 用**安装分支里**那个 elif 作锚点：脚本开头还有一处 [ -d .../.git ] 属于
        # 「已装过上游，只需确保容器在跑」的分支，拿它比会得到错误的先后（试过）。
        i_git = self.sh.index('elif [ -d "${UPSTREAM_DIR}/.git" ]')
        self.assertLess(i_bundled, i_git,
                        '包内自带的源码要排在「已有 git 目录」之前判断')
        # 检测到之后必须真的接上：赋给 UPSTREAM_SRC（后面复制/解压那段用的就是它）
        self.assertIn('UPSTREAM_SRC="$BUNDLED"', self.sh,
                      '检测到了包内源码却没接到 UPSTREAM_SRC —— 等于没检测')

    def test_release_workflow_embeds_upstream_source(self) -> None:
        """发布流程要把上游源码塞进包里，且**取不到时不阻断发布**。

        上游源码不放进本仓库，所以「随包分发」是用户拿到源码
        的唯一常规渠道。两步都不能少：
          · 从固定的载体 Release 取（tag upstream-src，标 pre-release 才行——
            否则它会成为 releases/latest，把面板的更新检查带偏）；
          · 取不到只记 warning 继续打包（否则一次网络抖动就让整个发布失败）。
        """
        import yaml
        wf = (_ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
        yaml.safe_load(wf)   # 先确保 YAML 没写坏
        self.assertIn('releases/download/upstream-src/workbuddy2api-src.tar.gz', wf,
                      '没有从载体 Release 取上游源码')
        self.assertIn("$STAGE/upstream", wf, '取回来的源码没有放进发布目录')
        self.assertIn('::warning::', wf, '取不到源码时会直接失败 —— 应该只告警')

    def test_clone_failure_lists_ways_out(self) -> None:
        """克隆失败要把三条退路写清楚——否则用户只看到 git 的 not found。"""
        block = self.sh[self.sh.find('克隆上游仓库失败'):]
        block = block[:block.find('fi\n') + 3]
        for hint in ('UPSTREAM_SRC=', 'UPSTREAM_REPO=', '--skip-upstream'):
            self.assertIn(hint, block, f'失败提示里缺 {hint}')

    def test_tar_strips_top_level_directory(self) -> None:
        """真跑：把「带顶层目录的源码包」按脚本的方式解开，文件必须落在根上。"""
        import shutil
        import subprocess
        if not shutil.which('tar'):
            self.skipTest('本机没有 tar')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'pkg' / 'workbuddy2api-abc1234'
            (src / 'scripts').mkdir(parents=True)
            (src / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
            (src / 'scripts' / 'x.py').write_text('', encoding='utf-8')
            tarball = root / 'upstream.tar.gz'
            subprocess.run(['tar', '-czf', str(tarball), '-C', str(root / 'pkg'),
                            'workbuddy2api-abc1234'], check=True)
            dest = root / 'dest'
            dest.mkdir()
            subprocess.run(['tar', '-xzf', str(tarball), '-C', str(dest),
                            '--strip-components=1'], check=True)
            self.assertTrue((dest / 'docker-compose.yml').is_file(),
                            '解压后 docker-compose.yml 不在根目录 —— 少了 --strip-components=1？')
            self.assertTrue((dest / 'scripts' / 'x.py').is_file(), '子目录内容也应在')
            self.assertFalse((dest / 'workbuddy2api-abc1234').exists(),
                             '顶层目录没被剥掉')
