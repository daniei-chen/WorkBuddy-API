#!/usr/bin/env python3
"""本地补丁：会话键注入（提升上游缓存命中）。幂等。

背景：ZCode 等 OpenAI 兼容客户端 body 不带 conversation_id，manager 转发又不
透传 x-session-id 头 → 上游会话粘性路由不启动、每请求随机换号，
prompt_cache_key（wb2a-<uid8>-<会话>）随账号变化 → 上游前缀缓存全 miss。
方案：把客户端 x-session-id（会话内稳定）注入 body 的 conversation_id，令
「同会话 → 同账号 → 稳定缓存键」生效。客户端已带会话键时不覆盖。
维护：由同目录 apply_patches.sh 在服务启动时自动重施（管理端更新会覆盖
server/，靠它恢复）。回退：cp backup/gateway.py.orig-* gateway.py 后重启。
"""
import os
import shutil
import sys

TARGET = '/opt/workbuddy-manager/server/routers/gateway.py'

HELPER = '''# ── 本地补丁：会话键注入（提升上游缓存命中）────────────────
# 背景：ZCode 等 OpenAI 兼容客户端 body 不带 conversation_id，manager 转发又不
# 透传 x-session-id 头 → 上游会话粘性路由不启动、每请求随机换号，
# prompt_cache_key（wb2a-<uid8>-<会话>）随账号变化 → 上游前缀缓存全 miss。
# 方案：把客户端 x-session-id（会话内稳定）注入 body 的 conversation_id，令
# 「同会话 → 同账号 → 稳定缓存键」生效。客户端已带会话键时不覆盖。
# 维护：由 /opt/workbuddy-manager/local-patches/apply_patches.sh 在服务启动时
# 自动重施（管理端更新会覆盖 server/，靠它恢复）。
_SESSION_HEADERS = ('x-session-id',)


def _inject_session_key(body: 'dict', request: Request) -> None:
    if not isinstance(body, dict):
        return
    if body.get('conversation_id') or body.get('conversationId'):
        return
    meta = body.get('metadata')
    if isinstance(meta, dict) and (meta.get('conversation_id') or meta.get('conversationId')):
        return
    for name in _SESSION_HEADERS:
        v = (request.headers.get(name) or '').strip()
        if v:
            body['conversation_id'] = v
            return


'''

DEF_ANCHOR = 'async def _chat(request: Request, upstream_path: str):'
CALL_OLD = (
    'async def _chat(request: Request, upstream_path: str):\n'
    '    body, err = await _read_json_body(request)\n'
    '    if err:\n'
    '        return err\n'
)
CALL_NEW = (
    'async def _chat(request: Request, upstream_path: str):\n'
    '    body, err = await _read_json_body(request)\n'
    '    if err:\n'
    '        return err\n'
    '\n'
    '    _inject_session_key(body, request)\n'
)

MARKER = '_inject_session_key'


def patch(path: str) -> int:
    if not os.path.isfile(path):
        print(f'[skip] {path} 不存在（更新换版中？）')
        return 0
    text = open(path, encoding='utf-8').read()
    if MARKER in text:
        print('[skip] 已含补丁标记')
        return 0
    if text.count(DEF_ANCHOR) != 1 or text.count(CALL_OLD) != 1:
        print('[FAIL] 锚点不唯一/缺失（上游代码可能已变），未打补丁')
        return 1
    text = text.replace(DEF_ANCHOR, HELPER + DEF_ANCHOR)
    text = text.replace(CALL_OLD, CALL_NEW)
    try:
        compile(text, path, 'exec')
    except SyntaxError as exc:
        print(f'[FAIL] 补丁后语法错误，未落盘：{exc}')
        return 1
    bak = path + '.pre-patch'
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    open(path, 'w', encoding='utf-8').write(text)
    print('[OK] 会话键注入补丁已施加')
    return 0


if __name__ == '__main__':
    sys.exit(patch(sys.argv[1] if len(sys.argv) > 1 else TARGET))
