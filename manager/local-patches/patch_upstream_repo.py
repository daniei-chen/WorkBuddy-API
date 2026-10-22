#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：上游更新检查指向自己的 fork（原 Sliverkiss/workbuddy2api 已 404）。

幂等；回退：local-patches/backup/updater.py.orig-repo-* 拷回后重启。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

TARGET = '/opt/workbuddy-manager/server/services/updater.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'
OLD = "or 'Sliverkiss/workbuddy2api'"
NEW = "or 'daniei-chen/WorkBuddy-API'"


def main() -> int:
    try:
        text = open(TARGET, encoding='utf-8').read()
    except FileNotFoundError:
        print('[skip] updater.py 不存在')
        return 0
    if NEW in text:
        print('[skip] 上游检查已指向 fork')
        return 0
    if OLD not in text:
        print('[FAIL] 找不到锚点，未落盘')
        return 1
    out = text.replace(OLD, NEW, 1)
    try:
        compile(out, TARGET, 'exec')
    except SyntaxError as exc:
        print(f'[FAIL] 补丁后语法错误，未落盘：{exc}')
        return 1
    os.makedirs(BACKUP, exist_ok=True)
    bak = os.path.join(BACKUP, 'updater.py.orig-repo-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(TARGET, bak)
    open(TARGET, 'w', encoding='utf-8').write(out)
    print('[OK] 上游更新检查 -> daniei-chen/WorkBuddy-API')
    print(f'     备份 -> {bak}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
