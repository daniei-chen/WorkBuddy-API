#!/usr/bin/env python3
"""本地补丁：只读（viewer）账号可用「实时积分查询」与「添加账号」。幂等。

背景：管理台默认把「添加账号」（POST /api/auth/start、GET /api/auth/poll）与
两个实时积分接口（GET /api/accounts/{file}/credits、POST /api/accounts/
refresh-credits）限定为 admin —— 只读账号看不到实时余额，也无法添加账号。
业务要求：只读账号也能看实时积分、也能添加账号（加入共享池）；其余写操作
（签到 / 测活 / 删除 / 强制重启 / 密钥 / 设置等）保持仅 admin。

方案：仅把这 4 个端点签名里的 Depends(security.require_admin) 换成
Depends(security.current_user)。按函数名精确匹配签名块，不碰其它端点。

维护：由同目录 apply_patches.sh 在服务启动时自动重施（管理端更新会覆盖
server/，靠它恢复）。回退：
  cp backup/accounts.py.orig-viewer-perms-* server/routers/accounts.py
  然后 systemctl restart workbuddy-web
"""
import re
import shutil
import time

ACCOUNTS = '/opt/workbuddy-manager/server/routers/accounts.py'
BACKUP_DIR = '/opt/workbuddy-manager/local-patches/backup'
FUNCS = ('auth_start', 'auth_poll', 'account_credits', 'refresh_all_credits')
OLD = 'Depends(security.require_admin)'
NEW = 'Depends(security.current_user)'


def main() -> int:
    try:
        with open(ACCOUNTS, encoding='utf-8') as f:
            src = f.read()
    except OSError as exc:
        print(f'patch_viewer_perms: 读取失败: {exc}')
        return 0  # 不阻塞服务启动

    orig = src
    changed = []
    skipped = []
    for fn in FUNCS:
        m = re.search(r'async def ' + fn + r'\([\s\S]*?\)\s*->\s*dict:', src)
        if not m:
            skipped.append(f'{fn}(未找到)')
            continue
        sig = m.group(0)
        if OLD not in sig:
            skipped.append(f'{fn}(已是 current_user)')
            continue
        src = src[:m.start()] + sig.replace(OLD, NEW) + src[m.end():]
        changed.append(fn)

    if src == orig:
        note = ('; 跳过: ' + ', '.join(skipped)) if skipped else ''
        print('patch_viewer_perms: 无需修改' + note)
        return 0

    stamp = time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(ACCOUNTS, f'{BACKUP_DIR}/accounts.py.orig-viewer-perms-{stamp}')
    with open(ACCOUNTS, 'w', encoding='utf-8') as f:
        f.write(src)
    note = ('; 跳过: ' + ', '.join(skipped)) if skipped else ''
    print('patch_viewer_perms: 已放开 ' + ', '.join(changed) + note)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
