#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：修复 backfill / rebuild 用量统计时丢失缓存字段的问题。双版本兼容。

背景（2026-09-19 定位）：request_logs 记录了每笔调用的 cache_hit_tokens /
cache_miss_tokens，但 `backfill_usage_from_logs` 与 `rebuild_usage_from_logs`
从日志聚合重建 usage_daily 时**没有带上这两列**，一旦执行「修复统计 / 重建统计」，
历史行的缓存数据即被清零（表现为「前几天没有命中率」）。

覆盖三个代码世代（1.0.37 / 1.0.57+ / 1.0.79）：
以「小片段」为锚点、逐条应用；缺失的可选锚点自动跳过，不误伤。

2026-10-04 适配 1.0.79：
  * 「聚合 SQL credit 行」由 expect=2 改为 opt —— 1.0.79 新增按桶聚合的
    _usage_expected_rows()，该片段出现 3 次（历史版本 2 次）；替换与上下文无关
    （只给 SELECT 补两列），故「有则全替换、无则跳过」更稳。
  * 「ON CONFLICT」锚点改为只命中 usage_daily 整块。1.0.79 里 credit = MAX(...)
    出现 2 次，第二处属于 usage_hourly —— 该表没有 cache_hit_tokens /
    cache_miss_tokens 列，误改会让小时统计写入直接报 no such column。
  * 末尾自检不再硬编码 2，改为「补上的列对数 == 本次实际替换次数」。

用法：patch_usage_cache.py [db.py 路径]   # 缺省 /opt/workbuddy-manager/...
幂等：检测到已含 cache 差额判断则跳过；异常只记录、返回 0，不阻塞启动。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

DEFAULT_DB = '/opt/workbuddy-manager/server/db.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'

# 规则元组：(old, new, expect, label)
#   expect 为整数 ≥ 1 —— 必须恰好命中该次数，否则整体中止；
#   expect 为 'opt'    —— 可选锚点：0 次跳过（另一代码世代），>0 次全部替换。
RULES = [
    # ── 聚合 SQL 的 credit 行 ──
    #    1.0.37 / 1.0.57：2 处（backfill + rebuild）；1.0.79：3 处（多一个按桶聚合）。
    #    替换与上下文无关（只给 SELECT 补两列），故用 opt；末尾自检核对列对数与替换次数。
    ('        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr "\n',
     '        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr, "\n'
     '        "COALESCE(SUM(cache_hit_tokens),0) AS ch, COALESCE(SUM(cache_miss_tokens),0) AS cm "\n',
     'opt', '聚合 SQL credit 行'),

    # ── backfill：current 查询（两代相同）──
    ("            'SELECT day, key_id, model, realm, requests, prompt_tokens, "
     "completion_tokens, credit FROM usage_daily'\n",
     "            'SELECT day, key_id, model, realm, requests, prompt_tokens, "
     "completion_tokens, credit, cache_hit_tokens, cache_miss_tokens FROM usage_daily'\n",
     1, 'backfill current 查询'),

    # ── backfill：差额判断（纳入缓存列；两代相同）──
    ("        cur_ct = int(cur['completion_tokens']) if cur else 0\n"
     "\n"
     "        d_req = int(row['requests']) - cur_req\n"
     "        d_pt = int(row['pt']) - cur_pt\n"
     "        d_ct = int(row['ct']) - cur_ct\n"
     "        if d_req <= 0 and d_pt <= 0 and d_ct <= 0:\n"
     "            continue\n",
     "        cur_ct = int(cur['completion_tokens']) if cur else 0\n"
     "        cur_ch = int(cur['cache_hit_tokens'] or 0) if cur else 0\n"
     "        cur_cm = int(cur['cache_miss_tokens'] or 0) if cur else 0\n"
     "\n"
     "        d_req = int(row['requests']) - cur_req\n"
     "        d_pt = int(row['pt']) - cur_pt\n"
     "        d_ct = int(row['ct']) - cur_ct\n"
     "        d_ch = int(row['ch']) - cur_ch\n"
     "        d_cm = int(row['cm']) - cur_cm\n"
     "        if d_req <= 0 and d_pt <= 0 and d_ct <= 0 and d_ch <= 0 and d_cm <= 0:\n"
     "            continue\n",
     1, 'backfill 差额判断'),

    # ── INSERT 列清单（单行版；两代出现次数不同：1.0.37 两处 / 1.0.57 一处）──
    ("            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, "
     "completion_tokens, credit, realm) '\n",
     "            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, "
     "completion_tokens, credit, realm, cache_hit_tokens, cache_miss_tokens) '\n",
     'opt', 'INSERT 列清单（单行版）'),

    # ── usage_daily 的 ON CONFLICT 子句 ──
    #    ⚠️ 不能只锚 credit = MAX(...) 那一行：1.0.79 起它同时出现在 usage_hourly 的
    #    upsert 里，而 usage_hourly 没有缓存列，误改会让小时统计写入报 no such column。
    #    故锚点带上 usage_daily 特有的 ON CONFLICT(day, key_id, model, realm) 那几行
    #    （实测 1.0.37 / 1.0.57 / 1.0.79 三代均恰好 1 处）。
    ("            'ON CONFLICT(day, key_id, model, realm) DO UPDATE SET '\n"
     "            '  requests = MAX(requests, excluded.requests), '\n"
     "            '  prompt_tokens = MAX(prompt_tokens, excluded.prompt_tokens), '\n"
     "            '  completion_tokens = MAX(completion_tokens, excluded.completion_tokens), '\n"
     "            '  credit = MAX(credit, excluded.credit)',\n",
     "            'ON CONFLICT(day, key_id, model, realm) DO UPDATE SET '\n"
     "            '  requests = MAX(requests, excluded.requests), '\n"
     "            '  prompt_tokens = MAX(prompt_tokens, excluded.prompt_tokens), '\n"
     "            '  completion_tokens = MAX(completion_tokens, excluded.completion_tokens), '\n"
     "            '  credit = MAX(credit, excluded.credit), '\n"
     "            '  cache_hit_tokens = MAX(cache_hit_tokens, excluded.cache_hit_tokens), '\n"
     "            '  cache_miss_tokens = MAX(cache_miss_tokens, excluded.cache_miss_tokens)',\n",
     1, 'usage_daily ON CONFLICT'),

    # ── VALUES 占位符：backfill（尾随空格；两代相同）──
    ("            'VALUES(?, ?, ?, ?, ?, ?, ?, ?) '\n",
     "            'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '\n",
     1, 'VALUES 占位符（backfill）'),

    # ── VALUES 占位符：1.0.37 的 rebuild（单行逗号结尾）──
    ("            'VALUES(?, ?, ?, ?, ?, ?, ?, ?)',\n",
     "            'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',\n",
     'opt', 'VALUES 占位符（1.0.37 rebuild）'),

    # ── 1.0.57 的 rebuild：跨行 INSERT 列清单 + VALUES ──
    ("                'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, '\n"
     "                'completion_tokens, credit, realm) VALUES(?, ?, ?, ?, ?, ?, ?, ?)',\n",
     "                'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, '\n"
     "                'completion_tokens, credit, realm, cache_hit_tokens, cache_miss_tokens) "
     "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',\n",
     'opt', 'rebuild INSERT（1.0.57）'),

    # ── 参数元组（16 空格版；1.0.37 两处 / 1.0.57 一处）──
    ("            (row['day'], row['key_id'], row['model'], int(row['requests']),\n"
     "             int(row['pt']), int(row['ct']), float(row['cr'] or 0),\n"
     "             str(row['realm'] or 'cn')),\n",
     "            (row['day'], row['key_id'], row['model'], int(row['requests']),\n"
     "             int(row['pt']), int(row['ct']), float(row['cr'] or 0),\n"
     "             int(row['ch'] or 0), int(row['cm'] or 0),\n"
     "             str(row['realm'] or 'cn')),\n",
     'opt', '参数元组（16 空格版）'),

    # ── 参数列表（1.0.57 rebuild 的 executemany 内，18 空格续行）──
    ("                [(row['day'], row['key_id'], row['model'], int(row['requests']),\n"
     "                  int(row['pt']), int(row['ct']), float(row['cr'] or 0),\n"
     "                  str(row['realm'] or 'cn')) for row in expected],\n",
     "                [(row['day'], row['key_id'], row['model'], int(row['requests']),\n"
     "                  int(row['pt']), int(row['ct']), float(row['cr'] or 0),\n"
     "                  int(row['ch'] or 0), int(row['cm'] or 0),\n"
     "                  str(row['realm'] or 'cn')) for row in expected],\n",
     'opt', '参数列表（1.0.57 rebuild）'),
]

