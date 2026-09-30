"""首字延迟（time-to-first-token）采集的回归测试。

背景：日志页原先只有「延迟」一列，它是**整条响应**的墙钟时长——流式请求
要等模型把全部内容生成完才结束，因此回答越长数字越大，反映不出上游响应
快慢。为此新增「首字」列，记录从发起上游请求到**第一个含正文的 delta**
到达的时间。

两个容易做错的点在这里锁住：
  1. 不能把「收到第一块 SSE」当成首字——OpenAI 流的第一块通常只有 role、
     content 为空，那记的是建连时间，数字会偏小且失真。
  2. 首字只记一次；后续 delta 不能覆盖它。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway  # noqa: E402


def _data(obj) -> str:
    import json
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n'


ROLE_ONLY = _data({'choices': [{'delta': {'role': 'assistant', 'content': ''}}]})
CONTENT_A = _data({'choices': [{'delta': {'content': '你'}}]})
CONTENT_B = _data({'choices': [{'delta': {'content': '好'}}]})
REASONING = _data({'choices': [{'delta': {'reasoning_content': '想想'}}]})
USAGE = _data({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'credit': 0.5}})
DONE = 'data: [DONE]\n'


class ScanSseTest(unittest.TestCase):
    def test_role_only_chunk_is_not_first_content(self) -> None:
        """只带 role 的首块不算正文——否则首字延迟会少算建连后的等待。"""
        pending, saw = gateway._scan_sse(ROLE_ONLY, {})
        self.assertFalse(saw)
        self.assertEqual(pending, '')

    def test_content_chunk_is_first_content(self) -> None:
        _, saw = gateway._scan_sse(CONTENT_A, {})
        self.assertTrue(saw)

    def test_reasoning_content_counts_as_content(self) -> None:
        """推理模型先吐 reasoning_content，那同样是「上游开始回话」。"""
        _, saw = gateway._scan_sse(REASONING, {})
        self.assertTrue(saw)

    def test_usage_extracted_alongside(self) -> None:
        usage: dict = {}
        _, saw = gateway._scan_sse(USAGE, usage)
        self.assertFalse(saw)
        self.assertEqual(usage['prompt_tokens'], 10)
        self.assertEqual(usage['credit'], 0.5)

    def test_incomplete_line_is_buffered(self) -> None:
        """分块截断时残留要留在缓冲区，不能丢。"""
        partial = CONTENT_A.rstrip('\n')
        pending, saw = gateway._scan_sse(partial, {})
        self.assertEqual(pending, partial)
        self.assertFalse(saw)

    def test_buffer_completes_across_chunks(self) -> None:
        partial = CONTENT_A.rstrip('\n')
        pending, saw = gateway._scan_sse(partial, {})
        self.assertFalse(saw)
        pending, saw = gateway._scan_sse(pending + '\n' + DONE, {})
        self.assertTrue(saw)
        self.assertEqual(pending, '')

    def test_done_and_blank_lines_ignored(self) -> None:
        _, saw = gateway._scan_sse(DONE + '\n\n\n', {})
        self.assertFalse(saw)

    def test_non_json_line_is_skipped_not_fatal(self) -> None:
        _, saw = gateway._scan_sse('data: {not json\n', {})
        self.assertFalse(saw)

    def test_multiple_lines_in_one_chunk(self) -> None:
        """一个 TCP 块里可能同时含 role 块与正文块，必须识别出正文。"""
        _, saw = gateway._scan_sse(ROLE_ONLY + CONTENT_A + CONTENT_B, {})
        self.assertTrue(saw)

    def test_message_form_also_detected(self) -> None:
        """非流式风格 message 也可能出现在某些实现的流里。"""
        line = _data({'choices': [{'message': {'content': '嗨'}}]})
        _, saw = gateway._scan_sse(line, {})
        self.assertTrue(saw)


class RecordFirstTokenTest(unittest.TestCase):
    """_record 必须把 first_token 存进库，且缺省为 NULL 而不是 0。"""

    def setUp(self) -> None:
        import tempfile

        from server import config, db
        self._db = db
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'ft.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        from server import config
        if self._db._conn is not None:
            self._db._conn.close()
        self._db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_first_token_stored(self) -> None:
        gateway._record(None, '1.2.3.4', 'm', 'm', 200, 1, 1, 5000, 'ua', None, True, first_token=820)
        row = self._db.query_one('SELECT latency_ms, first_token_ms, stream FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertEqual(row['first_token_ms'], 820)
        self.assertEqual(row['latency_ms'], 5000)
        self.assertEqual(row['stream'], 1)

    def test_absent_first_token_is_null_not_zero(self) -> None:
        """非流式请求不传该参数 → NULL。用 0 会被误读成「瞬间返回」。"""
        gateway._record(None, '1.2.3.4', 'm', 'm', 200, 1, 1, 300, 'ua', None, False)
        row = self._db.query_one('SELECT first_token_ms FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertIsNone(row['first_token_ms'])


class MigrationTest(unittest.TestCase):
    """升级路径：已有旧库必须补列成功，历史记录保留且首字为 NULL。

    SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，因此这条
    路径必须由显式 ALTER 覆盖——否则升级后一查 first_token_ms 就报
    no such column，日志页整页失败。
    """

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / 'old.db'

    def tearDown(self) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        # 连接关闭后 Windows 才允许删除；失败也不影响结论
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _make_old_db(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self._path)
        conn.executescript(
            'CREATE TABLE request_logs ('
            '  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key_id INTEGER,'
            '  ip TEXT, model TEXT, mapped_model TEXT, status INTEGER DEFAULT 0,'
            '  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,'
            '  latency_ms INTEGER DEFAULT 0, ua TEXT, error TEXT, stream INTEGER DEFAULT 0,'
            '  credit REAL);'
        )
        conn.execute('INSERT INTO request_logs(ts, latency_ms) VALUES(1, 1234)')
        conn.commit()
        conn.close()
        cols = {r[1] for r in sqlite3.connect(
            self._path).execute('PRAGMA table_info(request_logs)')}
        self.assertNotIn('first_token_ms', cols)

    def test_old_db_gains_column_and_keeps_rows(self) -> None:
        from server import config, db
        self._orig = config.DB_PATH
        self._make_old_db()
        config.DB_PATH = self._path
        db._conn = None
        db.connect()

        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')}
        self.assertIn('first_token_ms', cols)
        row = db.query_one('SELECT latency_ms, first_token_ms FROM request_logs WHERE id = 1')
        self.assertEqual(row['latency_ms'], 1234)
        self.assertIsNone(row['first_token_ms'])

    def test_migration_is_idempotent(self) -> None:
        from server import config, db
        self._orig = config.DB_PATH
        self._make_old_db()
        config.DB_PATH = self._path
        for _ in range(2):
            db._conn = None
            db.connect()  # 重复执行不能报错
        cols = [r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')]
        self.assertEqual(cols.count('first_token_ms'), 1)


if __name__ == '__main__':
    unittest.main()
