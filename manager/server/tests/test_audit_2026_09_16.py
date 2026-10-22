"""审计发现的缺陷的回归测试（2026-09-16 全面审查）。

这些**不是新功能**，而是把「已经写错的东西」钉住，避免修完之后又漂回去。
每条都对应一次实测复现。

覆盖：
  1. 未鉴权可达的 `ip_access_logs` 无上限、无清洗（可刷满磁盘 / 伪造日志行）
  2. SSRF 守卫可被 userinfo、localhost、通配 DNS 绕过
  3. `model` / `ua` / `error` 无长度限制 → 单次请求放大数 MB 入库
  4. `/api/*` 请求体无上限（`/api/login` 未鉴权）
  5. 上游配置下发用 denylist → 未来新增的密钥字段会泄漏
  6. `_usage_health` 只数「本该累计」的日志（被拒调用不该触发误报）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, security  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


class IpAccessLogHardeningTest(_DbCase):
    """入站访问日志：清洗 + 截断 + 行数上限。

    这张表在网关鉴权**之前**写入（缺 token / token 无效都会记一行），
    也就是**未鉴权可达**。没有上限时，匿名者用「无效 key + 超长 UA」就能把
    磁盘灌满（实测 2KB UA ≈ 1.3KB/行）；没有清洗时，UA 里的换行能伪造日志行。
    """

    def test_sanitizes_crlf(self) -> None:
        db.add_ip_access_log('1.2.3.4', '/v1/models', True,
                             'UA\r\nINFO fake line: admin login ok\r\n')
        row = db.query_one('SELECT ua, path FROM ip_access_logs')
        self.assertNotIn('\n', row['ua'])
        self.assertNotIn('\r', row['ua'])

    def test_truncates_long_fields(self) -> None:
        db.add_ip_access_log('1.2.3.4', '/x' * 5000, True, 'U' * 5000)
        row = db.query_one('SELECT length(ua) ua, length(path) p, length(ip) ip FROM ip_access_logs')
        self.assertLessEqual(row['ua'], 512)
        self.assertLessEqual(row['p'], 256)
        self.assertLessEqual(row['ip'], 64)

    def test_row_cap_enforced(self) -> None:
        """超出上限后旧行被裁掉，表不会无限增长。"""
        orig_max, orig_every = db._IP_ACCESS_LOG_MAX, db._IP_ACCESS_LOG_CHECK_EVERY
        db._IP_ACCESS_LOG_MAX, db._IP_ACCESS_LOG_CHECK_EVERY = 100, 10
        try:
            for _ in range(300):
                db.add_ip_access_log('1.2.3.4', '/v1/models', True, 'ua')
            n = db.query_one('SELECT COUNT(*) c FROM ip_access_logs')['c']
            self.assertLessEqual(
                n, db._IP_ACCESS_LOG_MAX + db._IP_ACCESS_LOG_CHECK_EVERY,
                f'表行数 {n} 超出上限，匿名者可以靠它把磁盘写满',
            )
        finally:
            db._IP_ACCESS_LOG_MAX, db._IP_ACCESS_LOG_CHECK_EVERY = orig_max, orig_every

    def test_cap_keeps_newest(self) -> None:
        """裁剪必须丢最旧的（近期攻击记录要留得住）。"""
        orig_max, orig_every = db._IP_ACCESS_LOG_MAX, db._IP_ACCESS_LOG_CHECK_EVERY
        db._IP_ACCESS_LOG_MAX, db._IP_ACCESS_LOG_CHECK_EVERY = 20, 5
        try:
            for i in range(100):
                db.add_ip_access_log(f'10.0.0.{i}', '/x', True, f'ua{i}')
            last = db.query_one('SELECT ip, ua FROM ip_access_logs ORDER BY id DESC LIMIT 1')
            self.assertEqual(last['ua'], 'ua99', '最新的记录被误删了')
        finally:
            db._IP_ACCESS_LOG_MAX, db._IP_ACCESS_LOG_CHECK_EVERY = orig_max, orig_every


class RequestLogHardeningTest(_DbCase):
    """调用日志：外部来源文本一律清洗 + 截断。

    `model` 来自请求体且长度无上限，会被写进 `request_logs.model`、
    `mapped_model` 与 `usage_daily.model` **三处**（后者在主键里、等于再加一份
    索引）。实测 1 MiB 的 model 单请求放大 ~2.5MB；持密钥者可用少量请求撑大库。
    """

    def _record(self, **kw):
        from server.routers import gateway

        defaults = dict(key=None, ip='1.2.3.4', model='glm-5.2', mapped='',
                        status=200, pt=1, ct=1, latency=10, ua='UA',
                        error=None, stream=False)
        defaults.update(kw)
        gateway._record(**defaults)

    def test_model_truncated(self) -> None:
        self._record(model='M' * 5000)
        row = db.query_one('SELECT length(model) a, length(mapped_model) b FROM request_logs')
        self.assertLessEqual(row['a'], 128, 'model 未截断')
        self.assertLessEqual(row['b'], 128)

    def test_ua_and_error_cleaned(self) -> None:
        self._record(ua='UA\r\nfake: admin ok', error='err\r\nfake line')
        row = db.query_one('SELECT ua, error FROM request_logs')
        self.assertNotIn('\n', row['ua'] or '')
        self.assertNotIn('\n', row['error'] or '')

    def test_realm_derived_from_truncated_model(self) -> None:
        """截断后仍要正确判定版本（前缀在最前面，截断不影响它）。"""
        self._record(model='global:' + 'x' * 500)
        row = db.query_one('SELECT realm FROM request_logs')
        self.assertEqual(row['realm'], 'global')

    def test_normal_model_untouched(self) -> None:
        """正常模型名不能被改动 —— 否则统计/日志按模型聚合会错。"""
        self._record(model='global:gpt-5.6-sol', mapped='global:gpt-5.6-sol')
        row = db.query_one('SELECT model, mapped_model, realm FROM request_logs')
        self.assertEqual(row['model'], 'global:gpt-5.6-sol')
        self.assertEqual(row['mapped_model'], 'global:gpt-5.6-sol')
        self.assertEqual(row['realm'], 'global')

    def test_realm_follows_the_outbound_model(self) -> None:
        """`realm` 判**实际发往上游**的那个名字（issue #47 的连带面）。

        上游按模型名的 `global:` 前缀选账号池，所以走哪个池由**出站**名字决定。
        配了模型映射时两者可能不同：

          · 请求 `global:gpt-5.6-sol`、映射成裸名 `gpt-5.6-sol`
            → 上游按裸名路由（国内池），日志就该记 cn。记成 global 会让
            「按版本筛选日志」把一次真实的国内版调用列到国际版那一栏。
          · 反向（裸名 → `global:...`）同理记 global。

        注：改这一条是因为 issue #47 之后**跨版本别名调用被放行**了——此前那样
        的请求直接被 400 拒掉，压根不会产生日志行，口径不一致看不出来。
        """
        self._record(model='global:gpt-5.6-sol', mapped='gpt-5.6-sol')
        self.assertEqual(db.query_one('SELECT realm FROM request_logs')['realm'], 'cn')
        db.execute('DELETE FROM request_logs')
        self._record(model='claude-fable-5', mapped='global:deepseek-v4.1-flash')
        self.assertEqual(db.query_one('SELECT realm FROM request_logs')['realm'], 'global')

    def test_realm_from_request_when_no_mapping(self) -> None:
        """没有映射（`mapped` 为空）时仍按请求名判 —— 行为不变。"""
        self._record(model='global:gpt-5.6-sol', mapped='')
        self.assertEqual(db.query_one('SELECT realm FROM request_logs')['realm'], 'global')


class ConfigMaskingAllowlistTest(unittest.TestCase):
    """上游配置下发必须是**白名单**。

    denylist 的失效模式很隐蔽：上游（或运维手工）新增一个密钥字段，它就会
    **原样下发**给任何登录用户（含只读 viewer），而且不会有任何报错。
    白名单的失效模式相反（新字段不显示），安全得多。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _write(self, data: dict) -> None:
        self.cfg.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def test_unknown_secret_keys_not_leaked(self) -> None:
        """未知的顶层键（上游未来可能新增的凭据）不应出现在响应里。"""
        from server.services import wb2api
        self._write({
            'api_key': 'sk-secret-value-abcdef123456',
            'future_secret_key': 'LEAK-A',
            'state_file': '/opt/state.json',
            'pool': {'max_in_flight': 3},
        })
        dumped = json.dumps(wb2api.load_upstream_config(), ensure_ascii=False)
        self.assertNotIn('LEAK-A', dumped, '未知顶层键被下发了')
        self.assertNotIn('sk-secret-value-abcdef123456', dumped, 'api_key 明文被下发了')
        self.assertNotIn('/opt/state.json', dumped, 'state_file 路径被下发了')

    def test_editable_sections_still_present(self) -> None:
        """白名单不能把界面要用的段一起挡掉（否则设置页全空）。"""
        from server.services import wb2api
        self._write({
            'pool': {'max_in_flight': 3},
            'schedule': {'checkin_hours': [9]},
            'upstream': {'user_agent': 'UA', 'device_token': 'devtok'},
        })
        view = wb2api.load_upstream_config()
        self.assertEqual(view['pool'], {'max_in_flight': 3})
        self.assertEqual(view['schedule'], {'checkin_hours': [9]})
        self.assertEqual(view['upstream'].get('user_agent'), 'UA')
        self.assertNotIn('device_token', view['upstream'], 'device_token 明文被下发了')

    def test_load_and_save_share_one_section_list(self) -> None:
        """下发与写入必须用同一份段列表，否则会出现「存了读不回」或反之。"""
        import inspect

        from server.services import wb2api
        src = inspect.getsource(wb2api.load_upstream_config)
        save_src = inspect.getsource(wb2api.save_upstream_config)
        self.assertIn('_EDITABLE_SECTIONS', src)
        self.assertIn('_EDITABLE_SECTIONS', save_src)


class ApiBodyLimitTest(unittest.TestCase):
    """`/api/*` 的请求体上限（网关那条路早有，管理端此前没有）。"""

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'm.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('TestPw123!')}],
            'api_keys': [],
        })
        from server.main import app
        cls.c = TestClient(app, raise_server_exceptions=False)

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

    def test_oversized_api_body_rejected(self) -> None:
        from server.main import MAX_API_BODY_BYTES
        r = self.c.post('/api/login', content=b'x' * 1024,
                        headers={'Content-Type': 'application/json',
                                 'Content-Length': str(MAX_API_BODY_BYTES + 1)})
        self.assertEqual(r.status_code, 413, '超大请求体没有被拦下')

    def test_normal_api_body_works(self) -> None:
        r = self.c.post('/api/login', json={'username': 'admin', 'password': 'TestPw123!'})
        self.assertEqual(r.status_code, 200)

    def test_gateway_not_affected(self) -> None:
        """网关有自己的逐块校验，不该被这条中间件重复拦截（它的路径不是 /api/）。"""
        r = self.c.get('/v1/models')
        self.assertEqual(r.status_code, 401, '网关的缺少密钥响应应保持不变')


if __name__ == '__main__':
    unittest.main()
