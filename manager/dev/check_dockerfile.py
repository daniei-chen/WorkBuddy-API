"""Dockerfile 结构自检（无 docker 环境下的等效校验）。

为什么需要：本地与 CI 都不一定随时能跑 docker build（本机就没装），而这个
Dockerfile 的改动（多阶段、条件分支）出错的代价是**用户装不上**。至少要把
机械可查的性质钉住：指令拼写、阶段引用可解析、shell 引号配平、续行完整。

    python dev/check_dockerfile.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DF = ROOT / 'Dockerfile'

KNOWN = ('FROM', 'RUN', 'CMD', 'LABEL', 'EXPOSE', 'ENV', 'ADD', 'COPY',
         'ENTRYPOINT', 'VOLUME', 'USER', 'WORKDIR', 'ARG', 'ONBUILD',
         'STOPSIGNAL', 'HEALTHCHECK', 'SHELL')


def main() -> int:
    df = DF.read_text(encoding='utf-8')
    lines = df.splitlines()
    problems: list[str] = []

    # 1. 指令拼写
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if not s or s.startswith('#'):
            continue
        first = s.split()[0].upper()
        if first in KNOWN:
            continue
        if i >= 2 and lines[i - 2].rstrip().endswith('\\'):
            continue   # 续行内容
        problems.append(f'{i}: 非法指令 {l[:70]!r}')

    # 2. 阶段引用可解析
    stages = [m.group(1) for m in
              re.finditer(r'(?m)^FROM\s+\S+\s+AS\s+(\S+)', df, re.I)]
    refs = set(re.findall(r'--from=(\S+)', df))
    for r in refs:
        if r not in stages and not r.isdigit():
            problems.append(f'COPY --from={r} 引用了不存在的阶段（已定义：{stages}）')

    # 3. shell 引号配平（逐 RUN 块统计，用于发现明显漏引号）
    run_buf: list[str] = []
    for i, l in enumerate(lines, 1):
        if l.strip().startswith('RUN '):
            run_buf = [l]
        elif run_buf and lines[i - 2].rstrip().endswith('\\'):
            run_buf.append(l)
        else:
            if run_buf:
                blk = '\n'.join(run_buf)
                if blk.count("'") % 2 != 0:
                    problems.append(f'RUN 块（约第 {i - len(run_buf)} 行）单引号数为奇数')
            run_buf = []
    if run_buf and '\n'.join(run_buf).count("'") % 2 != 0:
        problems.append('最后一个 RUN 块单引号数为奇数')

    # 4. 续行不能后面跟着空行
    for i, l in enumerate(lines, 1):
        if l.rstrip().endswith('\\') and i < len(lines) and not lines[i].strip():
            problems.append(f'{i}: 以 \\ 结尾但下一行为空')

    # 5. 关键契约（与 issue #38 直接相关）
    if not re.search(r'(?m)^FROM\s+node:', df):
        problems.append('缺少 Node 构建阶段（git clone 场景无法构建前端）')
    if 'NEXT_OUTPUT_EXPORT' not in df:
        problems.append('缺少 NEXT_OUTPUT_EXPORT（会构建出非静态导出产物）')
    if not re.search(r'(?m)^COPY\s+--from=\S+\s+\S+\s+/app/web/out\s*$', df):
        problems.append('没有把前端产物拷到 /app/web/out 的 COPY 指令')

    if problems:
        print('Dockerfile 自检发现问题：')
        for p in problems:
            print('  -', p)
        return 1
    print(f'Dockerfile 自检通过（阶段：{stages}）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
