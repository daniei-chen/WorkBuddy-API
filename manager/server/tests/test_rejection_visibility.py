"""issue #18 回归：拒绝原因必须能**穿过**客户端的错误折叠层。

现场：用户用 DeepSeek Harness（DSH）连本网关，调国际版模型时界面显示
「本轮运行失败 API 密钥无效」，于是反复重建密钥（在国内版页面里建的每一把
都还是国内版专用），永远好不了。

DSH 的两处代码决定了这件事（`deepseek-ai/deepseek-harness`）：
  · `packages/llm/llm-pi-ai/src/stream.ts`:
        if (/\b(?:401|403)\b/.test(message)) return 'AUTH'
  · `packages/client/ui-chat/src/client/chat/MessageItem.tsx`:
        return code === 'AUTH' ? t('message.failure.auth') : message
即 401/403 时真实报文被**整段丢弃**，界面只显示本地化的「API 密钥无效」。

所以本测试的判据不是「状态码等于几」，而是**照着 DSH 的规则折叠一遍之后，
用户还能不能看到真正的原因**。这样即使将来 DSH 改了规则（比如不再折叠 400），
测试要断言的仍是我们真正在意的性质：配置类错误不能被说成认证失败。

同时钉住两类语义边界：
  · 凭据真的不可用（停用/过期）→ 403，显示成「密钥无效」是贴切的；
  · 版本/白名单不匹配 → **不是**认证问题，必须避开 401/403。
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402

# DSH 折叠规则的原样复刻（见模块 docstring 的出处）
AUTH_STATUSES = (401, 403)
AUTH_LOCALIZED = 'API 密钥无效'


def dsh_display(status: int, message: str) -> str:
    """把 (状态码, 报文) 折叠成 DSH 界面上实际显示的那一行。"""
    if status in AUTH_STATUSES:
        return AUTH_LOCALIZED
    return message


class RejectionVisibilityTest(unittest.TestCase):
    """纯判定层：validate 返回的状态码该避开客户端的认证折叠。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'v.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _row(self, **kw) -> dict:
        return keysvc.resolve(keysvc.create_key('t', **kw)['key'])

    def _row_and_token(self, **kw) -> tuple[dict, str]:
        """create_key 只在创建时返回明文，之后只剩哈希——所以要留下明文。"""
        token = keysvc.create_key('t', **kw)['key']
        return keysvc.resolve(token), token

    def test_config_errors_are_visible_to_client(self) -> None:
        """版本不匹配 / 白名单不匹配 / 缺 model：用户必须看到真实原因。"""
        cases = [
            ('版本不匹配', self._row(realm='global'), 'glm-5.2'),
            ('版本不匹配（反方向）', self._row(realm='cn'), 'global:gpt-5.6-sol'),
            ('限定版本但缺 model', self._row(realm='global'), ''),
            ('模型白名单外', self._row(models=['only-this']), 'other-model'),
            ('白名单模型但缺 model', self._row(models=['only-this']), ''),
        ]
        for label, row, model in cases:
            with self.subTest(label):
                reason = keysvc.validate(row, '1.2.3.4', model)
                self.assertIsNotNone(reason, f'{label} 应被拒绝')
                status = getattr(reason, 'status', 403)
                self.assertNotIn(
                    status, AUTH_STATUSES,
                    f'{label}: 状态码 {status} 会被客户端折叠成「{AUTH_LOCALIZED}」，'
                    f'真实原因「{reason}」用户看不到')
                self.assertNotEqual(dsh_display(status, str(reason)), AUTH_LOCALIZED)
                self.assertIn(str(reason), dsh_display(status, str(reason)))

    def test_credential_errors_stay_403(self) -> None:
        """凭据本身不可用（停用/过期）→ 403 是贴切的，不该改。"""
        row, token = self._row_and_token()
        keysvc.update_key(row['id'], {'enabled': False})
        reason = keysvc.validate(keysvc.resolve(token), '1.2.3.4', 'glm-5.2')
        self.assertIsNotNone(reason)
        self.assertEqual(getattr(reason, 'status', 403), 403, str(reason))
        self.assertIn('停用', str(reason))

    def test_expired_key_stays_403(self) -> None:
        row, token = self._row_and_token()
        db.execute('UPDATE api_keys SET expires_at = ? WHERE id = ?',
                   (int(time.time()) - 60, row['id']))
        reason = keysvc.validate(keysvc.resolve(token), '1.2.3.4', 'glm-5.2')
        self.assertIsNotNone(reason)
        self.assertEqual(getattr(reason, 'status', 403), 403, str(reason))

    def test_quota_exhausted_is_not_auth_error(self) -> None:
        """配额用尽与认证无关：403 会被读成「密钥无效」，用户就去查密钥了。"""
        row, token = self._row_and_token(quota=10)
        db.execute('UPDATE api_keys SET used_tokens = 999 WHERE id = ?', (row['id'],))
        reason = keysvc.validate(keysvc.resolve(token), '1.2.3.4', 'glm-5.2')
        self.assertIsNotNone(reason)
        self.assertNotIn(getattr(reason, 'status', 403), AUTH_STATUSES, str(reason))
        self.assertIn('配额', str(reason))

    def test_rejection_is_still_a_plain_string(self) -> None:
        """兼容性：既有调用方把它当真值/文本用（`if reason:`、日志、DB 写入）。"""
        reason = keysvc.validate(self._row(realm='global'), '1.2.3.4', 'glm-5.2')
        self.assertTrue(reason)
        self.assertIsInstance(reason, str)
        self.assertIn('国际版', reason)
        self.assertEqual(len(reason), len(str(reason)))


class GatewayVisibilityE2ETest(unittest.TestCase):
    """真实 HTTP 链路：报文里必须带得上原因，且状态码不被折叠。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'e.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _chat(self, token: str, model: str):
        with mock.patch.object(config, 'WB2API_BASE', 'http://127.0.0.1:1'):
            return self.client.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {token}'},
                json={'model': model, 'messages': [{'role': 'user', 'content': 'hi'}]},
            )

    def test_wrong_realm_shows_real_reason_to_dsh_user(self) -> None:
        token = keysvc.create_key('g', realm='global')['key']
        r = self._chat(token, 'glm-5.2')
        self.assertEqual(r.status_code, 400, r.text)
        msg = r.json()['error']['message']
        shown = dsh_display(r.status_code, msg)
        self.assertNotEqual(shown, AUTH_LOCALIZED, f'原因被折叠掉了：{r.text}')
        self.assertIn('国际版', shown)
        # 报文里要给出可执行的下一步，否则用户只知道「错了」不知道「怎么改」
        self.assertIn('global:', msg)

    def test_anthropic_path_agrees_with_openai_path(self) -> None:
        """两个协议的鉴权必须同结论——同一把密钥不该在两个协议下判定不同。"""
        token = keysvc.create_key('g2', realm='global')['key']
        body = {'model': 'glm-5.2', 'max_tokens': 8,
                'messages': [{'role': 'user', 'content': 'hi'}]}
        with mock.patch.object(config, 'WB2API_BASE', 'http://127.0.0.1:1'):
            r = self.client.post('/v1/messages', headers={'x-api-key': token}, json=body)
        self.assertEqual(r.status_code, 400, r.text)
        text = re.sub(r'\s+', '', r.text)
        self.assertIn('国际版', text)


if __name__ == '__main__':
    unittest.main()
