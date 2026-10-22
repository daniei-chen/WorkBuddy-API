#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：前端界面定制（v1.0.71 重写版）。

1.0.70/71 官方前端已原生实现以下旧定制（无需再打）：
  - 统计页命中率列：消费 wb2api /v1/stats 的 cache_hit_rate（官方后端原生输出）；
  - 角色显示名（"管理员（可修改）"等文案官方已含）；
  - 测试台页面本身对只读角色无门控（配合 patch_viewer_open 的后端放开）。

本补丁仅剩一项：playground 的 locale 描述「仅管理员可用」已过时——
后端已放开只读角色可用，改为中性文案，避免只读用户被文案劝退。

对 _next/static/chunks/*.js 全量扫描替换（新版构建后 chunk 名变化仍可用）。
幂等：检测新文案已存在即跳过。找不到目标文案则跳过（fail-open，不阻塞启动）。
用法：patch_zhihui_ui.py
"""
from __future__ import annotations

import glob
import os
import shutil
import sys
import time

OUT_DIR = '/opt/workbuddy-manager/web/out'
BRAND_OLD = 'workbuddy2api'
BRAND_NEW = 'WorkBuddy API'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'
OLD = '真实消耗积分，仅管理员可用'
NEW = '真实消耗积分，管理员与只读账号均可使用'


def main() -> int:
    done = False
    for f in sorted(glob.glob(OUT_DIR + '/_next/static/chunks/*.js')):
        try:
            s = open(f, encoding='utf-8').read()
        except Exception:
            continue
        if BRAND_OLD in s:
            s2 = s.replace(BRAND_OLD, BRAND_NEW)
            os.makedirs(BACKUP, exist_ok=True)
            bak = os.path.join(BACKUP, 'brand-' + os.path.basename(f) + '-' + time.strftime('%Y%m%d-%H%M%S'))
            if not done:
                shutil.copy2(f, bak)
                print(f'备份 {f} -> {bak}')
            open(f, 'w', encoding='utf-8').write(s2)
            done = True
            print(f'品牌文案已统一: {f}')
            continue
        if NEW in s:
            done = True
            continue
        if OLD in s:
            os.makedirs(BACKUP, exist_ok=True)
            bak = os.path.join(BACKUP, 'ui-' + os.path.basename(f) + '-' + time.strftime('%Y%m%d-%H%M%S'))
            if not done:  # 同一批只备份一次
                shutil.copy2(f, bak)
                print(f'备份 {f} -> {bak}')
            open(f, 'w', encoding='utf-8').write(s.replace(OLD, NEW, 1))
            done = True
            print(f'已替换文案: {f}')
    if not done:
        print('未找到目标文案（官方可能已更新），跳过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
