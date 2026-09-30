"""入站访问日志要记下**被拦的原因**（issue #33）。

现场：用户贴出的日志里一排「已拦截」，UA 是 `CodexPlusPlus/RelayTest`，而同
一个 IP 上另一个客户端（ccswitch）能用。他问「为什么 Codex++ 一直失败」——
但**日志本身回答不了这个问题**：`ip_access_logs` 只存一个 blocked 布尔，
「没带密钥」「密钥不认识」「IP 规则拦的」三种情况在界面上长得一模一样，而
处置方式完全不同（改客户端配置 / 重新发密钥 / 改 IP 规则）。

所以这个文件锁两件事：

1. 每一处拒绝都带上**可区分的短码**，而不是只记一个 true；
2. **密钥层的拒绝也不能记成「已放行」**——那比不记更糟，用户会照着「已放行」
   去查网络和上游，方向直接跑偏。

顺带钉住三个实现细节（都是会静默出错的地方）：
  · 短码是稳定标识，不是译文（界面按语言翻译，切英文时历史记录也得是英文）；
  · 短码经 `db._clean` 清洗（它来自外部输入的路径上，换行能伪造日志行）；
  · 旧库升级后新列存在且历史行为 NULL（不编造原因）。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402


class ReasonColumnTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'r.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _rows(self) -> list[dict]:
        return db.query('SELECT * FROM ip_access_logs ORDER BY id')

    def test_schema_has_reason_column(self) -> None:
        cols = {r[1] for r in db.query('PRAGMA table_info(ip_access_logs)')}
        self.assertIn('reason', cols, 'ip_access_logs 缺少 reason 列')

    def test_reason_is_persisted(self) -> None:
        db.add_ip_access_log('1.2.3.4', '/v1/chat/completions', True, 'UA', 'missing_key')
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reason'], 'missing_key')
        self.assertEqual(rows[0]['blocked'], 1)

    def test_allowed_rows_carry_no_reason(self) -> None:
        db.add_ip_access_log('1.2.3.4', '/v1/models', False, 'UA')
        self.assertIsNone(self._rows()[0]['reason'], '放行不该带原因')

    def test_reason_is_sanitised(self) -> None:
        """换行必须被清掉：它能在日志界面上伪造出额外的行。"""
        db.add_ip_access_log('1.2.3.4', '/x', True, 'UA', 'bad\ninjected\r\nline')
        reason = self._rows()[0]['reason']
        self.assertNotIn('\n', reason)
        self.assertNotIn('\r', reason)

    def test_reason_is_truncated(self) -> None:
        db.add_ip_access_log('1.2.3.4', '/x', True, 'UA', 'z' * 5000)
        self.assertLessEqual(len(self._rows()[0]['reason']), 200)

    def test_reason_defaults_to_none_when_omitted(self) -> None:
        """省略 reason 的调用方（旧代码路径）不该报错，记成 NULL。"""
        db.add_ip_access_log('1.2.3.4', '/x', True, 'UA')
        self.assertIsNone(self._rows()[0]['reason'])


class OldDatabaseMigrationTest(unittest.TestCase):
    """升级路径：旧库（无 reason 列）打开后要能正常写入并读到 NULL。"""

    def test_migration_adds_column_to_existing_db(self) -> None:
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / 'old.db'
        # 造一个**旧版结构**的库：没有 reason 列，且已有一行历史数据
        conn = sqlite3.connect(str(path))
        conn.executescript(
            'CREATE TABLE ip_access_logs ('
            '  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,'
            '  ip TEXT, path TEXT, blocked INTEGER NOT NULL DEFAULT 0, ua TEXT);'
            'INSERT INTO ip_access_logs(ts, ip, path, blocked, ua)'
            "  VALUES(1, '9.9.9.9', '/v1/models', 1, 'old-ua');"
        )
        conn.commit()
        conn.close()

        orig = config.DB_PATH
        config.DB_PATH = path
        db._conn = None
        try:
            db.connect()
            cols = {r[1] for r in db.query('PRAGMA table_info(ip_access_logs)')}
            self.assertIn('reason', cols, '迁移没有给旧库加上 reason 列')
            # 历史行为 NULL —— 不能编造一个原因出来
            rows = db.query('SELECT * FROM ip_access_logs')
            self.assertEqual(len(rows), 1, '迁移把历史数据弄丢了')
            self.assertIsNone(rows[0]['reason'])
            # 新写入照常
            db.add_ip_access_log('1.1.1.1', '/v1/chat/completions', True, 'UA', 'invalid_key')
            fresh = db.query('SELECT * FROM ip_access_logs ORDER BY id DESC LIMIT 1')[0]
            self.assertEqual(fresh['reason'], 'invalid_key')
        finally:
            try:
                if db._conn is not None:
                    db._conn.close()
            except Exception:  # noqa: BLE001
                pass
            db._conn = None
            config.DB_PATH = orig
            tmp.cleanup()


class GatewayReasonCodeTest(unittest.TestCase):
    """网关的拒绝分支必须传原因码，且原因码要能区分不同原因。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = (Path(__file__).resolve().parents[1] / 'routers' / 'gateway.py').read_text(
            encoding='utf-8')

    def test_each_reject_branch_passes_a_reason(self) -> None:
        """四个拒绝分支（缺密钥 / 密钥无效 / IP 规则 / 密钥层 / 限速）都要带码。"""
        for code in ('missing_key', 'invalid_key', 'ip_blocked', 'rate_limited'):
            with self.subTest(code=code):
                self.assertIn(
                    f"_log_ip(ip, path, True, ua, '{code}')", self.src,
                    f'{code} 分支没有把原因写进入站日志',
                )

    def test_key_layer_reject_is_not_logged_as_allowed(self) -> None:
        """密钥层拒绝**不能**记成「已放行」。

        原先 `_log_ip(ip, path, False, ua)` 在 keysvc.validate 之前无条件执行，
        于是「密钥已停用」「配额用尽」这些请求在日志里显示「已放行」而实际失败。
        用户对着「已放行」去查网络与上游，方向直接跑偏 —— 比不记还糟。
        """
        # 放行的那一行必须出现在 key 层拒绝之后（即不再是无条件前置）
        validate_at = self.src.index('keysvc.validate(key, ip, model')
        allowed_at = self.src.index('_log_ip(ip, path, False, ua)')
        reason_at = self.src.index('_key_reject_code(reason')
        self.assertGreater(
            allowed_at, validate_at,
            '「已放行」仍写在密钥校验之前：被密钥层拒绝的请求会被记成已放行',
        )
        self.assertGreater(
            reason_at, validate_at,
            '密钥层拒绝没有记录原因',
        )

    def test_reject_code_helper_falls_back(self) -> None:
        """reason 上取不到 code 时回落到一个可读的通用码，不能是空串。"""
        import asyncio  # noqa: F401
        from server.routers import gateway

        class _NoCode:
            pass

        self.assertEqual(gateway._key_reject_code(_NoCode(), False), 'key_rejected')
        self.assertEqual(gateway._key_reject_code('', False), 'key_rejected')

    def test_reject_code_uses_keysvc_code(self) -> None:
        from server.routers import gateway
        from server.keysvc import Rejection

        r = Rejection('密钥已停用', 403, 'permission_error', 'key_disabled')
        self.assertEqual(gateway._key_reject_code(r, False), 'key_disabled')


