"""更新流程的健壮性回归（上游构建失败相关）。

真实事故：上游某提交删掉了一批脚本、却漏改 Dockerfile（仍 COPY 已删除的
`scripts/probe_active.py`），导致 `docker compose up --build` 失败。管理端
当时只把它当普通命令失败上报，用户看到的是难懂的 checksum 报错，
既不知道原因、也没有退路。

这里覆盖三件事：
  1. Dockerfile 预检能提前查出「引用了不存在的文件」，且不误报；
  2. 固定上游版本（回退手段）的读写与格式校验；
  3. 构建失败诊断能把 docker 报错归纳成人话。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import updater  # noqa: E402

# 复用 worker 里的纯函数（deploy/update.py 是独立脚本，不在包内）
DEPLOY_DIR = pathlib.Path(__file__).resolve().parents[2] / 'deploy'
sys.path.insert(0, str(DEPLOY_DIR))
import update as worker  # noqa: E402


class DockerfilePrecheck(unittest.TestCase):
    """构建前预检：查出 Dockerfile 引用了仓库里不存在的文件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig = worker.UPSTREAM_DIR
        worker.UPSTREAM_DIR = self.dir

    def tearDown(self) -> None:
        worker.UPSTREAM_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, dockerfile: str, files: list[str]) -> None:
        (self.dir / 'Dockerfile').write_text(dockerfile, encoding='utf-8')
        for f in files:
            path = self.dir / f
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('', encoding='utf-8')

    def test_detects_missing_copy(self) -> None:
        """复刻真实事故：COPY scripts/probe_active.py 但文件已删除。"""
        self._write(
            'FROM alpine\n'
            'COPY --from=build /out/wb2api /app/wb2api\n'
            'COPY login.sh /app/\n'
            'COPY scripts/probe_active.py /app/scripts/probe_active.py\n',
            ['login.sh'],
        )
        self.assertEqual(worker._missing_copy_sources(), ['scripts/probe_active.py'])

    def test_no_false_positive_when_present(self) -> None:
        self._write('COPY a.py /app/a.py\nCOPY b.py /app/b.py\n', ['a.py', 'b.py'])
        self.assertEqual(worker._missing_copy_sources(), [])

    def test_ignores_stage_copy_wildcard_and_url(self) -> None:
        """阶段拷贝 / 通配符 / URL 不是本地文件，不能误报。"""
        self._write(
            'COPY --from=build /out/x /app/x\n'
            'COPY scripts/*.py /app/scripts/\n'
            'COPY https://example.com/f /app/f\n'
            'RUN echo COPY not.a.copy /tmp/\n',
            [],
        )
        self.assertEqual(worker._missing_copy_sources(), [])

    def test_missing_dockerfile_is_not_an_error(self) -> None:
        self.assertEqual(worker._missing_copy_sources(), [])


class BuildFailureDiagnosis(unittest.TestCase):
    """把 docker 的报错归纳成一句人话。"""

    def test_detects_dockerfile_missing_file(self) -> None:
        out = ('failed to calculate checksum of ref x::y: '
               '"/scripts/probe_active.py": not found')
        hint = worker._diagnose_build_failure(out)
        self.assertIn('不存在的文件', hint)
        self.assertIn('上游', hint)

    def test_detects_disk_and_network(self) -> None:
        self.assertIn('磁盘', worker._diagnose_build_failure('no space left on device'))
        self.assertIn('网络', worker._diagnose_build_failure('dial tcp 1.2.3.4:443: i/o timeout'))
        self.assertIn('权限', worker._diagnose_build_failure('permission denied'))

    def test_unknown_returns_empty(self) -> None:
        self.assertEqual(worker._diagnose_build_failure('some novel error'), '')


