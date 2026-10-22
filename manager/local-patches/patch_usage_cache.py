#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：修复 backfill / rebuild 用量统计时丢失缓存字段的问题。双版本兼容。

背景（2026-09-19 定位）：request_logs 记录了每笔调用的 cache_hit_tokens /
cache_miss_tokens，但 `backfill_usage_from_logs` 与 `rebuild_usage_from_logs`
从日志聚合重建 usage_daily 时**没有带上这两列**，一旦执行「修复统计 / 重建统计」，
历史行的缓存数据即被清零（表现为「前几天没有命中率」）。

覆盖两个代码世代（1.0.37 与 1.0.57+，重构后 rebuild 改走事务化 executemany）：
以「小片段」为锚点、逐条应用；缺失的可选锚点自动跳过，不误伤。

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
    # ── 聚合 SQL 的 credit 行（backfill + rebuild 各一处；两代相同）──
    ('        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr "\n',
     '        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr, "\n'
     '        "COALESCE(SUM(cache_hit_tokens),0) AS ch, COALESCE(SUM(cache_miss_tokens),0) AS cm "\n',
     2, '聚合 SQL credit 行'),

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

    # ── backfill：ON CONFLICT 子句（两代相同）──
    ("            '  credit = MAX(credit, excluded.credit)',\n",
     "            '  credit = MAX(credit, excluded.credit), '\n"
     "            '  cache_hit_tokens = MAX(cache_hit_tokens, excluded.cache_hit_tokens), '\n"
     "            '  cache_miss_tokens = MAX(cache_miss_tokens, excluded.cache_miss_tokens)',\n",
     1, 'backfill ON CONFLICT'),

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
        print(f'patch_usage_cache: 应用 {label}（{n} 处）')

    if hits == 0 or out == src:
        print('patch_usage_cache: 无需修改')
        return 0

    # 替换后自检：关键特征必须到位，否则视为半套、不落盘
    if out.count('AS ch, COALESCE(SUM(cache_miss_tokens),0) AS cm ') != 2:
        print('patch_usage_cache: 自检失败（聚合列未成对出现），不写')
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
