"""存量库升级：usage_daily 主键迁移（issue #9）。

**只有存量部署会踩到，全新安装与 CI 都复现不了**——这正是它躲过测试的原因：
新库建表即四列主键，而旧库（v1.0.33 之前建的）主键是三列，迁移当时只做了
`ALTER TABLE ADD COLUMN realm`。SQLite 改不了主键，于是写入侧的四列
`ON CONFLICT(day, key_id, model, realm)` 在旧库上直接抛：

    OperationalError: ON CONFLICT clause does not match any PRIMARY KEY
                       or UNIQUE constraint

后果的**严重程度被低估过**：上一版注释写的是「接受旧库在这一维度上的精度
损失」（指两个版本的同名模型合并累计），但实际是一行都写不进去 ——
`bump_usage` 的异常在网关里被兜住只记 warning（旁路统计不该影响转发），
于是统计数字永久冻结、每笔请求刷一条日志、页面却看不出异常。

本文件锁住：旧库能被正确迁移到四列主键，且历史数据不丢。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import stats  # noqa: E402

# v1.0.33 之前建表 + 之后只 ADD COLUMN realm 的形态（issue #9 的真实库结构）
_OLD_USAGE_DAILY = """
CREATE TABLE usage_daily (
  day TEXT NOT NULL, key_id INTEGER NOT NULL, model TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  credit REAL NOT NULL DEFAULT 0,
  realm TEXT NOT NULL DEFAULT 'cn',
  PRIMARY KEY (day, key_id, model)
)
"""

# 迁移只用到这几张表的存在性；request_logs / api_keys 供 repair/rebuild 走通
_OTHER_TABLES = """
CREATE TABLE request_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key_id INTEGER, ip TEXT,
  model TEXT, mapped_model TEXT, status INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0, first_token_ms INTEGER,
  ua TEXT, error TEXT, stream INTEGER DEFAULT 0, credit REAL, realm TEXT);
CREATE TABLE api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, key_hash TEXT NOT NULL,
  prefix TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, expires_at INTEGER,
  max_ips INTEGER NOT NULL DEFAULT 0, ip_allowlist TEXT NOT NULL DEFAULT '[]',
  models TEXT NOT NULL DEFAULT '[]', quota INTEGER NOT NULL DEFAULT 0,
  used_tokens INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, last_used_at INTEGER);