class FrontendReasonMappingTest(unittest.TestCase):
    """前端的短码→文案映射必须覆盖后端会产出的所有码。"""

    def _page(self) -> str:
        return (Path(__file__).resolve().parents[2] / 'web' / 'app' / '(main)'
                / 'security' / 'page.tsx').read_text(encoding='utf-8')

    def test_all_backend_codes_are_mapped(self) -> None:
        page = self._page()
        # 网关自己产的
        gateway_codes = ['missing_key', 'invalid_key', 'ip_blocked', 'rate_limited']
        # keysvc 产的（与 keysvc.py 里的 code 字面量一致）
        keysvc_src = (Path(__file__).resolve().parents[1] / 'keysvc.py').read_text(encoding='utf-8')
        import re
        keysvc_codes = sorted(set(re.findall(
            r"Rejection\([^)]*?'(?:permission_error|insufficient_quota|invalid_request_error)'"
            r",\s*'([a-z_]+)'", keysvc_src, re.S)))
        self.assertGreater(len(keysvc_codes), 5, f'没解析出 keysvc 的 code：{keysvc_codes}')
        for code in gateway_codes + keysvc_codes:
            with self.subTest(code=code):
                self.assertIn(
                    f"{code}: 'security.reason", page,
                    f'前端没有 {code} 的文案映射，界面上会直接显示英文码',
                )

    def test_unknown_code_is_shown_verbatim(self) -> None:
        """认不出的码原样显示，不能变成空白或「未知」——用户至少能拿它去搜。"""
        page = self._page()
        self.assertIn('return key ? t(key) : code;', page,
                      '未知码的兜底不是原样显示')

    def test_reason_is_only_shown_when_blocked(self) -> None:
        """放行行不该显示原因（后端也不会给，但前端要防）。"""
        page = self._page()
        self.assertIn('l.blocked && l.reason', page)


