#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：只读角色开放聊天测试台 + 角色改名配套文案。

改动点（2026-09-19）：
  1) server/routers/playground.py
     /api/playground/chat 的登录依赖 require_admin -> current_user，
     使只读角色（viewer）也可使用聊天测试台。
     （界面侧解锁由同目录 patch_zhihui_ui.py 负责）
  2) server/routers/settings.py
     错误文案「至少保留一个管理员」->「至少保留一个最高管理员」，
     与前端角色显示名（管理员 -> 最高管理员）配套。

幂等：已是目标状态时跳过；任何异常只记录、返回 0，不阻塞服务启动。
"""
from __future__ import annotations

import re
import shutil
import time

BASE = '/opt/workbuddy-manager/server/routers'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'

PLAYGROUND = f'{BASE}/playground.py'
SETTINGS = f'{BASE}/settings.py'


def _patch_playground() -> str:
    try:
        with open(PLAYGROUND, encoding='utf-8') as fh:
            src = fh.read()
    except OSError as exc:
        return f'playground.py: 读取失败: {exc}'

    # 单行签名内匹配 require_admin（签名参数若微调也能命中）
    pat = re.compile(r'async def chat\([^\n]*security\.require_admin[^\n]*\):')
    m = pat.search(src)
    if not m:
        if 'async def chat(' in src:
            return 'playground.py: 跳过（chat 已是 current_user）'
        return 'playground.py: 跳过（未找到 chat 函数，需人工检查）'

    stamp = time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(PLAYGROUND, f'{BACKUP}/playground.py.orig-viewer-open-{stamp}')
    new_src = (
        src[:m.start()]
        + m.group(0).replace('security.require_admin', 'security.current_user')
        + src[m.end():]
    )
    with open(PLAYGROUND, 'w', encoding='utf-8') as fh:
        fh.write(new_src)
    return 'playground.py: 已放开 chat（require_admin -> current_user）'


def _patch_settings() -> str:
    try:
        with open(SETTINGS, encoding='utf-8') as fh:
            src = fh.read()
    except OSError as exc:
        return f'settings.py: 读取失败: {exc}'

    old = "detail='至少保留一个管理员'"
    new = "detail='至少保留一个最高管理员'"
    n = src.count(old)
    if n == 0:
        return 'settings.py: 跳过（文案已是最高管理员或结构变化）'

    stamp = time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(SETTINGS, f'{BACKUP}/settings.py.orig-role-rename-{stamp}')
    with open(SETTINGS, 'w', encoding='utf-8') as fh:
        fh.write(src.replace(old, new))
    return f'settings.py: 已改名 {n} 处'


def main() -> int:
    for fn in (_patch_playground, _patch_settings):
        try:
            print(f'patch_viewer_open: {fn()}')
        except Exception as exc:  # noqa: BLE001
            print(f'patch_viewer_open: 异常（已忽略）: {exc}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
