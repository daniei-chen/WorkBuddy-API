#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：/v1/usage 按模型拆分（2026-09-27 需求信，真主客户端团队）。

在 patch_usage_endpoint 建出的端点上增量扩展（本补丁必须排在它之后）：
  by_model:      规范模型 → {today, last_7d, last_30d}，字段与聚合段完全一致
  live_by_model: 规范模型 → {window_seconds, tokens, tokens_per_second}
两字段恒返回（向后兼容：旧客户端忽略未知键）；新模型自动出现。
模型 ID 规范化：剥 cn:/global: realm 前缀、转小写、空名归「未知」——否则同一
模型会被 cn: 变体与大小写差异拆成多张卡（需求信 B1 担心的事故形态）。
口径：只读 usage_daily / request_logs；不返回 credit/金额（站方政策：不透出实付）。

维护：apply_patches.sh 启动时自动重施。
幂等：gateway.py 已含 _usage_by_model_windows 标记即跳过。
回退：local-patches/backup/gateway.py.orig-by_model-* 拷回后重启。
用法：patch_usage_by_model.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

TARGET = '/opt/workbuddy-manager/server/routers/gateway.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'
MARKER = '_usage_by_model_windows'

HELPERS = '''

def _usage_norm_model(raw) -> str:
    """模型 ID 规范化：剥 realm 前缀（cn:/global:）、转小写；空名归「未知」。"""
    name = str(raw or '').strip()
    if not name:
        return '未知'
    if ':' in name:
        name = name.rsplit(':', 1)[-1].strip()
    return name.lower()


def _usage_by_model_windows(key_id: int, today: str, d7: str, d30: str) -> dict:
    """规范模型 → {today, last_7d, last_30d}，字段口径与 _usage_window 完全一致。

    一次取全部分日×分模型行，Python 侧归并 realm 变体与大小写；分模型卡之和
    == 聚合段（空模型名的失败请求计入「未知」，对账不打架）。usage_daily 的
    主键本就含 model——上线之前的历史同样可分（需求信 B2：可回溯）。
    """
    rows = db.query(
        "SELECT day, COALESCE(NULLIF(model, ''), '未知') AS name, "
        'COALESCE(SUM(requests),0) AS r, '
        'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t, '
        'COALESCE(SUM(cache_hit_tokens),0) AS h, '
        'COALESCE(SUM(cache_miss_tokens),0) AS m '
        'FROM usage_daily WHERE key_id = ? AND day >= ? GROUP BY day, name',
        (key_id, d30),
    )
    acc = {}
    for row in rows:
        name = _usage_norm_model(row['name'])
        bucket = acc.setdefault(
            name, {'today': [0, 0, 0, 0], 'last_7d': [0, 0, 0, 0], 'last_30d': [0, 0, 0, 0]})
        for win, since in (('today', today), ('last_7d', d7), ('last_30d', d30)):
            if row['day'] >= since:
                cell = bucket[win]
                cell[0] += int(row['r'] or 0)
                cell[1] += int(row['t'] or 0)
                cell[2] += int(row['h'] or 0)
                cell[3] += int(row['m'] or 0)
    out = {}
    for name, bucket in acc.items():
        cards = {}
        for win in ('today', 'last_7d', 'last_30d'):
            r, t, h, m = bucket[win]
            cards[win] = {
                'requests': r,
                'tokens': t,
                'cache_hit_tokens': h,
                'cache_miss_tokens': m,
                'hit_rate': round(h / (h + m), 4) if (h + m) else None,
            }
        out[name] = cards
    return out


def _usage_live_model(key_id: int, window: int) -> dict:
    """规范模型 → 实时速度（口径同 _usage_live：只计 completion_tokens）。"""
    since = int(time.time()) - window
    rows = db.query(
        'SELECT model, COALESCE(SUM(completion_tokens),0) AS t FROM request_logs '
        'WHERE key_id = ? AND ts >= ? GROUP BY model',
        (key_id, since),
    )
    acc = {}
    for row in rows:
        name = _usage_norm_model(row['model'])
        acc[name] = acc.get(name, 0) + int(row['t'] or 0)
    return {
        name: {'window_seconds': window, 'tokens': tk, 'tokens_per_second': round(tk / window, 1)}
        for name, tk in sorted(acc.items())
    }
'''

ANCHOR_TAIL = '''            'hit_rate': round(hit / cached, 4) if cached else None,
        })
    return out
'''

ANCHOR_BLOCK = '''    # by_model=1：额外给出近 30 天按模型拆分（用于客户端展示"哪个模型用了多少"）
    if request.query_params.get('by_model') in ('1', 'true', 'yes'):
        resp['by_model'] = _usage_by_model(key['id'], _usage_since(30))
'''

NEW_BLOCK = '''    # by_model / live_by_model：恒返回的增量字段（需求信 A2，向后兼容）。
    resp['by_model'] = _usage_by_model_windows(key['id'], today, _usage_since(7), _usage_since(30))
    resp['live_by_model'] = _usage_live_model(key['id'], live_window)
    for _name in resp['by_model']:
        resp['live_by_model'].setdefault(
            _name, {'window_seconds': live_window, 'tokens': 0, 'tokens_per_second': 0.0})
'''

ANCHOR_DOC = '''    by_model=1 时额外返回近 30 天按模型拆分（by_model 数组）。
'''

NEW_DOC = '''    响应恒含 by_model（规范模型 → today/last_7d/last_30d 全窗口拆分）与
    live_by_model（规范模型 → 实时速度），新模型自动出现。
'''


def main() -> int:
    try:
        text = open(TARGET, encoding='utf-8').read()
    except FileNotFoundError:
        print('[skip] gateway.py 不存在（更新换版中？）')
        return 0
    if MARKER in text:
        print('[skip] 已含按模型拆分标记')
        return 0
    for name, anchor in (('A(_usage_by_model 尾部)', ANCHOR_TAIL),
                         ('B(legacy by_model 块)', ANCHOR_BLOCK),
                         ('C(docstring 行)', ANCHOR_DOC)):
        if text.count(anchor) != 1:
            print(f'[FAIL] 锚点{name}不唯一/缺失（count={text.count(anchor)}），未落盘')
            return 1
    out = text.replace(ANCHOR_TAIL, ANCHOR_TAIL + HELPERS, 1)
    out = out.replace(ANCHOR_BLOCK, NEW_BLOCK, 1)
    out = out.replace(ANCHOR_DOC, NEW_DOC, 1)
    try:
        compile(out, TARGET, 'exec')
    except SyntaxError as exc:
        print(f'[FAIL] 补丁后语法错误，未落盘：{exc}')
        return 1
    os.makedirs(BACKUP, exist_ok=True)
    bak = os.path.join(BACKUP, 'gateway.py.orig-by_model-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(TARGET, bak)
    open(TARGET, 'w', encoding='utf-8').write(out)
    print('[OK] /v1/usage 按模型拆分已施加（by_model + live_by_model）')
    print(f'     备份 -> {bak}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
