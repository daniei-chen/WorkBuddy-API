"""端到端：一键更新的完整链路（下载 → 验签 → 解压 → 安装）。

与 test_release_signature.py 的分工：
  * 那边是**单元**测试，只锁「验签这个函数」和「接线是否接上」；
  * 这边跑**真实的 update_manager**，只有 GitHub API 的返回是伪造的
    （它指向 file:// 的假 Release），其余全程真跑：下载、验签、解压、
    替换 server/ 与 web/out/、同步 .version 与文档。

为什么值得单独写：单元测试全绿但主流程装错了包/装到一半的情况是存在的，
而这正是供应链攻击最想要的结果——**包验过了，然后在安装阶段被换掉**。
只有把整条链路跑一遍才能锁住「验签的包 == 安装的包」。

同时验证失败时的**不落盘**性质：验签不过，安装目录必须一个字节都没变。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _ssh_keygen_available() -> bool:
    return shutil.which('ssh-keygen') is not None


class _Rep:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.signature: dict | None = None
        self.state: dict = {}

    def log(self, msg: str, level: str = 'info') -> None:
        self.lines.append((level, msg))

    def step(self, name: str) -> None:
        self.lines.append(('info', f'== {name} =='))

    def set_target_version(self, tag: str) -> None:
        self.state['target_version'] = str(tag or '').strip().lstrip('vV')

    def set_signature(self, status: str, detail: str = '') -> None:
        self.signature = {'status': status, 'detail': detail}

    def finish(self, ok: bool) -> None:
        self.state['ok'] = ok

    def text(self) -> str:
        return '\n'.join(m for _, m in self.lines)

    def warned(self) -> bool:
        return any(lvl == 'warn' for lvl, _ in self.lines)


@unittest.skipUnless(_ssh_keygen_available(), '需要 ssh-keygen（OpenSSH 8.0+）')
class UpdateEndToEndTest(unittest.TestCase):
    """真实跑 update_manager，只伪造 GitHub API 的返回。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.key = cls.dir / 'signing'
        subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-N', '', '-C', 'test', '-f', str(cls.key)],
            check=True, capture_output=True,
        )
        cls.pub = cls.key.with_suffix('.pub').read_text(encoding='utf-8').strip()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    # ── 夹具 ──

    def _make_install(self, name: str) -> Path:
        """造一个"已部署"的安装目录（旧版本）。"""
        inst = Path(tempfile.mkdtemp(dir=self.dir)) / name
        (inst / 'server').mkdir(parents=True)
        (inst / 'server' / 'main.py').write_text("version='1.0.0'\n# OLD\n", encoding='utf-8')
        (inst / 'server' / 'requirements.txt').write_text('', encoding='utf-8')
        (inst / 'web' / 'out').mkdir(parents=True)
        (inst / 'web' / 'out' / 'index.html').write_text('<html>OLD</html>', encoding='utf-8')
        (inst / 'deploy').mkdir()
        # 安装目录里放一份"正在使用的" update.py（就是 repo 里这份）
        shutil.copyfile(_ROOT / 'deploy' / 'update.py', inst / 'deploy' / 'update.py')
        (inst / '.version').write_text('v1.0.0\n', encoding='utf-8')
        return inst

    def _make_release(self, name: str, *, version: str = '9.9.9',
                      deploy_diff: bool = True) -> tuple[Path, Path]:
        """打一个真实发布包并签名，返回 (tar.gz, sig) 路径。"""
        stage = Path(tempfile.mkdtemp(dir=self.dir)) / f'workbuddy-manager-v{version}'
        (stage / 'server').mkdir(parents=True)
        (stage / 'server' / 'main.py').write_text(
            f"version='{version}'\n# NEW\n", encoding='utf-8')
        (stage / 'server' / 'requirements.txt').write_text('', encoding='utf-8')
        (stage / 'web' / 'out').mkdir(parents=True)
        (stage / 'web' / 'out' / 'index.html').write_text('<html>NEW</html>', encoding='utf-8')
        (stage / '.version').write_text(f'v{version}\n', encoding='utf-8')
        (stage / 'CHANGELOG.md').write_text('# 更新日志\n\n## [9.9.9]\n- 新\n', encoding='utf-8')
        # 包体积下限（download() 会拒绝 <100KB，避免把错误页当包）。
        # 必须用**不可压缩**的数据：全零在 gzip 下会缩到几百字节，
        # 那样测的就不是"正常下载"而是"下载异常偏小"了。
        (stage / 'server' / 'big.bin').write_bytes(os.urandom(200_000))
        if deploy_diff:
            (stage / 'deploy').mkdir()
            (stage / 'deploy' / 'update.py').write_text('# 包内的（随包更新）\n', encoding='utf-8')

        pkg = Path(tempfile.mkdtemp(dir=self.dir)) / f'workbuddy-manager-v{version}.tar.gz'
        subprocess.run(['tar', 'czf', str(pkg), '-C', str(stage.parent), stage.name],
                       check=True, capture_output=True)
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', str(self.key), '-n', 'file', str(pkg)],
                       check=True, capture_output=True)
        sig = Path(str(pkg) + '.sig')
        assert sig.is_file()
        return pkg, sig

    def _load(self, install: Path, pub: str | None = None, **env):
        keys = ['WB_INSTALL_DIR', 'WB_DATA_DIR', 'WB_UPDATE_STATUS', 'WB_RELEASE_PUBKEY',
                'WB_SKIP_SIGNATURE', 'WB_SERVICE_NAME', *env.keys()]
        old = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.update({
                'WB_INSTALL_DIR': str(install),
                'WB_DATA_DIR': str(install / 'data'),
                'WB_UPDATE_STATUS': str(install / 'data' / 'update-status.json'),
                'WB_SERVICE_NAME': 'workbuddy-web-does-not-exist',
                'WB_RELEASE_PUBKEY': pub if pub is not None else self.pub,
                **{k: str(v) for k, v in env.items()},
            })
            spec = importlib.util.spec_from_file_location(
                'upd_e2e', str(install / 'deploy' / 'update.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return mod

    def _run_update(self, mod, pkg: Path, sig: Path | None, version: str = '9.9.9'):
        """用假 Release 元数据驱动真实的 update_manager。

        assets 用 file:// URL —— urlopen 原生支持，因此下载与验签都是真跑的。
        """
        assets = [{
            'name': pkg.name,
            'browser_download_url': pkg.as_uri(),
        }]
        if sig is not None:
            assets.append({
                'name': sig.name,
                'browser_download_url': sig.as_uri(),
            })
        rel = {'tag_name': f'v{version}', 'assets': assets}

        # 只伪造这一步（GitHub API 不可达）；其余全部真跑
        mod.http_json = lambda url, timeout=20: rel  # type: ignore[assignment]
        # systemctl restart 在测试环境不存在，替换为空操作
        mod.run = lambda *a, **kw: (0, '')  # type: ignore[assignment]

        rep = _Rep()
        try:
            mod.update_manager(rep)
            return True, rep
        except Exception as exc:  # noqa: BLE001
            rep.log(f'更新失败：{exc}', 'error')
            return False, rep

    # ── 正常路径：装上去的必须就是验过的那一份 ──

    def test_full_update_installs_verified_package(self) -> None:
        inst = self._make_install('ok')
        pkg, sig = self._make_release('ok')
        mod = self._load(inst)
        ok, rep = self._run_update(mod, pkg, sig)
        self.assertTrue(ok, rep.text())

        # 代码真的换了
        main = (inst / 'server' / 'main.py').read_text(encoding='utf-8')
        self.assertIn("version='9.9.9'", main)
        self.assertIn('# NEW', main)
        self.assertIn('NEW', (inst / 'web' / 'out' / 'index.html').read_text(encoding='utf-8'))
        # 版本标记与文档同步
        self.assertEqual((inst / '.version').read_text(encoding='utf-8').strip(), 'v9.9.9')
        self.assertIn('9.9.9', (inst / 'CHANGELOG.md').read_text(encoding='utf-8'))
        # 验签结果落到状态（界面据此显示「已验签」）
        self.assertEqual(rep.signature['status'], 'verified')

        # deploy/ 现在**随包更新**（维护者决定，见 issue #55）：更新器本身就在
        # 这个目录里，不更新它意味着以后每次都用旧逻辑，它修过的毛病到不了用户手上
        # （#28 修过的 compose 探测就是这么丢的）。
        #
        # 安全性依据：这一步在**验签之后**才执行（`verify_release_signature` 在
        # 解压前就把整个包验过了），所以覆盖 deploy/ 与覆盖 server/ 是同一性质 ——
        # 都是「维护者签过名的内容」。真正的不变量由下面那条攻击路径用例守着：
        # 验签不过 → 整个更新中止、什么都不落盘。
        installed_deploy = (inst / 'deploy' / 'update.py').read_text(encoding='utf-8')
        self.assertIn('包内的（随包更新）', installed_deploy,
                      'deploy/ 没随包更新 —— 更新器会永远停在旧版本')
        # 覆盖前必须备份（出问题能回退）
        self.assertTrue(list((inst).glob('backup-*/deploy/update.py')),
                        '覆盖 deploy/ 前没有备份')
        self.assertIn('已同步到包内版本', rep.text())

    def test_deploy_sync_can_be_disabled(self) -> None:
        """`WB_SYNC_DEPLOY=0` 时保持本地 deploy/ 不动（手工维护者的退路）。

        关掉时必须**告警**并说清代价（更新器会一直是旧的），不能静默跳过。
        """
        inst = self._make_install('ok')
        pkg, sig = self._make_release('ok')
        mod = self._load(inst)
        with mock.patch.dict(os.environ, {'WB_SYNC_DEPLOY': '0'}):
            ok, rep = self._run_update(mod, pkg, sig)
        self.assertTrue(ok, rep.text())
        installed_deploy = (inst / 'deploy' / 'update.py').read_text(encoding='utf-8')
        self.assertNotIn('包内的（随包更新）', installed_deploy,
                         '关了同步却还是被覆盖')
        self.assertIn('check_signature', installed_deploy)
        self.assertTrue(rep.warned(), '跳过同步却没告警')
        self.assertIn('WB_SYNC_DEPLOY=0', rep.text())

    # ── 攻击路径：包被换过，必须中止且不落盘 ──

    def test_tampered_package_aborts_without_touching_install(self) -> None:
        inst = self._make_install('tampered')
        pkg, sig = self._make_release('tampered')
        data = bytearray(pkg.read_bytes())
        data[len(data) // 2] ^= 0xFF
        pkg.write_bytes(bytes(data))  # 签名不重做 = 攻击者没有私钥的处境

        mod = self._load(inst)
        ok, rep = self._run_update(mod, pkg, sig)
        self.assertFalse(ok, '被篡改的包竟然装上了')
        self.assertIn('签名校验失败', rep.text())

        # 关键：一个字节都不能动
        self.assertIn('# OLD', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))
        self.assertIn('OLD', (inst / 'web' / 'out' / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual((inst / '.version').read_text(encoding='utf-8').strip(), 'v1.0.0')
        self.assertFalse((inst / 'CHANGELOG.md').exists())

    def test_unsigned_release_aborts(self) -> None:
        """攻击者控制发布渠道后，最省事的做法就是干脆不带签名。"""
        inst = self._make_install('nosig')
        pkg, _sig = self._make_release('nosig')
        mod = self._load(inst)
        ok, rep = self._run_update(mod, pkg, None)
        self.assertFalse(ok)
        self.assertIn('没有可用的签名文件', rep.text())
        self.assertIn('# OLD', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))

    def test_placeholder_pubkey_blocks_every_update(self) -> None:
        """公钥没配就拒绝一切更新 —— 默认安全，而不是默认放行。"""
        inst = self._make_install('nokey')
        pkg, sig = self._make_release('nokey')
        mod = self._load(inst, pub='ssh-ed25519 AAAA_REPLACE_ME_WITH_YOUR_REAL_PUBLIC_KEY x')
        ok, rep = self._run_update(mod, pkg, sig)
        self.assertFalse(ok)
        self.assertIn('公钥未配置', rep.text())
        self.assertIn('# OLD', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))

    def test_bootstrap_from_old_updater_installs_new_deploy(self) -> None:
        """鸡生蛋：旧版更新器（会把 deploy/ 整体拷过去）装上新版后，
        新的验签逻辑就位 —— 这是从"没有验签"过渡到"强制验签"的唯一路径，
        必须确保它确实成立，否则所有老部署都会卡死。"""
        inst = self._make_install('boot')
        # 模拟旧版 update.py：没有 check_signature，且会同步 deploy/
        old_updater = "import shutil\nfrom pathlib import Path\n"
        (inst / 'deploy' / 'update.py').write_text(old_updater, encoding='utf-8')
        # 新包里的 deploy/update.py 就是仓库里这份（含验签）
        stage = Path(tempfile.mkdtemp(dir=self.dir)) / 'workbuddy-manager-v9.9.9'
        shutil.copytree(_ROOT / 'deploy', stage / 'deploy')
        (stage / 'server').mkdir()
        (stage / 'server' / 'main.py').write_text("version='9.9.9'\n", encoding='utf-8')
        (stage / 'web' / 'out').mkdir(parents=True)
        (stage / 'web' / 'out' / 'index.html').write_text('NEW', encoding='utf-8')
        (stage / '.version').write_text('v9.9.9\n', encoding='utf-8')
        # 旧版更新器的行为：deploy/ 会被复制过去
        shutil.copytree(stage / 'deploy', inst / 'deploy', dirs_exist_ok=True)
        new_updater = (inst / 'deploy' / 'update.py').read_text(encoding='utf-8')
        self.assertIn('check_signature', new_updater, '新更新器没有就位，过渡会断')
        self.assertIn('RELEASE_PUBKEY', new_updater)


if __name__ == '__main__':
    unittest.main()