class UpstreamRefPinning(unittest.TestCase):
    """固定上游版本（回退手段）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_file = updater.UPSTREAM_REF_FILE
        self._orig_data = config.DATA_DIR
        updater.UPSTREAM_REF_FILE = pathlib.Path(self._tmp.name) / 'ref.txt'
        config.DATA_DIR = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        updater.UPSTREAM_REF_FILE = self._orig_file
        config.DATA_DIR = self._orig_data
        self._tmp.cleanup()

    def test_default_is_follow_branch(self) -> None:
        self.assertEqual(updater.upstream_ref(), '')

    def test_set_and_read_commit(self) -> None:
        self.assertEqual(updater.set_upstream_ref('98b5e160'), '98b5e160')
        self.assertEqual(updater.upstream_ref(), '98b5e160')

    def test_set_and_read_tag(self) -> None:
        self.assertEqual(updater.set_upstream_ref('v1.2.3'), 'v1.2.3')

    def test_full_sha_accepted(self) -> None:
        sha = '4a80249f6ffb7eea087ee86a5e6ade57432bb895'
        self.assertEqual(updater.set_upstream_ref(sha), sha)

    def test_clear_restores_branch_tracking(self) -> None:
        updater.set_upstream_ref('98b5e160')
        self.assertEqual(updater.set_upstream_ref(''), '')
        self.assertEqual(updater.upstream_ref(), '')
        self.assertFalse(updater.UPSTREAM_REF_FILE.exists(), '取消固定应删除文件')

    def test_rejects_injection(self) -> None:
        """该值会进入 git 参数位，必须严格校验。"""
        for bad in ('a;rm -rf /', '$(whoami)', 'a b', 'a\nb', '../x', '-flag', '`id`', 'a|b'):
            with self.assertRaises(ValueError, msg=bad):
                updater.set_upstream_ref(bad)
        self.assertEqual(updater.upstream_ref(), '', '非法输入不应落盘')

    def test_status_exposes_ref(self) -> None:
        updater.set_upstream_ref('98b5e160')
        self.assertEqual(updater.read_status().get('upstream_ref'), '98b5e160')


class UpstreamRemoteGoneTest(unittest.TestCase):
    """上游仓库没了的时候，更新日志要把原因说对。

    背景：上游原仓库 Sliverkiss/workbuddy2api 自 2026-09-23 起已不可访问。
    此前拉取失败一律写「git fetch 失败（网络问题？）」——用户会去查网络、
    反复重试一个必然失败的地址。真正的原因与出路要写清楚：源码还在本地、
    本次照常重建；以后要更新就指向自己的副本。

    判据用**真实的 git 报文**：git 的原文是
    `fatal: repository 'https://…' not found`（仓库地址夹在中间），
    所以「repository…not found」不能写成连写匹配（头一版就栽在这，测试看得见）。
    """

    GONE = [
        "remote: Repository not found.\nfatal: repository 'https://github.com/x/y.git/' not found",
        "fatal: repository 'https://github.com/Sliverkiss/workbuddy2api.git/' not found",
        'fatal: could not read from remote repository',
        "fatal: 'https://example.com/x.git' does not appear to be a git repository",
    ]
    OTHER = [
        'fatal: could not resolve host: github.com',
        'fatal: unable to access ...: Connection timed out',
        '',
    ]

    def test_repo_gone_is_named_as_such(self) -> None:
        """远端拿不到代码时：说清「源码从发布包来」这条出路，且**不把人往网络上引**。

        判据盯的是**实质**而不是某四个字：措辞会随文案调整（本轮就把「已不可访问」
        改成了中性的「远端仓库取不到代码」），但「出路要说清、别甩锅网络」这两条不变。
        """
        for out in self.GONE:
            with self.subTest(out=out.splitlines()[0] if out else ''):
                hint = worker._fetch_failed_hint(out)
                self.assertIn('WB_UPSTREAM_REPO', hint, '要给出可操作的出路')
                self.assertTrue('发布包' in hint or '包内' in hint,
                                '要说清源码随发布包分发这条正路')
                self.assertNotIn('网络', hint, '这不是网络问题，别让人去查网络')

    def test_network_failure_is_not_blamed_on_the_missing_repo(self) -> None:
        """网络、超时这类失败不能误报成「远端没了」——那会把排查带偏。"""
        for out in self.OTHER:
            with self.subTest(out=out):
                hint = worker._fetch_failed_hint(out)
                self.assertNotIn('发布包', hint)
                self.assertIn('网络', hint, '网络类失败要如实说是网络')

    def test_pinned_target_is_mentioned(self) -> None:
        hint = worker._fetch_failed_hint(self.GONE[1], 'v1.2.3')
        self.assertIn('v1.2.3', hint, '固定了版本时要说明是哪一次拉取失败')


class BundledUpstreamSyncTest(unittest.TestCase):
    """面板更新时同步包内自带的上游源码（上游公开地址已不可用，源码随包分发）。

    这里钉的是三条**不能错**的性质：

      · 用户数据一个字节都不能动：`config.json`（api_key）、`auths/`（账号凭据）、
        `data/`（运行数据）——写坏了用户就登不上、账号全丢；
      · 用户在 compose 里加过的东西（网络 / 卷 / 端口之外）要保住 —— issue #28 就
        是这套定制被更新抹掉，容器重建后连不上网络；
      · 内容没变要返回 0：调用方据此跳过容器重建，否则每次更新都白等一次构建、
        还把上游短暂停掉。
    """

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.target = root / 'upstream-target'
        self.bundle = root / 'bundle'
        self._orig = worker.UPSTREAM_DIR
        worker.UPSTREAM_DIR = self.target

        # 目标：一个「非 git」的上游目录（装的时候用 UPSTREAM_SRC / 包内 upstream）
        (self.target / 'scripts').mkdir(parents=True)
        (self.target / 'docker-compose.yml').write_text(
            'services:\n  workbuddy2api:\n    ports: ["127.0.0.1:7863:7863"]\n',
            encoding='utf-8')
        (self.target / 'scripts' / 'checkin.py').write_text('OLD\n', encoding='utf-8')
        (self.target / 'scripts' / 'keep.py').write_text('KEEP\n', encoding='utf-8')
        # 用户数据与凭据
        (self.target / 'config.json').write_text('{"api_key": "secret"}\n', encoding='utf-8')
        (self.target / 'auths').mkdir()
        (self.target / 'auths' / 'workbuddy-a.json').write_text('{"uid":"a"}', encoding='utf-8')
        (self.target / 'data').mkdir()
        (self.target / 'data' / 'state.json').write_text('{"x":1}', encoding='utf-8')

        # 包内自带的上游源码
        (self.bundle / 'scripts').mkdir(parents=True)
        (self.bundle / 'docker-compose.yml').write_text(
            'services:\n  workbuddy2api:\n    ports: ["7863:7863"]\n', encoding='utf-8')
        (self.bundle / 'scripts' / 'checkin.py').write_text('NEW\n', encoding='utf-8')
        (self.bundle / 'scripts' / 'brand_new.py').write_text('NEW FILE\n', encoding='utf-8')
        # 包内不该带用户数据，但真带了也不能覆盖（防御）
        (self.bundle / 'config.json').write_text('{"api_key": "from-bundle"}\n', encoding='utf-8')

    def tearDown(self) -> None:
        worker.UPSTREAM_DIR = self._orig
        self._tmp.cleanup()

    def _rep(self):
        class R:
            def __init__(self):
                self.logs = []

            def log(self, msg, level='info'):
                self.logs.append(str(msg))

        return R()

    def test_syncs_code_and_counts_changes(self) -> None:
        n = worker._sync_bundled_upstream(self.bundle, self._rep())
        # 改写 2 个（checkin.py + docker-compose.yml）、新增 1 个。
        # compose 也算改动是对的：包内那份是「上游原样」，同步后由调用方
        # 重新施加端口收敛（见下一条用例）。
        self.assertEqual(n, 3, '应改写 2 个 + 新增 1 个')
        self.assertEqual((self.target / 'scripts' / 'checkin.py').read_text(encoding='utf-8'),
                         'NEW\n')
        self.assertTrue((self.target / 'scripts' / 'brand_new.py').is_file())
        self.assertEqual((self.target / 'scripts' / 'keep.py').read_text(encoding='utf-8'),
                         'KEEP\n', '包里没有的文件保留原地，不删')

    def test_port_convergence_is_reapplied_after_sync(self) -> None:
        """同步会采用包内那份（上游原样、公网可达）的 compose —— 安全基线必须重来一遍。

        这一步在 update_manager 里紧跟同步之后：`enforce_local_bind`。漏掉
        它，上游就会重新监听 0.0.0.0:7863，端口收敛形同虚设。
        """
        worker._sync_bundled_upstream(self.bundle, self._rep())
        worker.enforce_local_bind(self._rep())
        compose = (self.target / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('127.0.0.1:7863:7863', compose,
                      '同步后没有重新收敛端口 —— 上游会暴露到公网')

    def test_user_data_untouched(self) -> None:
        worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertEqual((self.target / 'config.json').read_text(encoding='utf-8'),
                         '{"api_key": "secret"}\n', 'config.json 被覆盖 = 用户 api_key 丢了')
        self.assertEqual((self.target / 'auths' / 'workbuddy-a.json').read_text(encoding='utf-8'),
                         '{"uid":"a"}', 'auths 被覆盖 = 账号凭据丢了')
        self.assertEqual((self.target / 'data' / 'state.json').read_text(encoding='utf-8'),
                         '{"x":1}', 'data 被覆盖 = 运行数据丢了')

    def test_customized_compose_is_preserved(self) -> None:
        """除端口收敛之外的定制（加网络等）要保住——issue #28 的原样。"""
        custom = ('services:\n  workbuddy2api:\n'
                  '    ports: ["127.0.0.1:7863:7863"]\n'
                  '    networks: [external-net]\n'
                  'networks:\n  external-net:\n    external: true\n')
        (self.target / 'docker-compose.yml').write_text(custom, encoding='utf-8')
        worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertIn('external-net',
                      (self.target / 'docker-compose.yml').read_text(encoding='utf-8'),
                      '用户加的 external 网络被抹掉了')

    def test_plain_port_convergence_is_not_treated_as_customization(self) -> None:
        """只有端口收敛（我们自己制造的差异）时，应当**采用包内那份**，不算定制。"""
        worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertNotIn('127.0.0.1:7863',
                         (self.target / 'docker-compose.yml').read_text(encoding='utf-8'),
                         '端口收敛被误判成用户定制，导致 compose 永远是旧的')

    def test_second_sync_reports_no_change(self) -> None:
        worker._sync_bundled_upstream(self.bundle, self._rep())
        n = worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertEqual(n, 0, '内容一致时必须返回 0（调用方据此跳过容器重建）')

    def test_git_upstream_is_left_alone(self) -> None:
        (self.target / '.git').mkdir()
        n = worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertEqual(n, 0)
        self.assertEqual((self.target / 'scripts' / 'checkin.py').read_text(encoding='utf-8'),
                         'OLD\n', 'git 部署有自己的更新通道，不该被包内源码覆盖')

    def test_missing_bundle_is_noop(self) -> None:
        empty = self.bundle.parent / 'empty'
        empty.mkdir()
        n = worker._sync_bundled_upstream(empty, self._rep())
        self.assertEqual(n, 0)

    def test_update_manager_reapplies_baseline_after_sync(self) -> None:
        """接线：同步之后紧跟端口收敛，且**在重建之前**。

        上面那条只证明了 `enforce_local_bind` 本身有效，没证明它被调用 ——
        这正是我评审时栽过的那类假绿（测了函数、没测调用点与顺序）。
        """
        src = pathlib.Path(worker.__file__).read_text(encoding='utf-8')
        i = src.index('_sync_bundled_upstream(new_root')
        tail = src[i:i + 800]
        self.assertIn('enforce_local_bind(rep)', tail,
                      '同步了包内源码却没重新施加端口收敛 —— 上游会暴露到公网')
        self.assertLess(tail.index('enforce_local_bind(rep)'),
                        tail.index('rebuild_upstream(rep)'),
                        '顺序反了：收敛必须在重建之前，否则重建时用的是公网可达那份 compose')


if __name__ == '__main__':
    unittest.main()