if __name__ == '__main__':
    unittest.main()


class AuditLogOnlyBlockedTest(unittest.TestCase):
    """入站访问日志默认**只记拦截**（服务器审计的结论）。

    现场：线上 7877 行里只有 17 行是拦截记录（0.2%），其余全是正常请求
    （`_log_ip(..., False, ...)` 给每个成功请求也写一行）。那张表在安全页叫
    「IP 访问日志」，用途是回答「谁在扫我、谁被挡了」——被正常流量淹没后，
    真正的信号要翻 7800 行才找得到；而它的 2 万行滚动上限（注释写明「按每次
    拒绝一行估算足够回溯近期攻击」）被成功请求占满后，保留窗口从数月压到
    约 17 天，真出事时记录可能已被挤掉。

    放行的明细在「请求日志」页有完整记录，这里不重复记不丢信息。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = (Path(__file__).resolve().parents[1] / 'routers' / 'gateway.py').read_text(
            encoding='utf-8')

    def test_allowed_not_written_by_default(self) -> None:
        """`_log_ip` 必须在放行且未开全量开关时直接返回。"""
        self.assertIn('if not blocked and not config.AUDIT_ALL_ACCESS:', self.src,
                      '_log_ip 没有默认不记放行的过滤，正常流量会继续淹没安全日志')

    def test_switch_exists_and_defaults_off(self) -> None:
        from server import config
        self.assertFalse(config.AUDIT_ALL_ACCESS, '全量记录开关应默认关闭（会淹没信号）')
        cfg_src = (Path(__file__).resolve().parents[1] / 'config.py').read_text(encoding='utf-8')
        self.assertIn('WB_AUDIT_ALL_ACCESS', cfg_src, '开关没有对应的环境变量')

    def test_blocked_branches_still_log(self) -> None:
        """所有拦截分支照旧记录（过滤只针对放行）。"""
        for code in ('missing_key', 'invalid_key', 'ip_blocked', 'rate_limited'):
            with self.subTest(code=code):
                self.assertIn(f"_log_ip(ip, path, True, ua, '{code}')", self.src)

    def test_reason_filter_is_order_independent(self) -> None:
        """反证用：确认过滤条件不会把所有写入都挡掉。

        断言里必须出现 `not blocked`（而不是写成 `blocked`）——写反就会
        「只记放行、不记拦截」，与意图完全相反且同样静默。
        """
        idx = self.src.index('def _log_ip')
        seg = self.src[idx:idx + 3000]
        self.assertIn('if not blocked and not config.AUDIT_ALL_ACCESS', seg)
        self.assertNotIn('if blocked and not config.AUDIT_ALL_ACCESS', seg,
                         '条件写反了：变成只记放行、不记拦截')


class RequestLogRetentionTest(unittest.TestCase):
    """请求日志按保留期滚动清理（此前完全没有清理机制）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'r.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _add(self, ts: int) -> None:
        db.add_request_log(ts=ts, key_id=None, ip='1.1.1.1', model='m',
                           mapped_model='m', status=200, prompt_tokens=1,
                           completion_tokens=1, latency_ms=1, first_token_ms=None,
                           ua='UA', error=None, stream=1, credit=None, realm='cn')

    def test_old_rows_pruned(self) -> None:
        now = int(time.time())
        keep = db._REQUEST_LOG_RETAIN_DAYS
        self._add(now - (keep + 10) * 86400)   # 超出保留期
        self._add(now - 5 * 86400)             # 保留期内
        db._prune_request_logs()
        rows = db.query('SELECT ts FROM request_logs')
        self.assertEqual(len(rows), 1, f'清理没生效或删多了：{rows}')
        self.assertGreater(rows[0]['ts'], now - (keep + 1) * 86400)

    def test_boundary_row_kept(self) -> None:
        """正好在保留期边界的记录不该被删（边界差一天）。"""
        now = int(time.time())
        keep = db._REQUEST_LOG_RETAIN_DAYS
        self._add(now - (keep - 1) * 86400)
        db._prune_request_logs()
        self.assertEqual(len(db.query('SELECT ts FROM request_logs')), 1)

    def test_add_request_log_writes_values_not_column_names(self) -> None:
        """**回归**：kwargs 必须展开成位置参数。

        `execute()` 内部是 `tuple(args)`，直接传 dict 会把它当成单个参数，
        于是**列名被当值**写进库（实测 `credit` 列存进了字符串 `'credit'`，
        紧接着 rebuild 因 `NOT NULL constraint failed: usage_daily.day` 崩掉）。
        这类损坏是静默的：写入不报错，只有下游查询才炸。
        """
        now = int(time.time())
        self._add(now)
        row = db.query_one('SELECT ts, key_id, ip, model, status, credit, realm '
                           'FROM request_logs')
        self.assertEqual(row['ip'], '1.1.1.1')
        self.assertEqual(row['model'], 'm')
        self.assertEqual(int(row['status']), 200)
        self.assertIsNone(row['credit'], 'credit 应为 NULL，不是字符串 "credit"')
        self.assertIn(row['realm'], ('cn', 'global'), f'realm 异常：{row["realm"]!r}')
        self.assertGreater(int(row['ts']), 0)

    def test_prune_failure_is_swallowed(self) -> None:
        """清理失败必须被吞掉（旁路操作，不能影响写入，更不能影响转发）。

        做法：让 DELETE 真的失败（表名写坏），确认 `_prune_request_logs`
        仍然正常返回 —— 若它把异常抛出去，网关的写入路径会连带出错。
        """
        with mock.patch.object(db, 'execute', side_effect=RuntimeError('boom')):
            db._prune_request_logs()   # 不该抛

    def test_check_counter_triggers_prune(self) -> None:
        """按写入次数触发清理（而非每次写入都查一次，那是一趟全表扫描）。"""
        with mock.patch.object(db, '_prune_request_logs') as prune:
            db._request_log_writes = 0
            for _ in range(db._REQUEST_LOG_CHECK_EVERY):
                self._add(int(time.time()))
            self.assertEqual(prune.call_count, 1, '写入达到间隔后没有触发清理')
            self.assertEqual(db._request_log_writes, 0, '计数器没有重置')