"""


class LegacyUsageDailyMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'legacy.db'

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _make_legacy_db(self, rows: list[tuple] = ()) -> None:
        """建一个「v1.0.32 建表 + v1.0.33 只加了列」的旧库。"""
        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript(_OLD_USAGE_DAILY + ';' + _OTHER_TABLES)
        for day, key_id, model, req, pt, ct, cr, realm in rows:
            conn.execute(
                'INSERT INTO usage_daily(day,key_id,model,requests,prompt_tokens,'
                'completion_tokens,credit,realm) VALUES(?,?,?,?,?,?,?,?)',
                (day, key_id, model, req, pt, ct, cr, realm))
        conn.commit()
        conn.close()

    def _pk_line(self) -> str:
        row = db.query_one("SELECT sql FROM sqlite_master WHERE name='usage_daily'")
        for ln in (row['sql'] or '').splitlines():
            if 'PRIMARY KEY' in ln.upper():
                return ln.strip()
        return ''

    def test_legacy_pk_is_migrated_to_four_columns(self) -> None:
        """迁移后主键必须含 realm——否则四列 UPSERT 一行也写不进去。"""
        self._make_legacy_db()
        db.connect()
        pk = self._pk_line()
        self.assertIn('realm', pk, f'主键没迁成四列：{pk}')
        self.assertIn('model', pk)

    def test_legacy_history_is_preserved(self) -> None:
        """历史汇总不能因为重建表而丢失。"""
        self._make_legacy_db([
            ('2026-09-10', 1, 'glm-5.2', 7, 700, 300, 1.5, 'cn'),
            ('2026-09-11', 1, 'gpt-5.4', 3, 300, 100, 0.9, 'global'),
        ])
        db.connect()
        rows = db.query('SELECT day, model, requests, prompt_tokens, credit, realm '
                        'FROM usage_daily ORDER BY day')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['requests'], 7)
        self.assertEqual(rows[0]['prompt_tokens'], 700)
        self.assertEqual(rows[0]['credit'], 1.5)
        self.assertEqual(rows[0]['realm'], 'cn')
        self.assertEqual(rows[1]['realm'], 'global')

    def test_bump_usage_works_after_migration(self) -> None:
        """核心回归：迁移后写入必须成功（issue #9 之前这里是静默失败）。"""
        self._make_legacy_db()
        db.connect()
        db.bump_usage(1, 'glm-5.2', 10, 20, credit=0.5, realm='cn')
        row = db.query_one("SELECT requests, prompt_tokens, realm FROM usage_daily "
                           "WHERE model='glm-5.2'")
        self.assertIsNotNone(row, '写入没落库')
        self.assertEqual(row['requests'], 1)
        self.assertEqual(row['prompt_tokens'], 10)
        self.assertEqual(row['realm'], 'cn')

    def test_different_realms_accumulate_separately(self) -> None:
        """两个版本的同名模型各占一行——四列主键存在的意义。"""
        self._make_legacy_db()
        db.connect()
        db.bump_usage(1, 'dual', 10, 20, credit=0.5, realm='cn')
        db.bump_usage(1, 'dual', 10, 20, credit=0.5, realm='global')
        db.bump_usage(1, 'dual', 10, 20, credit=0.5, realm='cn')   # 同一行累加
        rows = db.query("SELECT realm, requests FROM usage_daily WHERE model='dual' ORDER BY realm")
        self.assertEqual([(r['realm'], r['requests']) for r in rows], [('cn', 2), ('global', 1)])

    def test_repair_and_rebuild_work_after_migration(self) -> None:
        """「修复统计」此前 500、「重建统计」跨版本撞旧唯一约束——迁移后都应可用。"""
        self._make_legacy_db()
        db.connect()
        # 造一条请求日志，让 repair/rebuild 有活干
        db.execute('INSERT INTO request_logs(ts,key_id,model,prompt_tokens,'
                   'completion_tokens,credit,realm,status) VALUES(?,?,?,?,?,?,?,?)',
                   (int(db.time.time()), 1, 'glm-5.2', 100, 50, 0.1, 'cn', 200))
        db.bump_usage(1, 'glm-5.2', 10, 5, credit=0.0, realm='cn')
        db.backfill_usage_from_logs()      # 修复统计
        db.rebuild_usage_from_logs()       # 重建统计
        # 走到这里没抛异常即为通过（此前是 OperationalError → 路由 500）
        rows = db.query('SELECT realm, requests FROM usage_daily')
        self.assertTrue(rows, 'rebuild 之后不该是空表')

    def test_migration_is_idempotent(self) -> None:
        """重复启动不该反复迁移（更不该把数据搬没）。"""
        self._make_legacy_db([('2026-09-10', 1, 'glm-5.2', 7, 700, 300, 1.5, 'cn')])
        db.connect()
        first = self._pk_line()
        db._conn.close()
        db._conn = None
        db.connect()   # 二次启动
        self.assertEqual(self._pk_line(), first)
        row = db.query_one('SELECT COUNT(*) c, COALESCE(SUM(requests),0) r FROM usage_daily')
        self.assertEqual(row['c'], 1)
        self.assertEqual(row['r'], 7)

    def test_recovers_from_leftover_table(self) -> None:
        """上次迁移若留下半成品表，这次必须能自愈——否则永远修不好。

        **SQLite 不回滚 DDL**（实测确认）：在事务里 `CREATE TABLE` 之后即使
        抛异常回滚，那张表依然留在库里。所以迁移失败一次就会留下
        `usage_daily_new`，下次启动的 `CREATE TABLE` 会因重名失败、再留下一次
        ——如此循环，统计永远修不好，而每次的原因看起来都一样。

        这里手工造出那个残留，验证迁移仍能完成。
        """
        self._make_legacy_db([('2026-09-10', 1, 'glm-5.2', 7, 700, 300, 1.5, 'cn')])
        conn = sqlite3.connect(config.DB_PATH)
        # 模拟「上次迁移中途失败」留下的半成品表（结构与真表不同，更接近残骸）
        conn.execute('CREATE TABLE usage_daily_new (garbage TEXT)')
        conn.commit()
        conn.close()

        db.connect()
        self.assertIn('realm', self._pk_line(), '有残留表时迁移失败了')
        self.assertEqual(db.query_one('SELECT COUNT(*) c FROM usage_daily')['c'], 1,
                         '历史数据应已搬过来')
        # 残留表必须被清掉，否则下次 CREATE 又会重名
        left = db.query("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'usage_daily%'")
        self.assertEqual([r['name'] for r in left], ['usage_daily'],
                         '残留的临时表没清干净')
        # 而且要真的能写入（迁移成功的最终判据）
        db.bump_usage(1, 'glm-5.2', 10, 20, credit=0.5, realm='cn')

    def test_new_db_untouched(self) -> None:
        """全新库本来就是四列主键，迁移不该动它。"""
        db.connect()
        self.assertIn('realm', self._pk_line())
        db.bump_usage(1, 'glm-5.2', 10, 20, credit=0.5, realm='cn')
        self.assertEqual(db.query_one('SELECT COUNT(*) c FROM usage_daily')['c'], 1)

    def test_realm_column_survives_migration(self) -> None:
        """迁移是「搬列」而非「重设默认值」：原有 realm 值必须原样带过去。

        这条替代了我最初想写的「NULL 归一到 cn」——实测写不进去：旧库的
        realm 列声明是 `NOT NULL DEFAULT 'cn'`（迁移 ADD COLUMN 时就是这么建的），
        所以 **NULL 根本不存在于该列**。我原本为不存在的情况加了 COALESCE 兜底，
        那是多余的；这里改为验证真正会发生的事：值不被改动。
        """
        self._make_legacy_db([
            ('2026-09-10', 1, 'a', 1, 10, 5, 0.1, 'cn'),
            ('2026-09-11', 1, 'b', 1, 10, 5, 0.1, 'global'),
        ])
        db.connect()
        got = {r['model']: r['realm'] for r in
               db.query('SELECT model, realm FROM usage_daily')}
        self.assertEqual(got, {'a': 'cn', 'b': 'global'})


