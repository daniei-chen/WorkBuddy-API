#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：usage_daily 聚合层补上缓存列的写入链（1.0.67+ 计量重写后的适配）。

背景（2026-09-26 定位）：官方 1.0.67 重写了计量写入（_record 自带缓存提取、
add_request_log 写 request_logs），但聚合表 usage_daily 的 bump_usage **不接收
缓存**——升级 1.0.70/71 后旧补丁锚点失效，表现为「命中率冻结在升级前的值」
（request_logs 采集正常、usage_daily 增量为零）。

本补丁三件事：
  1. 增量迁移列表补 usage_daily 的 cache_hit/miss 两列（新库/重建库不再缺列）；
  2. bump_usage 增加 cache 参数并写入聚合表（累加语义，与既有列一致）；
  3. gateway._record 的 bump 调用传入 (cache_hit, cache_miss)。

幂等：检测到 bump_usage 签名含 cache 即跳过。锚点异常中止不写（fail-closed）。
用法：patch_bump_cache.py [db.py 路径] [gateway.py 路径]
"""
from __future__ import annotations

import os
import shutil
import sys
import time

DEFAULT_DB = '/opt/workbuddy-manager/server/db.py'
DEFAULT_GW = '/opt/workbuddy-manager/server/routers/gateway.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'

MARK = 'cache: tuple[int, int] | None = None,'

MIG_OLD = "    ('request_logs', 'cache_write_tokens', 'INTEGER'),\n"
MIG_NEW = ("    ('request_logs', 'cache_write_tokens', 'INTEGER'),\n"
           "    # usage_daily 聚合层的缓存列（本地：命中率按天聚合展示）——\n"
           "    # 官方 1.0.67 的建表不含（官方聚合层无缓存），由本补丁迁移补齐。\n"
           "    ('usage_daily', 'cache_hit_tokens', 'INTEGER NOT NULL DEFAULT 0'),\n"
           "    ('usage_daily', 'cache_miss_tokens', 'INTEGER NOT NULL DEFAULT 0'),\n")

SIG_OLD = ("    credit: float | None = None,\n"
           "    realm: str | None = None,\n"
           ") -> None:\n"
           '    """累计当日用量。')
SIG_NEW = ("    credit: float | None = None,\n"
           "    realm: str | None = None,\n"
           "    cache: tuple[int, int] | None = None,\n"
           ") -> None:\n"
           '    """累计当日用量。')

INS_OLD = ("        'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit, realm) '\n"
           "        'VALUES(?, ?, ?, 1, ?, ?, ?, ?) '\n"
           "        'ON CONFLICT(day, key_id, model, realm) DO UPDATE SET '\n"
           "        '  requests = requests + 1, '\n"
           "        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '\n"
           "        '  completion_tokens = completion_tokens + excluded.completion_tokens, '\n"
           "        '  credit = credit + excluded.credit',\n"
           "        (day, key_id, model, prompt_tokens, completion_tokens, float(credit or 0), r),\n")
INS_NEW = ("    ch, cm = cache if cache else (0, 0)\n"
           "    execute(\n"
           "        'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit, realm, cache_hit_tokens, cache_miss_tokens) '\n"
           "        'VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, ?) '\n"
           "        'ON CONFLICT(day, key_id, model, realm) DO UPDATE SET '\n"
           "        '  requests = requests + 1, '\n"
           "        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '\n"
           "        '  completion_tokens = completion_tokens + excluded.completion_tokens, '\n"
           "        '  credit = credit + excluded.credit, '\n"
           "        '  cache_hit_tokens = cache_hit_tokens + excluded.cache_hit_tokens, '\n"
           "        '  cache_miss_tokens = cache_miss_tokens + excluded.cache_miss_tokens',\n"
           "        (day, key_id, model, prompt_tokens, completion_tokens, float(credit or 0), r, ch, cm),\n"
           "        )\n")

