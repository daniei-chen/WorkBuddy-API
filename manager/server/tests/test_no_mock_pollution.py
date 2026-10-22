"""测试之间不得互相污染（本次排查的真实事故的回归防线）。

## 事故

`test_model_filter.UpstreamFallbackPathTest` 写的是：

    p = pat.start()
    self.addCleanup(p.stop)

而 `patch.start()` 返回的是**被装上去的 mock 对象**，不是 patcher ——
它的 `.stop` 是自动生成的 MagicMock 属性，调用它**什么都不做**。于是那个
patch **永不撤销**，而 `wb2api` 是全局单例模块，后续所有依赖
`list_auth_accounts` 的用例都静默拿到 mock 的值。

表现极具迷惑性：新写的 `test_token_renew` **单独跑全绿、全量跑 6 个红**，
失败信息还是「统计数为 0」这种业务语义，完全指向不到 mock 泄漏。
最后是脚本二分（`dev/bisect_test_pollution.py`）才定位到模块、再逐类跑才定位到类。

## 这里防两类

  1. **源码级**：`pat.start()` 的返回值被存下来又叫 `.stop` 的写法直接拦掉
     （全仓扫描）。这是可静态发现的形态，成本极低。
  2. **运行时**：跑完一批会污染全局单例的模块后，断言关键属性身份没变。
     静态扫描只能抓已知写法（`var = pat.start()` 后接 `var.stop`），
     直接 `mock.patch(...).start()` 或第三方库留下的全局态抓不到。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TESTS = ROOT / 'server' / 'tests'


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """返回 (行号, 去掉注释与文档串后的代码行)。

    必须剥掉注释与字符串：这个文件本身就在解释那种错误写法，注释里必然会
    出现 `addCleanup(p.stop)` / `.start()` 这些字样，不剥的话扫描器会举报自己
    （以及 test_model_filter 里那段说明注释）。
    """
    import io
    import tokenize

    out: list[tuple[int, str]] = []
    try:
        with path.open('rb') as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.COMMENT:
                    continue
                if tok.type == tokenize.STRING:
                    continue
                if tok.type in (tokenize.NEWLINE, tokenize.NL):
                    out.append((tok.start[0], ''))
                    continue
                out.append((tok.start[0], tok.string))
    except Exception:  # noqa: BLE001
        # 解析失败（语法怪）时退回朴素切分，至少不误伤
        for i, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            out.append((i, line.split('#', 1)[0]))
    return out


def _code_text(path: Path) -> str:
    """把代码行拼回文本（行号保留，便于报位置）。"""
    lines: dict[int, str] = {}
    for lineno, text in _code_lines(path):
        if text:
            lines[lineno] = lines.get(lineno, '') + text
    if not lines:
        return ''
    top = max(lines)
    return chr(10).join(lines.get(i, '') for i in range(1, top + 1))


class NoDanglingPatcherTest(unittest.TestCase):
    """静态扫描：`x = pat.start()` 且随后出现 `x.stop`（或 addCleanup(x.stop)）。"""

    # 形如 `p = something.start()`，捕获变量名
    ASSIGN = re.compile(r'^\s*(\w+)\s*=\s*[\w.]*\.start\(\)\s*$', re.M)

    def _stop_re(self, var: str) -> re.Pattern[str]:
        return re.compile(rf'\b{re.escape(var)}\.stop\b')

    def test_no_assigned_start_result_is_stopped(self) -> None:
        offenders: list[str] = []
        for path in sorted(TESTS.glob('test_*.py')):
            if path.name == Path(__file__).name:
                continue
            src = _code_text(path)
            for m in self.ASSIGN.finditer(src):
                var = m.group(1)
                if self._stop_re(var).search(src):
                    line = src[:m.start()].count(chr(10)) + 1
                    offenders.append(f'{path.name}:{line} —— {var} = ...start() 后又 {var}.stop')
        self.assertEqual(
            offenders, [],
            'pat.start() 返回的是 mock 对象、不是 patcher，它的 .stop 是空操作，\n'
            '这样写 patch 永远不会撤销（会污染后续用例）。改成 addCleanup(pat.stop)：\n'
            + chr(10).join(offenders),
        )

    def test_addcleanup_uses_patcher_not_start_result(self) -> None:
        """`addCleanup(...)` 里出现 `.start()` 说明传的是 mock，必然是错的。"""
        offenders: list[str] = []
        for path in sorted(TESTS.glob('test_*.py')):
            if path.name == Path(__file__).name:
                continue
            for lineno, text in _code_lines(path):
                if 'addCleanup' in text and '.start()' in text:
                    offenders.append(f'{path.name}:{lineno} —— {text.strip()}')
        self.assertEqual(offenders, [],
                         'addCleanup 收到的是 pat.start() 的返回值（mock），stop 不会生效：\n'
                         + chr(10).join(offenders))


class GlobalSingletonNotPollutedTest(unittest.TestCase):
    """运行时：跑完那批动过全局 patch 的模块后，关键属性必须还是原函数。

    为什么单列一条：静态扫描抓不到「直接 `mock.patch(...).start()` 没存变量」
    或「第三方在 import 时改了全局态」这类形态。这里用一个**真实会污染**的
    场景兜住：跑 `UpstreamFallbackPathTest`（历史上就是它泄漏），跑完检查身份。
    """

    def test_wb2api_attrs_restored_after_patching_module(self) -> None:
        from server.services import wb2api

        watched = ('list_auth_accounts', 'read_account_file_any', 'merge_pool_status')
        before = {n: getattr(wb2api, n) for n in watched}
        try:
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromName(
                'server.tests.test_model_filter.UpstreamFallbackPathTest')
            unittest.TextTestRunner(verbosity=0).run(suite)
        finally:
            after = {n: getattr(wb2api, n) for n in watched}
        for n in watched:
            self.assertIs(
                after[n], before[n],
                f'跑完 test_model_filter 后 wb2api.{n} 被换成了 {after[n]!r} —— '
                '有 patch 没被撤销，后续用例会静默走偏',
            )


if __name__ == '__main__':
    unittest.main()
