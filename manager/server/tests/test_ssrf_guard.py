"""SSRF 防护：Upstash 连通性测试不得探测内网与云元数据端点。

**漏的是什么**：「设置 → Upstash 连通性测试」把用户给的地址拿去**服务端发请求**，
再把响应片段回显。地址归一化只抽 host、不判安全性，于是可以指向：

    http://127.0.0.1:7863          → 本机上游客关（探测内网服务）
    169.254.169.254                → 云元数据（常能拿到实例临时凭证）
    metadata.tencentyun.com        → 腾讯云元数据
    10.0.0.5 / 192.168.x.x         → 内网其它主机

**为什么值得修**：它需要管理员权限，看起来"不是洞"。但管理员权限往往是通过
别的漏洞拿到的（本仓库历史上就发生过路径穿越 + 提权后门导致真实入侵），
而「拿到低权限后顺着内网横向移动」正是入侵的第二步。纵深防御应当在能拦的
地方就拦。

**修法**：不做域名白名单（会误伤自建/第三方 Upstash 兼容服务），而是
**排除回环、私有、链路本地、保留段与已知元数据域名**；公网主机照常放行。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import wb2api  # noqa: E402


class InternalHostRejectionTest(unittest.TestCase):
    """地址判定：内网/元数据一律拒绝，公网一律放行。"""

    BLOCKED = (
        '127.0.0.1', '127.1.2.3', '10.0.0.5', '192.168.1.1', '172.16.0.1',
        '169.254.169.254',          # 云元数据（AWS/腾讯云等通用）
        '0.0.0.0', '::1', 'fc00::1', 'fe80::1',
        'metadata.tencentyun.com',  # 腾讯云内网元数据
        'metadata.google.internal',
        'something.internal', 'db.local',
    )
    ALLOWED = (
        'us1-xxx.upstash.io', 'upstash.io', 'my-redis.example.com',
        '1.2.3.4', '2606:4700::1111',   # 公网 IP（IPv6 也要放行）
    )

    def test_blocked_hosts(self) -> None:
        for h in self.BLOCKED:
            self.assertIsNotNone(
                wb2api._reject_internal_host(h),
                f'{h} 应被拒绝 —— 它能被用来探测内网或云元数据',
            )

    def test_allowed_hosts(self) -> None:
        for h in self.ALLOWED:
            self.assertIsNone(
                wb2api._reject_internal_host(h),
                f'{h} 是合法目标，不应被拦（否则自建/第三方 Redis 会被误伤）',
            )

    def test_empty_host_rejected(self) -> None:
        self.assertIsNotNone(wb2api._reject_internal_host(''))
        self.assertIsNotNone(wb2api._reject_internal_host('   '))


class TestUpstashEndpointTest(unittest.TestCase):
    """端到端：test_upstash 对内网地址必须在**发请求之前**就返回失败。

    关键断言是「没有发出任何 HTTP 请求」——只检查返回值不够：返回失败但请求
    已经打出去了，SSRF 已经成立（对方服务已经收到探测）。
    """

    def _call(self, url: str):
        sent: list[str] = []

        class _Client:
            def __init__(self, *a, **k) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, u, **kw):
                sent.append(u)
                raise AssertionError('不应发出请求')

        with mock.patch.object(config, 'http_client', _Client):
            ok, msg = asyncio.run(wb2api.test_upstash(url, 'tok'))
        return ok, msg, sent

    def test_internal_targets_never_requested(self) -> None:
        for url in ('http://127.0.0.1:7863/ping', '169.254.169.254',
                    'http://metadata.tencentyun.com', 'redis://u:p@10.0.0.5:6379'):
            ok, msg, sent = self._call(url)
            self.assertFalse(ok, f'{url} 应被拒绝')
            self.assertEqual(sent, [], f'{url} 竟然发出了请求：{sent}')
            self.assertIn('不允许探测', msg)

    def test_public_target_is_actually_probed(self) -> None:
        """公网地址要照常探测 —— 修复不能把正常功能一起关掉。"""
        sent: list[str] = []

        class _Resp:
            status_code = 200
            text = 'PONG'

        class _Client:
            def __init__(self, *a, **k) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, u, **kw):
                sent.append(u)
                return _Resp()

        with mock.patch.object(config, 'http_client', _Client):
            ok, msg = asyncio.run(wb2api.test_upstash('https://us1-abc.upstash.io', 'tok'))
        self.assertTrue(ok, msg)
        self.assertEqual(len(sent), 1, '公网地址应正常探测')
        self.assertIn('upstash.io/ping', sent[0])


if __name__ == '__main__':
    unittest.main()


class DbPermissionTest(unittest.TestCase):
    """数据库文件必须收紧到 0600（含 WAL/SHM 伴生文件）。

    库里存着 **API 密钥哈希与前缀、全部请求日志（来源 IP / UA）、审计日志**。
    SQLite 默认建出的文件是 0644，同主机的其他用户/进程可读——密钥前缀能用
    于针对性爆破，日志暴露调用方与内部拓扑。WAL 模式的伴生文件同样含数据。

    在 Windows 上 chmod 只影响只读位、无法验证实际模式，因此这里断言
    「以 0600 调用了 chmod，且三个文件都被覆盖」——那是 Linux 上生效的依据。
    """

    def test_chmod_0600_called_for_all_db_files(self) -> None:
        import os as _os
        import tempfile
        from unittest import mock
        from pathlib import Path as _P

        tmp = tempfile.mkdtemp()
        from server import config as _cfg
        old_dir, old_db = _cfg.DATA_DIR, _cfg.DB_PATH
        _cfg.DATA_DIR = _P(tmp)
        _cfg.DB_PATH = _P(tmp) / 'perm.db'
        from server import db as _db
        old_conn = _db._conn
        _db._conn = None
        try:
            calls: list[tuple[str, int]] = []
            real_chmod = _os.chmod

            def spy(path, mode):
                calls.append((str(path), mode))
                return real_chmod(path, mode)

            with mock.patch.object(_os, 'chmod', spy):
                _db.connect()
        finally:
            _db._conn = old_conn
            _cfg.DATA_DIR, _cfg.DB_PATH = old_dir, old_db

        modes = {m for _, m in calls}
        self.assertTrue(calls, '建库时没有收紧权限')
        self.assertEqual(modes, {0o600}, f'权限值应为 0600，实际 {[oct(m) for m in modes]}')
        names = ' '.join(p for p, _ in calls)
        for suffix in ('-wal', '-shm'):
            self.assertIn(suffix, names, f'{suffix} 伴生文件没有被收紧')
