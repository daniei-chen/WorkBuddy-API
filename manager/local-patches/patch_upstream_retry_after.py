#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：上游抖动期间给 502 响应加 Retry-After + X-Upstream-Blip 头（2026-09-28）。

背景：wb2api 网关在调腾讯 copilot 上游遇 EOF / closed network connection / TLS
握手超时 / 连接重置时，gateway.py:773/973/984 三处返回 `上游不可用` 502。客户端
（如 DeepSeek CLI / deepseek-harness）拿到 502 后固定 ~907ms 重试，撞恢复窗口
会让多轮连续 502（观察案例：25 分钟 8 请求 5 次 502 全在恢复窗口）。
方案：502 时附带 HTTP 标准 Retry-After（秒，初始 5）+ X-Upstream-Blip: 1。
- 只对网络层抖动打这俩头，不打给业务 5xx（语义错位会误导客户端）
- 503「upstream_unavailable」（密钥池隔离）不动——不是抖动，是配置错误
- 5s 是温和起点：相对于当前 907ms 固定间隔的 5× 退避，给抖动恢复留余量

维护：apply_patches.sh 启动时自动重施（官方更新覆盖 server/ 时靠它恢复）
幂等：gateway.py 已含 _oai_error_with_blip 标记即跳过
回退：local-patches/backup/gateway.py.orig-retry-after-* 拷回后重启
用法：patch_upstream_retry_after.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

TARGET = '/opt/workbuddy-manager/server/routers/gateway.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'
MARKER = '_oai_error_with_blip'

HELPERS = '''

def _oai_error_with_blip(message: str, status: int = 502, exc=None,
                         err_type: str = 'api_error',
                         code: str | None = 'upstream_unavailable') -> JSONResponse:
    """抖动感知的 502 报错：附加 Retry-After（秒）+ X-Upstream-Blip: 1。

    网关在调腾讯 copilot 上游遇网络层错误（EOF / 关闭的连接 / TLS 握手超时 /
    连接重置）时返回。客户端拿到头后按 HTTP 标准退避，避免「007ms 固定间隔撞
    恢复窗口」式的连续失败。

    退避策略：单次返回头，不在网关内累计（保留每请求独立判断语义）。
    """
    resp = _oai_error(message, status, err_type, code)
    resp.headers['Retry-After'] = '5'
    resp.headers['X-Upstream-Blip'] = '1'
    return resp
'''

ANCHOR_OAI_ERROR_RETURN = "    return JSONResponse({'error': err}, status_code=status)\n"

ANCHORS_502 = [
    "        return _oai_error(f'上游不可用: {exc}', 502, 'api_error', 'upstream_unavailable')\n",
]


def main() -> int:
    try:
        text = open(TARGET, encoding='utf-8').read()
    except FileNotFoundError:
        print('[skip] gateway.py 不存在（更新换版中？）')
        return 0
    if MARKER in text:
        print('[skip] 已含 Retry-After 抖动头标记')
        return 0
    if text.count(ANCHOR_OAI_ERROR_RETURN) != 1:
        print(f'[FAIL] _oai_error 结尾锚点不唯一（count={text.count(ANCHOR_OAI_ERROR_RETURN)}），未落盘')
        return 1
    count_502 = text.count(ANCHORS_502[0])
    if count_502 != 3:
        print(f'[FAIL] 502 上游不可用锚点期望 3 处，实际 {count_502}（上游代码可能已变），未落盘')
        return 1
    out = text.replace(ANCHOR_OAI_ERROR_RETURN, ANCHOR_OAI_ERROR_RETURN + HELPERS, 1)
    out = out.replace(ANCHORS_502[0],
                      "        return _oai_error_with_blip(f'上游不可用: {exc}', 502, exc)\n")
    try:
        compile(out, TARGET, 'exec')
    except SyntaxError as exc:
        print(f'[FAIL] 补丁后语法错误，未落盘：{exc}')
        return 1
    os.makedirs(BACKUP, exist_ok=True)
    bak = os.path.join(BACKUP, 'gateway.py.orig-retry-after-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(TARGET, bak)
    open(TARGET, 'w', encoding='utf-8').write(out)
    print('[OK] 502 上游不可用响应已附加 Retry-After(5s) + X-Upstream-Blip')
    print(f'     备份 -> {bak}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
