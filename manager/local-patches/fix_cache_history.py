#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性恢复：按 request_logs 重算 usage_daily 的缓存列。

背景：backfill/rebuild 曾丢失缓存列（修复见 patch_usage_cache.py），
导致 2026-09-16 ~ 09-18 的缓存命中数据被清零、今日仅剩重建后的增量。
请求日志中数据完整，这里以日志为准回写两列（**只动这两列**，其余字段不动）。

安全：执行前用 VACUUM INTO 备份整库；幂等（可重复执行，结果一致）。
用法：fix_cache_history.py [manager.db 路径]
"""
from __future__ import annotations

import sqlite3
import sys
import time

DEFAULT_DB = '/opt/workbuddy-manager/data/manager.db'

MATCH = """
  r.key_id = usage_daily.key_id
  AND COALESCE(r.model,'') = usage_daily.model
  AND COALESCE(r.realm,'cn') = usage_daily.realm
  AND strftime('%Y-%m-%d', r.ts, 'unixepoch', 'localtime') = usage_daily.day
"""


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = f'{db_path}.bak-cachefix-{stamp}'

    con = sqlite3.connect(db_path, timeout=30)
    con.execute('PRAGMA busy_timeout = 30000')
    cur = con.cursor()

    cur.execute('VACUUM INTO ?', (bak,))
    print(f'备份完成: {bak}')

    before = cur.execute(
        'SELECT day, SUM(cache_hit_tokens), SUM(cache_miss_tokens) FROM usage_daily '
        'GROUP BY day ORDER BY day'
    ).fetchall()
    print('恢复前:')
    for day, h, m in before:
        d = (h or 0) + (m or 0)
        print(f'  {day}: hit={h:,} miss={m:,} rate={(f"{h/d*100:.1f}%" if d else "—")}')

    cur.execute(f"""
        UPDATE usage_daily SET
          cache_hit_tokens = COALESCE((
            SELECT SUM(r.cache_hit_tokens) FROM request_logs r WHERE {MATCH}
          ), 0),
          cache_miss_tokens = COALESCE((
            SELECT SUM(r.cache_miss_tokens) FROM request_logs r WHERE {MATCH}
          ), 0)
    """)
    con.commit()
    print(f'已按请求日志重算 {cur.rowcount} 行')

    after = cur.execute(
        'SELECT day, SUM(cache_hit_tokens), SUM(cache_miss_tokens) FROM usage_daily '
        'GROUP BY day ORDER BY day'
    ).fetchall()
    print('恢复后:')
    for day, h, m in after:
        d = (h or 0) + (m or 0)
        print(f'  {day}: hit={h:,} miss={m:,} rate={(f"{h/d*100:.1f}%" if d else "—")}')

    # 对账：usage 与 logs 的缓存总量应完全一致
    logs = cur.execute(
        "SELECT COALESCE(SUM(cache_hit_tokens),0), COALESCE(SUM(cache_miss_tokens),0) "
        "FROM request_logs WHERE key_id IS NOT NULL"
    ).fetchone()
    usage = cur.execute(
        'SELECT COALESCE(SUM(cache_hit_tokens),0), COALESCE(SUM(cache_miss_tokens),0) '
        'FROM usage_daily'
    ).fetchone()
    print()
    print(f'对账: usage={usage}  logs={logs}  '
          f'{"一致 ✓" if tuple(usage) == tuple(logs) else "不一致 ✗"}')
    con.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