CALL_OLD = "                db.bump_usage(key['id'], model_clean, pt, ct, credit, realm=realm)\n"
CALL_NEW = "                db.bump_usage(key['id'], model_clean, pt, ct, credit, realm=realm, cache=(cache_hit, cache_miss))\n"


def main() -> int:
    dbp = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    gwp = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_GW
    for f in (dbp, gwp):
        if not os.path.exists(f):
            print(f'ERROR: {f} 不存在，跳过')
            return 0
    db_s = open(dbp, encoding='utf-8').read()
    gw_s = open(gwp, encoding='utf-8').read()

    if MARK in db_s:
        print('已含 bump cache 参数，跳过')
        # 官方 1.0.71 的 rebuild INSERT 参数顺序 bug（realm 落进 cache_miss_tokens），
        # 即便聚合列已存在也要修——幂等检测后仍继续做这一步。
        OFFICIAL_ARG_ORDER = (
            "            int(row['ch'] or 0), int(row['cm'] or 0),"
            + chr(10) +
            "            str(row['realm'] or 'cn'))"
        )
        OFFICIAL_FIXED = (
            "            str(row['realm'] or 'cn'),"
            + chr(10) +
            "            int(row['ch'] or 0), int(row['cm'] or 0))"
        )
        OFFICIAL_BULK_ARG_ORDER = (
            "                  int(row['ch'] or 0), int(row['cm'] or 0),"
            + chr(10) +
            "                  str(row['realm'] or 'cn')) for row in expected],"
        )
        OFFICIAL_BULK_FIXED = (
            "                  str(row['realm'] or 'cn'),"
            + chr(10) +
            "                  int(row['ch'] or 0), int(row['cm'] or 0)) for row in expected],"
        )
        if OFFICIAL_BULK_ARG_ORDER in db_s:
            db_s = db_s.replace(OFFICIAL_BULK_ARG_ORDER, OFFICIAL_BULK_FIXED, 1)
            open(dbp, 'w', encoding='utf-8').write(db_s)
            print('已修正官方 rebuild bulk 参数顺序（1.0.71 bug）')
        if OFFICIAL_ARG_ORDER in db_s:
            db_s = db_s.replace(OFFICIAL_ARG_ORDER, OFFICIAL_FIXED, 1)
            open(dbp, 'w', encoding='utf-8').write(db_s)
            print('已修正官方 rebuild 参数顺序（1.0.71 bug）')
        return 0

    for label, old, new, src in (
        ('迁移列表', MIG_OLD, MIG_NEW, db_s),
        ('bump 签名', SIG_OLD, SIG_NEW, db_s),
        ('bump INSERT', INS_OLD, INS_NEW, db_s),
        ('gateway 调用', CALL_OLD, CALL_NEW, gw_s),
    ):
        if src.count(old) != 1:
            print(f'ERROR: {label} 锚点命中 {src.count(old)} 次（应为 1），中止不写')
            return 0

    os.makedirs(BACKUP, exist_ok=True)
    ts = time.strftime('%Y%m%d-%H%M%S')
    for f in (dbp, gwp):
        dst = os.path.join(BACKUP, os.path.basename(f) + '.orig-bumpcache-' + ts)
        if not any(x.startswith(os.path.basename(f) + '.orig-bumpcache') for x in os.listdir(BACKUP)):
            shutil.copy2(f, dst)
            print(f'备份 {f} -> {dst}')
    db_s = db_s.replace(MIG_OLD, MIG_NEW, 1).replace(SIG_OLD, SIG_NEW, 1).replace(INS_OLD, INS_NEW, 1)
    gw_s = gw_s.replace(CALL_OLD, CALL_NEW, 1)
    open(dbp, 'w', encoding='utf-8').write(db_s)
    open(gwp, 'w', encoding='utf-8').write(gw_s)
    print('已应用 usage_daily 缓存写入链补丁')
    return 0


if __name__ == '__main__':
    sys.exit(main())