MARK = "cache_hit_tokens = MAX(cache_hit_tokens, excluded.cache_hit_tokens)"


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    try:
        with open(target, encoding='utf-8') as fh:
            src = fh.read()
    except OSError as exc:
        print(f'patch_usage_cache: 读取失败: {exc}')
        return 0

    if MARK in src:
        print('patch_usage_cache: 已修复，跳过')
        return 0

    out = src
    hits = 0
    agg_hits = 0        # 「聚合 SQL credit 行」实际替换处数，供末尾自检
    for old, new, expect, label in RULES:
        n = out.count(old)
        if expect == 'opt':
            if n == 0:
                print(f'patch_usage_cache: 跳过可选锚点 {label}（本代码世代无此片段）')
                continue
        elif n != expect:
            print(f'patch_usage_cache: 锚点异常 {label}（期望 {expect} 处，实为 {n} 处），中止不写')
            return 0
        out = out.replace(old, new)
        hits += 1
        if label.startswith('聚合 SQL'):
            agg_hits = n
        print(f'patch_usage_cache: 应用 {label}（{n} 处）')

    if hits == 0 or out == src:
        print('patch_usage_cache: 无需修改')
        return 0

    # 替换后自检：补上的列对数必须与「聚合 SQL」规则的实际替换次数一致
    ch_pairs = out.count('AS ch, COALESCE(SUM(cache_miss_tokens),0) AS cm ')
    if ch_pairs != agg_hits:
        print(f'patch_usage_cache: 自检失败（补上的列对数 {ch_pairs} 与替换次数 {agg_hits} 不一致），不写')
        return 0
    try:
        compile(out, target, 'exec')
    except SyntaxError as exc:
        print(f'patch_usage_cache: 补丁后语法错误，不写：{exc}')
        return 0

    stamp = time.strftime('%Y%m%d-%H%M%S')
    try:
        os.makedirs(BACKUP, exist_ok=True)
        shutil.copy2(target, f'{BACKUP}/db.py.orig-usage-cache-{stamp}')
    except OSError as exc:
        print(f'patch_usage_cache: 备份失败（不阻塞写回）：{exc}')
    with open(target, 'w', encoding='utf-8') as fh:
        fh.write(out)
    print(f'patch_usage_cache: 已写回（{hits} 组规则）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
