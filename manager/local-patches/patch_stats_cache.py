#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：/api/stats 系列接口的缓存字段。双版本兼容（1.0.37 / 1.0.57+）。

覆盖：
  * `/daily`     —— SQL 与返回增查 cache_hit/miss；
      · 1.0.37 的返回块（无 failed）；
      · 1.0.57+ 的返回块（含 `'failed': failures.get(...)`，按各自锚点匹配）。
  * `/by-model`、`/by-key` —— SQL 与返回增查 cache_hit/miss。

逐条应用：缺失的可选锚点自动跳过（另一代码世代 / 已应用），
任何计数异常即中止不写；异常只记录、返回 0，不阻塞服务启动。
用法：patch_stats_cache.py [stats.py 路径]
"""
from __future__ import annotations

import os
import shutil
import sys
import time

DEFAULT_STATS = '/opt/workbuddy-manager/server/routers/stats.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'

# 规则元组：(old, new, expect, label)
#   expect 为整数 ≥ 1 —— 必须恰好命中该次数，否则整体中止；
#   expect 为 'opt'    —— 可选锚点：0 次跳过（另一代码世代/已应用），>0 次替换。
RULES = [
    # ── daily：SQL（两代相同）──
    ("        'SELECT day, SUM(requests) AS requests, '\n"
     "        'SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, '\n"
     "        'COALESCE(SUM(credit),0) AS credit '\n"
     "        f'FROM usage_daily WHERE day >= ?{rf} GROUP BY day ORDER BY day ASC',\n",
     "        'SELECT day, SUM(requests) AS requests, '\n"
     "        'SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, '\n"
     "        'COALESCE(SUM(credit),0) AS credit, '\n"
     "        'COALESCE(SUM(cache_hit_tokens),0) AS cache_hit_tokens, '\n"
     "        'COALESCE(SUM(cache_miss_tokens),0) AS cache_miss_tokens '\n"
     "        f'FROM usage_daily WHERE day >= ?{rf} GROUP BY day ORDER BY day ASC',\n",
     'opt', 'daily SQL'),

    # ── daily：返回块（1.0.37，无 failed）──
    ("            'day': r['day'],\n"
     "            'requests': int(r['requests'] or 0),\n"
     "            'prompt_tokens': int(r['prompt_tokens'] or 0),\n"
     "            'completion_tokens': int(r['completion_tokens'] or 0),\n"
     "            'credit': float(r['credit'] or 0),\n"
     "        }\n"
     "        for r in rows\n",
     "            'day': r['day'],\n"
     "            'requests': int(r['requests'] or 0),\n"
     "            'prompt_tokens': int(r['prompt_tokens'] or 0),\n"
     "            'completion_tokens': int(r['completion_tokens'] or 0),\n"
     "            'credit': float(r['credit'] or 0),\n"
     "            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),\n"
     "            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),\n"
     "        }\n"
     "        for r in rows\n",
     'opt', 'daily 返回（1.0.37）'),

    # ── daily：返回块（1.0.57+，含 failed）──
    ("            'credit': float(r['credit'] or 0),\n"
     "            'failed': failures.get(r['day'], 0),\n"
     "        }\n"
     "        for r in rows\n",
     "            'credit': float(r['credit'] or 0),\n"
     "            'failed': failures.get(r['day'], 0),\n"
     "            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),\n"
     "            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),\n"
     "        }\n"
     "        for r in rows\n",
     'opt', 'daily 返回（1.0.57）'),

    # ── by-model：SQL（两代相同）──
    ("        'COALESCE(SUM(credit),0) AS credit '\n"
     "        f'FROM usage_daily WHERE day >= ?{rf} GROUP BY model ORDER BY SUM(prompt_tokens + completion_tokens) DESC',\n",
     "        'COALESCE(SUM(credit),0) AS credit, '\n"
     "        'COALESCE(SUM(cache_hit_tokens),0) AS cache_hit_tokens, '\n"
     "        'COALESCE(SUM(cache_miss_tokens),0) AS cache_miss_tokens '\n"
     "        f'FROM usage_daily WHERE day >= ?{rf} GROUP BY model ORDER BY SUM(prompt_tokens + completion_tokens) DESC',\n",
     'opt', 'by-model SQL'),

    # ── by-key：SQL（两代相同）──
    ("        'COALESCE(SUM(u.credit),0) AS credit '\n"
     "        'FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id '\n",
     "        'COALESCE(SUM(u.credit),0) AS credit, '\n"
     "        'COALESCE(SUM(u.cache_hit_tokens),0) AS cache_hit_tokens, '\n"
     "        'COALESCE(SUM(u.cache_miss_tokens),0) AS cache_miss_tokens '\n"
     "        'FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id '\n",
     'opt', 'by-key SQL'),

    # ── by-model / by-key 的返回块（两处相同，两代相同）──
    ("            'name': r['name'] or '未知',\n"
     "            'requests': int(r['requests'] or 0),\n"
     "            'prompt_tokens': int(r['prompt_tokens'] or 0),\n"
     "            'completion_tokens': int(r['completion_tokens'] or 0),\n"
     "            'credit': float(r['credit'] or 0),\n"
     "        }\n"
     "        for r in rows\n",
     "            'name': r['name'] or '未知',\n"
     "            'requests': int(r['requests'] or 0),\n"
     "            'prompt_tokens': int(r['prompt_tokens'] or 0),\n"
     "            'completion_tokens': int(r['completion_tokens'] or 0),\n"
     "            'credit': float(r['credit'] or 0),\n"
     "            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),\n"
     "            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),\n"
     "        }\n"
     "        for r in rows\n",
     'opt', 'by-model/by-key 返回'),
]

MARK = "COALESCE(SUM(cache_hit_tokens),0) AS cache_hit_tokens"


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STATS
    try:
        with open(target, encoding='utf-8') as fh:
            src = fh.read()
    except OSError as exc:
        print(f'patch_stats_cache: 读取失败: {exc}')
        return 0

    out = src
    hits = 0
    for old, new, expect, label in RULES:
        n = out.count(old)
        if n == 0:
            print(f'patch_stats_cache: 跳过 {label}（本代码世代无此片段或已应用）')
            continue
        if expect != 'opt' and n != expect:
            print(f'patch_stats_cache: 锚点异常 {label}（期望 {expect} 处，实为 {n} 处），中止不写')
            return 0
        out = out.replace(old, new)
        hits += 1
        print(f'patch_stats_cache: 应用 {label}（{n} 处）')

    if hits == 0 or out == src:
        print('patch_stats_cache: 无需修改')
        return 0

    # 自检：daily + by-model + by-key 三处返回块都应带上 cache 字段
    if out.count("'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),") < 3:
        print('patch_stats_cache: 自检失败（返回块少于 3 处），不写')
        return 0
    try:
        compile(out, target, 'exec')
    except SyntaxError as exc:
        print(f'patch_stats_cache: 补丁后语法错误，不写：{exc}')
        return 0

    stamp = time.strftime('%Y%m%d-%H%M%S')
    try:
        os.makedirs(BACKUP, exist_ok=True)
        shutil.copy2(target, f'{BACKUP}/stats.py.orig-cache-v3-{stamp}')
    except OSError as exc:
        print(f'patch_stats_cache: 备份失败（不阻塞写回）：{exc}')
    with open(target, 'w', encoding='utf-8') as fh:
        fh.write(out)
    print(f'patch_stats_cache: 已写回（{hits} 组规则）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
