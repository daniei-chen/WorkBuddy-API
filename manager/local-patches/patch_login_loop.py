#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：登录页 401 守卫死循环修复。

背景：控制台部署在 /admin/ 子路径（nginx 剥前缀反代），前端 axios 拦截器的
401 守卫用 `pathname.startsWith(basePath+"/login")` 判断"已在登录页"。官方
根路径部署下成立；子路径下登录页地址是 /admin/login/，判不中 → 未登录状态
访问登录页时：页面自检调 /api/me → 401 → 守卫失守 → 强跳 /login →
nginx 302 回 /admin/login/ → 整页重载 → 再 401 → 每秒约 2 轮死循环
（2026-09-27 01:21 首现）。

方案：把守卫的 startsWith 放宽为 includes——"/admin/login/".includes("/login")
成立，登录页不再自跳；会话过期停留在其它页面时仍照常跳登录，行为不变。

维护：apply_patches.sh 启动时自动重施（管理端更新整体替换 web/out 且 chunk
文件名会变，本补丁按代码结构特征动态定位，不写死文件名）。
幂等：已打补丁的 chunk 含 /*wb-login-loop-fixed*/ 标记即跳过。
回退：local-patches/backup/loginloop-<chunk>.orig-* 拷回后刷新页面即可。
用法：patch_login_loop.py
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import sys
import time

OUT_DIR = '/opt/workbuddy-manager/web/out'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'
MARKER = '/*wb-login-loop-fixed*/'

# 401 守卫结构：some(参=>对象.startsWith(参)) || (window.location.href=...
# 变量名随构建可能变化，锚定结构而非具体名字。
GUARD_RE = re.compile(
    r'some\(\s*([A-Za-z_$][\w$]*)\s*=>\s*([A-Za-z_$][\w$]*)\.\s*startsWith\('
    r'\s*([A-Za-z_$][\w$]*)\s*\)\s*\)\s*\|\|\s*\(\s*window\.location\.href\s*='
)


def main() -> int:
    patched = 0
    scanned = 0
    for f in sorted(glob.glob(OUT_DIR + '/_next/static/chunks/*.js')):
        try:
            s = open(f, encoding='utf-8').read()
        except Exception:
            continue
        if MARKER in s:
            patched += 1
            continue
        scanned += 1
        m = GUARD_RE.search(s)
        if not m:
            continue
        seg = m.group(0)
        if '.startsWith(' not in seg:
            continue
        body = s[:m.start()] + seg.replace('.startsWith(', '.includes(') + s[m.end():]
        # 安全检查：startsWith(10)→includes(8) 恰好 -2 字符，多了少了都不落盘
        if len(s) - len(body) != 2:
            print(f'[FAIL] {f}: 替换差异异常，未落盘')
            return 1
        os.makedirs(BACKUP, exist_ok=True)
        bak = os.path.join(BACKUP, 'loginloop-' + os.path.basename(f) + '-' +
                           time.strftime('%Y%m%d-%H%M%S'))
        shutil.copy2(f, bak)
        open(f, 'w', encoding='utf-8').write(MARKER + body)
        patched += 1
        print(f'[OK] 登录守卫已放宽 startsWith→includes: {f}')
        print(f'     备份 -> {bak}')
    if patched:
        print(f'[OK] 登录死循环补丁就位（{patched} 个 chunk）')
    else:
        print('[WARN] 未找到 401 守卫锚点——若官方更新后循环复发，需核对新版守卫结构')
    return 0


if __name__ == '__main__':
    sys.exit(main())
