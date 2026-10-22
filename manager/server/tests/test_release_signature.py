"""发布包签名校验的回归测试（供应链防护）。

背景：一键更新以 **root** 把 Release 产物直接落盘并重启服务。而「能发
Release」的门槛比想象中低——能合并 PR 的协作者、或被钓鱼的维护者账号都能
发版。于是「合并恶意 PR → 发版 → 用户点更新」是一条完整链路，一次得手就是
**所有部署同时沦陷**（SolarWinds / event-stream 形态）。

签名把「能改代码」与「能发布可信产物」分开：私钥离线保管、不进仓库也不进
CI（放进 CI 的话恶意 PR 可以改 workflow 偷走）。攻击者拿到合并权限后仍能
发版，但**签不出名，所有用户的更新会中止**。

测试策略：本文件**不 mock 任何标准库**。验签是信任链本身，必须真实调用
ssh-keygen；而下载走 `file://` URL（urlopen 原生支持），因此连网络层也不
需要替身。—— 早先的版本用 `mock.patch.object(urllib.request, 'urlopen')`
去替换**共享的** stdlib 模块，结果伪造的响应在用例之间互相泄漏，出现了
时灵时不灵的失败。那种脆弱性本身就是风险：信任链的测试必须只有一个答案。

锁定以下不变量：
  1. 有效签名 → 放行
  2. 包被篡改 → 拒绝（这是攻击者的主要目标）
  3. 签名被篡改 → 拒绝
  4. 没有签名文件 → 拒绝（不能"没签就当通过"）
  5. 公钥不匹配 → 拒绝
  6. 公钥未配置（占位值）→ 拒绝并给出指引（默认安全）
  7. WB_SKIP_SIGNATURE=1 → 跳过但**必须告警**（紧急逃生门）
  8. deploy/ 不被包内覆盖（验证逻辑本身不可被替换）
  9. 验签发生在解压之前（先确认"包是你签的"，再谈包里的内容）
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]

PLACEHOLDER_PUBKEY = 'ssh-ed25519 AAAA_REPLACE_ME_WITH_YOUR_REAL_PUBLIC_KEY release-signing'


def _load_update_mod(pubkey: str | None = None, **env):
    """加载 deploy/update.py，并按需覆盖公钥与开关。

    模块常量在导入时从环境读取，所以这里「设环境 → 导入 → 恢复环境」；
    导入后常量已固化，互不影响。每次调用返回全新模块对象，测试之间没有
    共享可变状态。
    """
    keys = ['WB_RELEASE_PUBKEY', 'WB_SKIP_SIGNATURE', *env.keys()]
    old = {k: os.environ.get(k) for k in keys}
    try:
        if pubkey is not None:
            os.environ['WB_RELEASE_PUBKEY'] = pubkey
        os.environ.update({k: str(v) for k, v in env.items()})
        spec = importlib.util.spec_from_file_location('upd', str(_ROOT / 'deploy' / 'update.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


def _ssh_keygen_available() -> bool:
    return shutil.which('ssh-keygen') is not None


class _Rep:
    """收集日志，便于断言「是否告警 / 报错文案」。"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.signature: dict | None = None

    def log(self, msg: str, level: str = 'info') -> None:
        self.lines.append((level, msg))

    def set_signature(self, status: str, detail: str = '') -> None:
        self.signature = {'status': status, 'detail': detail}

    def warned(self) -> bool:
        return any(lvl == 'warn' for lvl, _ in self.lines)

    def text(self) -> str:
        return '\n'.join(m for _, m in self.lines)


