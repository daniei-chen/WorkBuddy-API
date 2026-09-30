#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：Manager->上游(wb2api) 的 httpx 超时默认 120s -> 300s。

背景：wb2api 单次上游尝试看门狗 60s，失败换号；两个 60s ~= 120s，正好撞上
Manager 的 120s 超时被掐断（日志出现 `rotate backoff aborted: ctx cancelled`），
客户端收到 502。放宽到 300s 让换号重试跑完。nginx proxy_read_timeout 已是 600s。

幂等：已是 300 时跳过。
回退：local-patches/backup/config.py.orig-timeout-* 拷回后重启服务。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

TARGET = '/opt/workbuddy-manager/server/config.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'
OLD = "UPSTREAM_TIMEOUT = _env_int('WB_UPSTREAM_TIMEOUT', 120)"
NEW = "UPSTREAM_TIMEOUT = _env_int('WB_UPSTREAM_TIMEOUT', 300)"


def main() -> int:
    try:
        text = open(TARGET, encoding='utf-8').read()
    except FileNotFoundError:
        print('[skip] config.py 不存在')
        return 0
    if NEW in text:
        print('[skip] 已是 300s 默认值')
        return 0
    if OLD not in text:
        print('[FAIL] 找不到 UPSTREAM_TIMEOUT 默认值锚点，未落盘')
        return 1
    out = text.replace(OLD, NEW, 1)
    try:
        compile(out, TARGET, 'exec')
    except SyntaxError as exc:
        print(f'[FAIL] 补丁后语法错误，未落盘：{exc}')
        return 1
    os.makedirs(BACKUP, exist_ok=True)
    bak = os.path.join(BACKUP, 'config.py.orig-timeout-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(TARGET, bak)
    open(TARGET, 'w', encoding='utf-8').write(out)
    print('[OK] UPSTREAM_TIMEOUT 默认值 120 -> 300')
    print(f'     备份 -> {bak}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
