"""静态文件路径穿越的回归测试（严重安全漏洞）。

背景：静态前端由 `main.py` 的兜底路由 `spa()` 提供。该路由原先把请求路径
直接拼到 `STATIC_DIR` 上而不做越界校验。ASGI 会先对 %2f 解码，于是
`/..%2f..%2fdata%2fusers.json` 经 `Path / str` 拼接后指向部署目录**之外**，
造成任意文件读取。实测可读到：

  - `users.json`：内含签发会话 Cookie 的 `secret`，可据此**伪造 admin 会话**
  - `.env`：`WB_SECRET` 等
  - 上游 `config.json`：`api_key`（可直接盗用额度）
  - `auths/*.json`：腾讯账号 `accessToken`（等于接管账号）

修复要点是「resolve() 归一化后强制仍在 STATIC_DIR 内」。这些用例锁定该行为：
既要拦住各类穿越变体，也不能误伤正常的静态资源与目录索引。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import main as server_main  # noqa: E402


class SafeStaticPathTest(unittest.TestCase):
    """直接测路径解析函数——它是唯一的越界防线。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.static = self.root / 'web' / 'out'
        (self.static / '_next').mkdir(parents=True)
        (self.static / 'index.html').write_text('<html>ok</html>', encoding='utf-8')
        (self.static / 'logs').mkdir()
        (self.static / 'logs' / 'index.html').write_text('<html>logs</html>', encoding='utf-8')
        # 部署目录之外的"机密"：正是漏洞能读到的那些
        (self.root / 'data').mkdir()
        (self.root / 'data' / 'users.json').write_text(
            '{"secret":"LEAKED_SESSION_SECRET"}', encoding='utf-8')
        (self.root / '.env').write_text('WB_SECRET=LEAKED', encoding='utf-8')
        self._orig = server_main.config.STATIC_DIR
        server_main.config.STATIC_DIR = self.static

    def tearDown(self) -> None:
        server_main.config.STATIC_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _call(self, p: str):
        return server_main._safe_static_path(p)

    # ── 必须拦住的穿越变体 ──────────────────────────────
    def test_plain_dotdot_is_blocked(self) -> None:
        self.assertIsNone(self._call('../data/users.json'))
        self.assertIsNone(self._call('../../data/users.json'))

    def test_decoded_slash_traversal_blocked(self) -> None:
        """ASGI 会把 %2f 解成 /，函数收到的就是这种形态——必须拦住。"""
        self.assertIsNone(self._call('../data/users.json'))
        self.assertIsNone(self._call('../../root/.env'))

    def test_absolute_path_is_blocked(self) -> None:
        """绝对路径会让 Path 丢掉左操作数，必须剥离后再校验。"""
        self.assertIsNone(self._call('/etc/passwd'))
        self.assertIsNone(self._call(str(self.root / '.env')))

    def test_dotdot_wrapped_in_segments_is_blocked(self) -> None:
        for p in ('logs/../../data/users.json',
                  'a/b/../../../data/users.json',
                  './../data/users.json'):
            self.assertIsNone(self._call(p), p)

    def test_empty_and_root_like_inputs(self) -> None:
        self.assertIsNone(self._call(''))
        # 目录本身不是文件
        self.assertIsNone(self._call('.'))

    def test_nonexistent_file_returns_none(self) -> None:
        self.assertIsNone(self._call('no-such-file.html'))

    # ── 正常访问不受影响 ────────────────────────────────
    def test_normal_files_still_served(self) -> None:
        self.assertIsNotNone(self._call('index.html'))
        self.assertIsNotNone(self._call('logs/index.html'))

    def test_nested_asset_served(self) -> None:
        (self.static / '_next' / 'a.js').write_text('x', encoding='utf-8')
        self.assertIsNotNone(self._call('_next/a.js'))