@unittest.skipUnless(_ssh_keygen_available(), '需要 ssh-keygen（OpenSSH 8.0+）')
class SignatureVerifyTest(unittest.TestCase):
    """真实调用 ssh-keygen 做签名/验签，不用 mock —— 这是信任链本身。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.key = cls.dir / 'signing'
        # 生成一次性测试密钥（-N '' 无口令，仅测试用）
        subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-N', '', '-C', 'test', '-f', str(cls.key)],
            check=True, capture_output=True,
        )
        cls.pub = cls.key.with_suffix('.pub').read_text(encoding='utf-8').strip()
        # 造一个最小发布包（结构无关紧要，只验证签名覆盖）
        stage = cls.dir / 'stage' / 'workbuddy-manager-9.9.9' / 'server'
        stage.mkdir(parents=True)
        (stage / 'main.py').write_text('x = 1', encoding='utf-8')
        cls.pkg = cls.dir / 'pkg.tar.gz'
        subprocess.run(
            ['tar', 'czf', str(cls.pkg), '-C', str(cls.dir / 'stage'), 'workbuddy-manager-9.9.9'],
            check=True, capture_output=True,
        )
        # 签名（不经过 shell 管道：管道曾被环境改写，导致签名损坏）
        subprocess.run(
            ['ssh-keygen', '-Y', 'sign', '-f', str(cls.key), '-n', 'file', str(cls.pkg)],
            check=True, capture_output=True,
        )
        cls.sig = Path(str(cls.pkg) + '.sig')
        assert cls.sig.is_file(), '签名文件未生成'

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    # ── 辅助：把签名内容放到指定文件，走**纯函数**路径 ──

    def _check(self, pkg: Path, sig_bytes: bytes | None, **env):
        """把包与签名复制到本用例的私有目录，再调用 check_signature。

        刻意用私有副本：验签代码会在包旁边写 allowed_signers、下载 .sig，
        若直接操作类级夹具，一个用例就会污染另一个（这正是之前 mock 泄漏
        的同款问题）。返回 (是否通过, 日志, 异常文本)。
        """
        mod = _load_update_mod(pubkey=env.pop('pub', self.pub), **env)
        work = Path(tempfile.mkdtemp(dir=self.dir))
        pkg2 = work / pkg.name
        shutil.copyfile(pkg, pkg2)
        sig_path = Path(str(pkg2) + '.sig')
        if sig_bytes is not None:
            sig_path.write_bytes(sig_bytes)
        # 不传 sig_bytes = 该 Release 压根没有签名资产（文件不存在）
        rep = _Rep()
        try:
            mod.check_signature(pkg2, sig_path, rep)
            return True, rep, ''
        except RuntimeError as exc:
            return False, rep, str(exc)

    # ── 正常路径 ──

    def test_valid_signature_passes(self) -> None:
        ok, rep, err = self._check(self.pkg, self.sig.read_bytes())
        self.assertTrue(ok, rep.text() + err)
        self.assertIn('签名校验通过', rep.text())
        # 界面据此显示「已验签」——结果必须落到状态里，而不只是日志
        self.assertEqual(rep.signature['status'], 'verified')

    # ── 攻击者的主要目标：替换包 ──

    def test_tampered_package_rejected(self) -> None:
        """包内容改一个字节就必须拒绝 —— 这正是攻击者要做的。"""
        tampered = self.dir / 'tampered.tar.gz'
        data = bytearray(self.pkg.read_bytes())
        data[len(data) // 2] ^= 0xFF
        tampered.write_bytes(bytes(data))
        ok, rep, err = self._check(tampered, self.sig.read_bytes())
        self.assertFalse(ok, '被篡改的包竟然通过了验签')
        self.assertIn('签名校验失败', rep.text() + err)

    def test_tampered_signature_rejected(self) -> None:
        bad = bytearray(self.sig.read_bytes())
        bad[len(bad) // 2] ^= 0xFF
        ok, rep, err = self._check(self.pkg, bytes(bad))
        self.assertFalse(ok)
        self.assertIn('签名校验失败', rep.text() + err)

    def test_missing_signature_rejected(self) -> None:
        """不能把「没签名」当成「通过」——否则攻击者只要不传 .sig 就绕过了。"""
        ok, rep, err = self._check(self.pkg, None)
        self.assertFalse(ok)
        self.assertIn('没有可用的签名文件', rep.text() + err)

    def test_wrong_public_key_rejected(self) -> None:
        """换成别人的公钥：签名对不上，必须拒绝。"""
        other = self.dir / 'other'
        subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-C', 'x', '-f', str(other)],
                       check=True, capture_output=True)
        pub2 = other.with_suffix('.pub').read_text(encoding='utf-8').strip()
        ok, rep, err = self._check(self.pkg, self.sig.read_bytes(), pub=pub2)
        self.assertFalse(ok)
        self.assertIn('签名校验失败', rep.text() + err)

    # ── 默认安全：没配公钥时不放行 ──

    def test_placeholder_key_refuses_update(self) -> None:
        """公钥仍是占位值时拒绝自动更新（默认安全，避免"忘配公钥=不验签"）。"""
        ok, rep, err = self._check(self.pkg, self.sig.read_bytes(), pub=PLACEHOLDER_PUBKEY)
        self.assertFalse(ok)
        self.assertIn('公钥未配置', rep.text() + err)

    # ── 逃生门必须留痕 ──

    def test_skip_switch_bypasses_but_warns(self) -> None:
        ok, rep, err = self._check(self.pkg, None, WB_SKIP_SIGNATURE='1')
        self.assertTrue(ok, '跳过开关应放行')
        self.assertTrue(rep.warned(), '跳过验签必须留下告警')
        self.assertIn('跳过签名校验', rep.text())
        # 绕过必须落到状态里，让界面能显示醒目告警
        self.assertEqual(rep.signature['status'], 'skipped')

    def test_normal_run_does_not_warn(self) -> None:
        ok, rep, err = self._check(self.pkg, self.sig.read_bytes())
        self.assertTrue(ok)
        self.assertFalse(rep.warned())

    # ── 下载 → 验签的完整接线（用 file:// URL，不 mock）──

    def test_full_flow_downloads_then_verifies(self) -> None:
        """verify_release_signature 真的会把 .sig 下载到包旁边再验。"""
        mod = _load_update_mod(pubkey=self.pub)
        work = Path(tempfile.mkdtemp(dir=self.dir))
        pkg = work / 'flow.tar.gz'
        shutil.copyfile(self.pkg, pkg)
        # 造一个"远端"签名文件，用 file:// 当作下载源
        remote_sig = work / 'remote.sig'
        remote_sig.write_bytes(self.sig.read_bytes())
        rep = _Rep()
        mod.verify_release_signature(pkg, remote_sig.as_uri(), rep)
        self.assertIn('下载签名文件', rep.text())
        self.assertIn('签名校验通过', rep.text())
        self.assertIn('校验发布包签名', rep.text())

    def test_release_without_sig_asset_rejected(self) -> None:
        """Release 里没有 .sig 资产（sig_url 为空）→ 拒绝，且不能假装通过。"""
        mod = _load_update_mod(pubkey=self.pub)
        work = Path(tempfile.mkdtemp(dir=self.dir))
        pkg = work / 'nosig.tar.gz'
        shutil.copyfile(self.pkg, pkg)
        rep = _Rep()
        with self.assertRaises(RuntimeError) as ctx:
            mod.verify_release_signature(pkg, '', rep)
        self.assertIn('没有可用的签名文件', str(ctx.exception))
        self.assertFalse(Path(str(pkg) + '.sig').exists(), '不应凭空造出签名文件')

    def test_sig_download_failure_rejected(self) -> None:
        """签名下载失败（404 / 网络故障）不能降级为"跳过"。"""
        mod = _load_update_mod(pubkey=self.pub)
        work = Path(tempfile.mkdtemp(dir=self.dir))
        pkg = work / 'dlfail.tar.gz'
        shutil.copyfile(self.pkg, pkg)
        missing = (work / 'does-not-exist.sig').as_uri()
        rep = _Rep()
        with self.assertRaises(RuntimeError) as ctx:
            mod.verify_release_signature(pkg, missing, rep)
        self.assertIn('没有可用的签名文件', str(ctx.exception))


class UpdateManagerWiringTest(unittest.TestCase):
    """验签必须**接在**更新主流程里，且顺序正确。

    纯函数的单测通过 ≠ 主流程真的会用它。历史上就吃过这个亏：
    校验函数写好了、导出没接上，等于没写。这里直接把接线与顺序钉死。
    """

    def setUp(self) -> None:
        self.src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')

    def _body_of(self, func: str) -> str:
        m = re.search(rf'^def {func}\(.*?(?=^def |\Z)', self.src, re.S | re.M)
        self.assertIsNotNone(m, f'找不到函数 {func}')
        return m.group(0)

    def test_update_manager_calls_verify(self) -> None:
        body = self._body_of('update_manager')
        self.assertIn('verify_release_signature(', body,
                      '更新主流程没有调用验签 —— 等于没有防护')

    def test_verify_happens_before_extract(self) -> None:
        """先确认「这个包是你签的」，再谈包里的内容。"""
        body = self._body_of('update_manager')
        i_verify = body.find('verify_release_signature(')
        i_extract = body.find('_safe_extract(')
        self.assertGreaterEqual(i_verify, 0)
        self.assertGreaterEqual(i_extract, 0)
        self.assertLess(i_verify, i_extract, '验签必须在解压之前')


class UpdateDeployNotReplacedTest(unittest.TestCase):
    """deploy/ 不能被包内容自动覆盖 —— 它是整条信任链的锚点。

    若允许覆盖，攻击者只要在某次更新里带一个改过的 update.py，
    之后所有更新就都不验签了（一次得手、永久失效）。
    """

    def test_update_py_not_copied_from_package(self) -> None:
        src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')
        # 不应再把包内 deploy/ 整体拷进安装目录
        self.assertNotIn(
            "shutil.copytree(new_root / 'deploy', INSTALL_DIR / 'deploy', dirs_exist_ok=True)",
            src,
        )
        # 应保留提示而非静默同步
        self.assertIn('已跳过同步', src)
        self.assertIn('验签', src)


class ReleaseWorkflowTest(unittest.TestCase):
    """发布流程要上传签名文件，否则更新器取不到 .sig 会一律拒绝。"""

    def test_workflow_mentions_sig_asset(self) -> None:
        wf = (_ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
        self.assertIn('.sig', wf, '发布流程未涉及签名文件')


class PubkeyConsistencyTest(unittest.TestCase):
    """信任锚只有一份，但被写在了两个地方：

      * `deploy/update.py` 的 RELEASE_PUBKEY —— 更新器**实际使用**的
      * `deploy/release-signing-key.pub`   —— CI 校验与人工核对时读的

    两者一旦不一致，就会出现最坏的组合：CI 说「签名与仓库公钥匹配」，
    用户侧却全部拒绝安装（或反过来，CI 报假警）。密钥轮换时最容易漏改
    其中一个，因此把「必须完全一致」钉死在测试里。
    """

    def _norm(self, line: str) -> str:
        # keytype + base64 是信任的全部；注释（第三段）不参与校验
        parts = line.split()
        self.assertGreaterEqual(len(parts), 2, f'不是合法的公钥行：{line!r}')
        return f'{parts[0]} {parts[1]}'

    def test_embedded_pubkey_matches_pub_file(self) -> None:
        src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')
        # 取出内嵌公钥（可能被拆成多行字符串拼接）
        m = re.search(r'RELEASE_PUBKEY = os\.environ\.get\(.*?\) or \(\s*((?:\s*\'[^\']*\'\s*)+)\)',
                      src, re.S)
        if m is None:
            m = re.search(r'RELEASE_PUBKEY = os\.environ\.get\(.*?\) or (\'[^\']*\')', src, re.S)
        self.assertIsNotNone(m, '没解析出 RELEASE_PUBKEY，测试需要跟着改')
        chunks = re.findall(r"'([^']*)'", m.group(1) if m.lastindex else m.group(0))
        embedded = ''.join(chunks).strip()

        pub_file = (_ROOT / 'deploy' / 'release-signing-key.pub').read_text(encoding='utf-8').strip()
        # .pub 可能有多行（轮换期），取第一行有效公钥
        file_key = next((ln for ln in pub_file.splitlines()
                         if ln.strip().startswith(('ssh-ed25519', 'ssh-rsa', 'ecdsa-'))), '')
        self.assertTrue(file_key, 'deploy/release-signing-key.pub 里没有公钥行')
        self.assertEqual(
            self._norm(embedded), self._norm(file_key),
            '内嵌公钥与 deploy/release-signing-key.pub 不一致 —— 轮换时漏改了其中一个',
        )

    def test_verify_script_signer_id_matches_updater(self) -> None:
        """verify-release.sh 的 -I 身份必须与更新器一致，否则手动验签会误报失败。"""
        src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')
        m = re.search(r"RELEASE_SIGNER_ID = os\.environ\.get\('WB_RELEASE_SIGNER'\) or '([^']+)'", src)
        self.assertIsNotNone(m)
        signer_id = m.group(1)
        script = (_ROOT / 'deploy' / 'verify-release.sh').read_text(encoding='utf-8')
        self.assertIn(f'SIGNER_ID="{signer_id}"', script,
                      'verify-release.sh 的签名者身份与更新器不一致')


class VerifyReleaseScriptTest(unittest.TestCase):
    """手动安装的独立校验脚本必须存在，且默认拒绝、不静默放行。"""

    def test_script_exists_and_is_strict(self) -> None:
        script = (_ROOT / 'deploy' / 'verify-release.sh').read_text(encoding='utf-8')
        self.assertIn('ssh-keygen -Y verify', script)
        self.assertIn('exit 3', script, '缺签名文件必须是非 0 退出')
        self.assertIn('请勿解压安装', script)
        # 不允许出现"验签失败但继续"的分支
        self.assertNotIn('exit 0 # force', script)


if __name__ == '__main__':
    unittest.main()
