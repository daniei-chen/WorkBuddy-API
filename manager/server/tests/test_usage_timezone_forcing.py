"""在 UTC+8 时区下真复现「同一次调用被算成两天」（P0 回归）。

为什么单开一个文件：CI 与很多容器默认跑 UTC，那里「本地日 == UTC 日」，
原缺陷完全不会显现——本文件用**子进程 + TZ=UTC-8**（POSIX 格式，即 UTC+8）强制偏移，
让它在任何机器上都能跑出真实结论。这里刻意不用 `Asia/Shanghai`：Windows 的
C 运行时不认 IANA 时区名，实测会退化成 +0100，导致测试静默跳过。

本质检的是「口径」：修复前的回填用 SQLite 的 date(ts,'unixepoch')（UTC），
而写入用 time.strftime（本地）。两者在 UTC+8 下对同一时间戳会给出不同日期，
于是同一次调用落进两个 day。修复后统一走 db.day_sql（带 localtime）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 在 TZ=UTC-8（即 UTC+8）下执行的检查脚本：
# 1) 本地日与 UTC 日确实不同（证明确实处于偏移时区）
# 2) db.day_of / db.day_sql 跟随本地时区（修复后的口径）
_SNIPPET = textwrap.dedent(
    """
    import sys, time, tempfile, pathlib
    sys.path.insert(0, {root!r})
    from server import config, db

    # 取「本地今天 00:30」——UTC+8 下它的 UTC 日期是前一天
    lt = time.localtime()
    ts = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 30, 0, 0, 0, -1)))
    local_day = time.strftime('%Y-%m-%d', time.localtime(ts))
    utc_day = time.strftime('%Y-%m-%d', time.gmtime(ts))
    assert local_day != utc_day, f'时区未生效: local={{local_day}} utc={{utc_day}}'

    td = tempfile.mkdtemp()
    config.DB_PATH = pathlib.Path(td) / 'tz.db'
    db._conn = None
    db.connect()

    # 口径必须跟随本地，而不是 UTC
    assert db.day_of(ts) == local_day, f'day_of 未跟随本地: {{db.day_of(ts)}}'
    sql_day = db.query_one(f"SELECT {{db.day_sql('ts')}} AS d FROM (SELECT {{ts}} AS ts)")['d']
    assert sql_day == local_day, f'day_sql 未跟随本地: {{sql_day}}'

    # 老写法（UTC）确实会给出不同日期——这正是当初的 bug
    naive = db.query_one(f"SELECT date({{ts}}, 'unixepoch') AS d")['d']
    assert naive == utc_day, f'date(unixepoch) 应为 UTC: {{naive}}'

    # 端到端：写入本地日后再回填，不应产生缺口/第二行
    db.execute(
        'INSERT INTO api_keys(name,key_hash,prefix,enabled,expires_at,max_ips,'
        'ip_allowlist,models,quota,used_tokens,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        ('k','h','wbk_abc',1,None,0,'[]','[]',0,0,ts))
    db.execute(
        'INSERT INTO request_logs(ts,key_id,ip,model,mapped_model,status,'
        'prompt_tokens,completion_tokens,latency_ms,ua,error,stream) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (ts,1,'1.2.3.4','glm-5.2','glm-5.2',200,100,50,10,'ua','',0))
    db.execute(
        'INSERT INTO usage_daily(day,key_id,model,requests,prompt_tokens,completion_tokens) '
        'VALUES(?,?,?,?,?,?)', (db.day_of(ts),1,'glm-5.2',1,100,50))

    res = db.backfill_usage_from_logs()
    assert res['repaired'] == 0, f'UTC+8 下仍产生回填: {{res}}'
    rows = db.query('SELECT day FROM usage_daily')
    assert len(rows) == 1 and rows[0]['day'] == local_day, f'出现多余 day 行: {{[r["day"] for r in rows]}}'
    print('TZ_OK')
    """
)


# POSIX TZ 格式：std offset（offset 向西为正）。UTC-8 == UTC+8。
_TZ_VALUE = 'UTC-8'


def _can_force_tz() -> bool:
    """Windows 也认 TZ 环境变量；仅当真的生效才运行本测试。"""
    try:
        out = subprocess.run(
            [sys.executable, '-c',
             "import time;print(time.strftime('%z', time.localtime()))"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'TZ': _TZ_VALUE},
        )
    except Exception:  # noqa: BLE001
        return False
    return out.stdout.strip() in ('+0800',)


@unittest.skipUnless(_can_force_tz(), '当前环境无法通过 TZ 强制时区（需要 +0800 生效）')
class ForcedTimezone(unittest.TestCase):
    def test_no_double_count_in_utc_plus_8(self) -> None:
        code = _SNIPPET.format(root=str(ROOT))
        proc = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True, text=True, timeout=120,
            cwd=str(ROOT),
            env={**os.environ, 'TZ': _TZ_VALUE},
        )
        self.assertEqual(
            proc.returncode, 0,
            f'UTC+8 复现失败：\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}',
        )
        self.assertIn('TZ_OK', proc.stdout)


if __name__ == '__main__':
    unittest.main()
