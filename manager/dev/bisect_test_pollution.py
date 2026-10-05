"""二分定位：哪个测试模块与 test_token_renew 同跑时会把它带红。

背景：`test_token_renew` 单独跑全绿，全量跑就 6 个失败。典型原因是某个模块
留下了未撤销的 mock（`list_auth_accounts` / `http_client` / AUTH_DIR 之类），
污染了后面的用例。这个脚本二分找出那个模块，方便去修它的 tearDown。
"""
from __future__ import annotations

import glob
import re
import subprocess
import sys
from pathlib import Path

TARGET = 'server.tests.test_token_renew'


def module_of(path: str) -> str:
    stem = Path(path.replace('\\', '/')).stem
    return f'server.tests.{stem}'


def run(subset: list[str]) -> int:
    r = subprocess.run([sys.executable, '-m', 'unittest', *subset, TARGET, '-q'],
                       capture_output=True, text=True)
    return len(re.findall(r'^(?:FAIL|ERROR): ', r.stderr, re.M))


def main() -> int:
    mods = sorted(module_of(p) for p in glob.glob('server/tests/test_*.py'))
    mods = [m for m in mods if m != TARGET]
    if run([]) != 0:
        print('基线：单独跑 test_token_renew 就已经红了，问题不在别的模块')
        return 1
    lo, hi = 0, len(mods)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if run(mods[lo:mid]) > 0:
            hi = mid
        else:
            lo = mid
    if lo >= len(mods):
        print(f'{len(mods)} 个模块全跑一遍都没有失败 —— 需要更细的二分')
        return 1
    suspect = mods[lo]
    print('可疑模块:', suspect, '（单独配对失败数:', run([suspect]), '）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
