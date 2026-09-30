"""密钥输入校验的回归测试（发版前对抗性测试发现的三处）。

三处都不是 5xx，但都让用户**看不懂发生了什么**——比直接报错更费解：

  1. **CIDR 写错 = 密钥永久不可用**。`ip_matches` 对非法 CIDR 一律返回 False
     （fail-closed，方向本来是对的），所以白名单里只要有一个写错的条目，
     这把密钥对**所有**来源都拒绝。而错误只说「不在密钥白名单内」，
     用户根本看不出是自己把 CIDR 写错了（实测确认过这条错误信息）。
  2. **名称只填空格**能建出来。列表里就是一行「没有名字」的密钥，
     管理员认不出它是干什么的，也不知道是自己误操作。
  3. **对不存在的 id 重置用量返回 200**（静默成功）。前端提示「已重置」，
     而实际什么都没发生——密钥可能已被别人删掉。

前两条改为**写入前校验并明确说明该怎么写**，第三条改为 404。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402


class KeyInputValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'k.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        # 直接用已有登录态太重，这里绕过依赖校验，直接测路由函数的校验分支
        from server.main import app
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    # ── 直接测校验函数（不依赖登录态，稳定）────────────────────

    def test_bad_cidr_rejected_with_hint(self) -> None:
        """非法 CIDR 必须在写入前被拒，且说明会有什么后果。"""
        from fastapi import HTTPException

        from server.routers.keys import _check_ip_allowlist
        for bad in ['not-a-cidr', '999.999.999.999/99', '10.0.0.0/999', 'abc/8']:
            with self.assertRaises(HTTPException, msg=bad) as ctx:
                _check_ip_allowlist([bad])
            self.assertEqual(ctx.exception.status_code, 400)
            detail = str(ctx.exception.detail)
            self.assertIn(bad, detail, '要指出是哪一条写错了')
            self.assertIn('CIDR', detail, '要说明正确写法')

    def test_good_cidr_accepted(self) -> None:
        from server.routers.keys import _check_ip_allowlist
        for good in ['10.0.0.0/8', '1.2.3.4', '192.168.1.0/24', '::1', '2001:db8::/32']:
            _check_ip_allowlist([good])       # 不抛即通过
        _check_ip_allowlist([])
        _check_ip_allowlist(None)
        _check_ip_allowlist(['', '  '])       # 空项交给 keysvc 过滤，不算错
        _check_ip_allowlist(['10.0.0.0/8', '1.2.3.4'])   # 多项混合

    def test_blank_name_rejected(self) -> None:
        from fastapi import HTTPException

        from server.routers.keys import _check_name
        for bad in ['   ', '\t', '\n', ' \t \n ']:
            with self.assertRaises(HTTPException, msg=repr(bad)) as ctx:
                _check_name(bad)
            self.assertEqual(ctx.exception.status_code, 400)
        for good in ['a', ' 名称 ', 'x']:
            _check_name(good)
        _check_name(None)      # PATCH 不带 name 时不校验

    def test_bad_cidr_would_lock_key_out(self) -> None:
        """把「为什么必须拦」固化成测试：坏 CIDR 让密钥对所有 IP 都拒绝。

        这是本组校验存在的理由，写下来免得以后有人觉得多余而删掉。
        """
        from server.iputil import ip_matches
        for ip in ['9.9.9.9', '1.2.3.4', '127.0.0.1', '2001:db8::1']:
            self.assertFalse(ip_matches(ip, 'not-a-cidr'),
                             '非法 CIDR 必须匹配不上任何 IP（fail-closed）')

    def test_reset_usage_returns_bool(self) -> None:
        """reset_usage 要能区分「重置了」与「没有这把密钥」。"""
        created = keysvc.create_key('t')
        self.assertTrue(keysvc.reset_usage(created['id']))
        self.assertFalse(keysvc.reset_usage(999999), '不存在的 id 应返回 False')

    def test_validation_does_not_block_legit_edit(self) -> None:
        """合法编辑不能被新校验误伤（尤其 PATCH 只发部分字段时）。"""
        from server.routers.keys import _check_ip_allowlist, _check_name
        # 只改名字，不带 ip_allowlist → 不应触发 CIDR 校验
        _check_name('新名字')
        # 只改白名单，合法
        _check_ip_allowlist(['10.0.0.0/8'])


if __name__ == '__main__':
    unittest.main()
