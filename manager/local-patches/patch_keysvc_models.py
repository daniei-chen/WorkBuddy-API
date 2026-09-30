#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：模型白名单输入兜底——全角/半角逗号统一拆分（keysvc.py）。

背景（2026-09-25 客户实例）：编辑密钥时模型名用全角逗号连接
（"deepseek-v4.1-flash，glm-5.3-flash"），前端按半角拆分、后端整体入库，
白名单变成一个坏元素——列表显示"1 模型"，实际任何模型都被 400 拒绝。
存量数据已手工修复；本补丁在后端归一化，防复发（红包批量创建同样覆盖，
它复用 create_key）。

幂等：检测到 _norm_models 定义即跳过。锚点异常则中止不写（fail-closed）。
用法：patch_keysvc_models.py [keysvc.py 路径]
"""
from __future__ import annotations

import os
import shutil
import sys
import time

DEFAULT = '/opt/workbuddy-manager/server/keysvc.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'
MARK = 'def _norm_models'

FN = '''def _norm_models(items: object) -> list[str]:
    """模型白名单归一化：兼容"全角/半角逗号连接的整串"输入。

    前端模型输入框按半角逗号拆分；用户用中文输入法敲出全角逗号时
    （如 "模型A，模型B"）会整体存成一个元素——列表显示"1 模型"，实际
    白名单连任何模型都匹配不上（400 model_not_allowed）。这里在后端
    兜底：每个元素再按全角/半角逗号拆分并去空白；正常数组输入原样保留。
    """
    if not isinstance(items, list):
        return items
    out: list[str] = []
    for m in items:
        if isinstance(m, str):
            out.extend(p.strip() for p in m.replace('，', ',').split(',') if p.strip())
        else:
            out.append(m)
    return out


'''

RULES = [
    ("        json.dumps(models or []),",
     "        json.dumps(_norm_models(models or [])),", 1),
    ("        fields['models'] = json.dumps(patch['models'] or [])",
     "        fields['models'] = json.dumps(_norm_models(patch['models'] or []))", 1),
]
ANCHOR = 'def list_keys() -> list[dict]:'


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if not os.path.exists(src):
        print(f'ERROR: {src} 不存在，跳过')
        return 0
    s = open(src, encoding='utf-8').read()
    if MARK in s:
        print('已含 _norm_models，跳过')
        return 0
    for old, _new, expect in RULES:
        if s.count(old) != expect:
            print(f'ERROR: 锚点命中 {s.count(old)} 次（应为 {expect}）：{old[:50]!r}，中止不写')
            return 0
    os.makedirs(BACKUP, exist_ok=True)
    if not any(f.startswith('keysvc.py.orig-models-split') for f in os.listdir(BACKUP)):
        bak = os.path.join(BACKUP, 'keysvc.py.orig-models-split-' + time.strftime('%Y%m%d-%H%M%S'))
        shutil.copy2(src, bak)
        print(f'备份原文件 -> {bak}')
    s = s.replace(ANCHOR, FN + ANCHOR, 1)
    for old, new, _ in RULES:
        s = s.replace(old, new, 1)
    open(src, 'w', encoding='utf-8').write(s)
    print('已应用模型白名单归一化补丁')
    return 0


if __name__ == '__main__':
    sys.exit(main())