class UsageHealthTest(unittest.TestCase):
    """统计健康提示：暴露「今天有请求但统计为 0」这一异常组合。

    issue #9 最伤的地方是**静默**：统计写入是旁路（失败只记 warning、不影响
    转发），坏了以后页面照常刷新、数字只是不动，用户完全看不出异常。所以除了
    修根因，还要让这种状态在界面上**可见**。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'h.db'
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

    def _log_today(self, n: int = 1) -> None:
        # 必须带 token：`_usage_health` 只数**本该累计用量**的日志
        # （有 key 且 token/扣费非 0），与 `bump_usage` 的调用条件对齐。
        # 不带 token 的日志（如被 403 拒绝的调用）根本不会产生用量行，
        # 把它们算进去会造成误报 —— 那是修掉的 bug，不是这里要复现的场景。
        self._log_realm(n, 'cn')



    def test_flags_when_logs_exist_but_stats_empty(self) -> None:
        """今天有调用记录、统计却为 0 —— 正是 issue #9 的形态。"""
        self._log_today(3)
        h = stats._usage_health(db.day_of(), 0)
        self.assertFalse(h['ok'], '这种组合必须报警')
        self.assertIn('3', h['detail'], '提示里应带上实际调用次数，便于判断')

    def test_ok_when_stats_have_data(self) -> None:
        self._log_today(3)
        self.assertTrue(stats._usage_health(db.day_of(), 3)['ok'])

    def test_ok_when_nothing_happened_today(self) -> None:
        """今天既没请求也没统计 → 正常（刚部署 / 还没人用），不该误报。"""
        self.assertTrue(stats._usage_health(db.day_of(), 0)['ok'])

    def test_ok_when_logs_were_cleared(self) -> None:
        """请求日志被清空但统计仍有值 → 不该误报（重建统计时会遇到）。"""
        self.assertTrue(stats._usage_health(db.day_of(), 10)['ok'])

    def test_never_raises_on_broken_db(self) -> None:
        """健康检查本身绝不能把统计接口带崩——它只是个提示。"""
        db._conn.close()
        db._conn = None
        config.DB_PATH = Path(self._tmp.name) / 'nope' / 'x.db'
        h = stats._usage_health(db.day_of(), 0)
        self.assertIn('ok', h)

    # ── 版本口径：两边必须一致（真实误报教会的）─────────────────

    def _log_realm(self, n: int, realm: str) -> None:
        now = int(db.time.time())
        for _ in range(n):
            db.execute('INSERT INTO request_logs(ts,key_id,model,status,realm,'
                       'prompt_tokens,completion_tokens) VALUES(?,?,?,?,?,?,?)',
                       (now, 1, 'm', 200, realm, 10, 5))

    def test_global_not_flagged_when_only_cn_has_traffic(self) -> None:
        """**核心回归**：只有国内版流量时，切到国际版不该报「统计没在累计」。

        曾经的实现里，日志条数按**全版本**统计、用量按**当前版本**过滤 ——
        于是「有国内版流量、没国际版流量」时，切到国际版必然触发
        `n > 0 and today_requests == 0`，页面显示
        「今天已有 605 次调用记录，但用量统计为 0」，
        而点「修复统计」又说「统计与请求日志一致」。两条结论自相矛盾。
        """
        self._log_realm(605, 'cn')
        h = stats._usage_health(db.day_of(), 0, 'global')
        self.assertTrue(h['ok'], f'国际版不该被国内版流量误报：{h}')
        self.assertEqual(h['detail'], '')

    def test_cn_still_flagged_when_cn_traffic_but_no_stats(self) -> None:
        """反证：国内版真的「有日志没统计」时，仍然必须报警。

        修口径不能把检测本身改瞎——那等于把 issue #9 的防线拆了。
        """
        self._log_realm(5, 'cn')
        h = stats._usage_health(db.day_of(), 0, 'cn')
        self.assertFalse(h['ok'], '国内版有日志没统计时必须报警')
        self.assertIn('国内版', h['detail'], '提示要指明是哪个版本')

    def test_global_flagged_when_global_traffic_but_no_stats(self) -> None:
        """国际版自己真的有「有日志没统计」时也要报警（别只顾一边）。"""
        self._log_realm(7, 'global')
        h = stats._usage_health(db.day_of(), 0, 'global')
        self.assertFalse(h['ok'], '国际版有日志没统计时必须报警')
        self.assertIn('国际版', h['detail'])

    def test_cross_realm_counts_are_isolated(self) -> None:
        """两个版本的日志互不干扰：各自只看自己的。"""
        self._log_realm(4, 'cn')
        self._log_realm(6, 'global')
        # 国际版有日志、有统计 → ok
        self.assertTrue(stats._usage_health(db.day_of(), 6, 'global')['ok'])
        # 国际版有日志、统计为 0 → 报警，且报的是 6 次（不是 10 次）
        h = stats._usage_health(db.day_of(), 0, 'global')
        self.assertFalse(h['ok'])
        self.assertIn('6', h['detail'], f'次数应只算国际版：{h["detail"]}')

    def test_null_realm_history_counts_as_cn(self) -> None:
        """历史日志 realm 为 NULL → 归 cn（与 realm_of_model 口径一致）。"""
        db.execute('INSERT INTO request_logs(ts,key_id,model,status,realm,'
                   'prompt_tokens,completion_tokens) VALUES(?,?,?,?,?,?,?)',
                   (int(db.time.time()), 1, 'm', 200, None, 10, 5))
        self.assertFalse(stats._usage_health(db.day_of(), 0, 'cn')['ok'],
                         'NULL 应算作国内版')
        self.assertTrue(stats._usage_health(db.day_of(), 0, 'global')['ok'],
                        'NULL 不该算进国际版')

    def test_rejected_calls_do_not_trigger_the_warning(self) -> None:
        """**反误报**：只发生被拒绝的调用时不该报警。

        `request_logs` 是无条件写的，而 `bump_usage` 只在「有 key 且
        token/扣费非 0」时调用 —— 所以「今天只有 403 / 429 / 无 key 的调用」
        会留下日志却没有用量行。那种部署**完全健康**，此前却被报
        「统计可能没有正常写入」（实测复现过）。判据必须与累计条件对齐。
        """
        now = int(db.time.time())
        # 被拒绝的调用：有日志、无 token、key_id 为 NULL
        for _ in range(5):
            db.execute('INSERT INTO request_logs(ts,key_id,model,status,realm,error) '
                       'VALUES(?,?,?,?,?,?)', (now, None, '', 403, 'cn', 'IP 被拦截'))
        # 只调了 /v1/models：有 key 但 0 token、无扣费
        db.execute('INSERT INTO request_logs(ts,key_id,model,status,realm,'
                   'prompt_tokens,completion_tokens) VALUES(?,?,?,?,?,?,?)',
                   (now, 1, '', 200, 'cn', 0, 0))
        self.assertEqual(db.query_one('SELECT COUNT(*) c FROM usage_daily')['c'], 0,
                         '这些调用本就不该产生用量行')
        h = stats._usage_health(db.day_of(), 0, 'cn')
        self.assertTrue(h['ok'], f'健康部署不该被误报：{h}')

    def test_unscoped_check_counts_everything(self) -> None:
        """不传版本时看全部（传给「不区分版本」的调用方）。"""
        self._log_realm(2, 'cn')
        self._log_realm(3, 'global')
        h = stats._usage_health(db.day_of(), 0)
        self.assertFalse(h['ok'])
        self.assertIn('5', h['detail'], '不限版本时算全部')


if __name__ == '__main__':
    unittest.main()