class SpaRouteTest(unittest.TestCase):
    """走真实路由：确认穿越请求得到 404 且**不返回文件内容**。"""

    @classmethod
    def setUpClass(cls) -> None:
        import os
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        static = root / 'web' / 'out'
        (static / '_next').mkdir(parents=True)
        (static / 'index.html').write_text('<html>home</html>', encoding='utf-8')
        (root / 'data').mkdir()
        (root / 'data' / 'users.json').write_text(
            '{"secret":"LEAKED_SESSION_SECRET"}', encoding='utf-8')
        cls._orig_static = server_main.config.STATIC_DIR
        cls._orig_db = None
        server_main.config.STATIC_DIR = static
        # 让模块级 mount 用的是这个目录：重新导入不现实，直接挂载测试用实例
        from fastapi import FastAPI
        from fastapi.responses import FileResponse, JSONResponse
        cls.app = FastAPI()

        @cls.app.get('/{full_path:path}')
        def spa(full_path: str):  # 与生产实现同源
            target = server_main._safe_static_path(full_path)
            if target is not None:
                return FileResponse(target)
            cand = server_main._safe_static_path(f'{full_path}/index.html')
            if cand is not None:
                return FileResponse(cand)
            return JSONResponse({'error': 'not found'}, status_code=404)

        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        server_main.config.STATIC_DIR = cls._orig_static
        cls._tmp.cleanup()

    def test_traversal_request_returns_404_without_content(self) -> None:
        for path in ('/..%2f..%2fdata%2fusers.json',
                     '/%2e%2e/%2e%2e/data/users.json',
                     '/../data/users.json'):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 404, path)
            self.assertNotIn('LEAKED_SESSION_SECRET', r.text, f'{path} 泄露了内容')

    def test_index_still_served(self) -> None:
        self.assertEqual(self.client.get('/index.html').status_code, 200)


if __name__ == '__main__':
    unittest.main()

class SafeAuthFilenameTest(unittest.TestCase):
    """账号文件名校验：穿越只是其一，越权读同目录其他文件也要拦住。

    `_safe_file` 是 `accounts` 一组路由（checkin/credits/test/refresh/delete）
    共用的唯一入口，一旦被绕过就等于任意文件读取。
    """

    def setUp(self) -> None:
        import tempfile
        from server.services import wb2api
        self._wb2api = wb2api
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = wb2api.config.AUTH_DIR
        wb2api.config.AUTH_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._wb2api.config.AUTH_DIR = self._orig
        self._tmp.cleanup()

    def test_normal_account_file_passes(self) -> None:
        p = self._wb2api._safe_file('workbuddy-89374120.json')
        self.assertTrue(str(p).endswith('workbuddy-89374120.json'))

    def test_traversal_blocked(self) -> None:
        BS = chr(92)
        for bad in ('../users.json', '..' + BS + 'users.json',
                    'a/../../b.json', '/etc/passwd',
                    '..%2f..%2fdata%2fusers.json'):
            with self.assertRaises(ValueError, msg=bad):
                self._wb2api._safe_file(bad)

    def test_other_files_in_dir_blocked(self) -> None:
        """同目录里**不是账号文件**的一律拒绝（隐藏文件、非 workbuddy 前缀、非 .json）。

        形态是 `workbuddy*.json`（**与上游 `auth.AuthFileGlob` 一致**），不是更窄的
        `workbuddy-*.json`。上游为这个问题留过注释：它曾用窄模式，导致
        `workbuddy_new.json` 被网关加载却被运维工具跳过、两边口径对不上。
        我们此前正是窄模式 —— 那种账号在上游池里能被选中、面板却看不到。

        放宽不会引入越权：前缀固定为 `workbuddy`、后缀必须是 `.json`、
        字符类里不含路径分隔符与点号，所以能命中的只可能是 auths 目录下的账号文件。
        """
        for bad in ('.hidden.json', 'users.json', 'config.json',
                    'workbuddy-1.txt',            # 结尾不是 .json
                    'notes.json',
                    'workbuddy.json.bak',         # 结尾不是 .json
                    '.workbuddy-x.json'):         # 点开头（隐藏文件）
            with self.assertRaises(ValueError, msg=bad):
                self._wb2api._safe_file(bad)

    def test_upstream_compatible_names_accepted(self) -> None:
        """与上游同宽的形态要能通过 —— 否则面板列出了却操作不了（checkin/delete 都会失败）。"""
        for good in ('workbuddy-89374120.json',   # 网关自己写的形态
                     'workbuddy_new.json',        # 不带连字符（上游特意兼容的）
                     'workbuddy_x-1.json'):
            self.assertTrue(self._wb2api._safe_file(good))

    def test_null_byte_blocked(self) -> None:
        with self.assertRaises(ValueError):
            self._wb2api._safe_file('workbuddy-1' + chr(0) + '.json')


if __name__ == '__main__':
    unittest.main()
