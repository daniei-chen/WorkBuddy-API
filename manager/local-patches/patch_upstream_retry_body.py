#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：在 502 错误体里追加 retry_after_seconds（2026-09-28 修订）。

背景：前一轮 patch_upstream_retry_after 只在响应头里加 Retry-After。但实测：
nginx 反代透传头不稳定，客户端根本读不到。改用「错误体里加字段」——任何
OpenAI 兼容客户端都会原样打印 message+error，里面立刻能看出「建议等 5 秒」。

形式：error.message 末尾追加「请等 5 秒后重试」，新增 error.retry_after_seconds=5。
两个字段冗余（message 给人读、retry_after_seconds 给程序读），与 OpenAI 错误
形状兼容（额外的字段会被容错解析忽略）。

维护：apply_patches.sh 启动时自动重施。
幂等：_oai_error_with_blip_body 函数定义即跳过。
回退：local-patches/backup/gateway.py.orig-retry-body-* 拷回后重启。
用法：patch_upstream_retry_body.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

TARGET = '/opt/workbuddy-manager/server/routers/gateway.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'
MARKER = '_oai_error_with_blip_body'

HELPERS = '''

def _oai_error_with_blip_body(message: str, status: int = 502, exc=None,
                              err_type: str = 'api_error',
                              code: str | None = 'upstream_unavailable') -> JSONResponse:
    """502 上游不可用：在响应头 + 错误体中同时提示客户端等 5 秒再试。

    响应头路径（Retry-After + X-Upstream-Blip）在 nginx 反代链路上透传不稳，
    实测 deepseek-harness 等客户端读不到；错误体字段（retry_after_seconds
    + message 末尾提示）任何 OpenAI 兼容客户端都能从 message/原样 JSON 拿到。
    """
    suffix = ' 请等 5 秒后重试'
    body = {'error': {
        'message': message + suffix,
        'type': err_type,
        'code': code,
        'retry_after_seconds': 5,
    }}
    resp = JSONResponse(body, status_code=status)
    resp.headers['Retry-After'] = '5'
    resp.headers['X-Upstream-Blip'] = '1'
    return resp
'''

# _oai_error_with_blip 调用点（3 处，patch_upstream_retry_after.py 制造的）
ANCHOR_NEW = "        return _oai_error_with_blip(f'上游不可用: {exc}', 502, exc)\n"


def main() -> int:
    try:
        text = open(TARGET, encoding='utf-8').read()
    except FileNotFoundError:
        print('[skip] gateway.py 不存在（更新换版中？）')
        return 0
    if MARKER in text:
        print('[skip] 已含 502 错误体内 retry 字段标记')
        return 0
    # 校验前置补丁已生效
    if text.count(ANCHOR_NEW) != 3:
        print(f'[FAIL] 前置 patch_upstream_retry_after 调用点期望 3 处，实际 {text.count(ANCHOR_NEW)}，未落盘')
        return 1
    out = text
    # 把 _oai_error_with_blip 替换为 _oai_error_with_blip_body；helper 加在末尾避免锚点竞争
    last_helper = "    return resp\n"
    if out.count(last_helper) == 0:
        print('[FAIL] 找不到 helper 函数收尾锚点（上游代码可能已变），未落盘')
        return 1
    out = out.replace(ANCHOR_NEW,
                      "        return _oai_error_with_blip_body(f'上游不可用: {exc}', 502, exc)\n", 1)
    # 把 helper 紧贴在 _oai_error_with_blip 函数定义之后
    blip_def_end = "    resp = _oai_error(message, status, err_type, code)\n    resp.headers['Retry-After'] = '5'\n    resp.headers['X-Upstream-Blip'] = '1'\n    return resp\n"
    if out.count(blip_def_end) != 1:
        print('[FAIL] 找不到 _oai_error_with_blip 函数定义，未落盘')
        return 1
    out = out.replace(blip_def_end, blip_def_end + HELPERS, 1)
    try:
        compile(out, TARGET, 'exec')
    except SyntaxError as exc:
        print(f'[FAIL] 补丁后语法错误，未落盘：{exc}')
        return 1
    os.makedirs(BACKUP, exist_ok=True)
    bak = os.path.join(BACKUP, 'gateway.py.orig-retry-body-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(TARGET, bak)
    open(TARGET, 'w', encoding='utf-8').write(out)
    print('[OK] 502 错误体已追加 retry_after_seconds=5 + message 末尾提示')
    print(f'     备份 -> {bak}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
