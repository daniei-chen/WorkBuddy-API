"""Windows 批处理脚本（.cmd）的编码与换行符守卫。

为什么需要它：这类问题**在开发机上不一定看得出来**，而在用户机器上表现成
一串吓人的报错。

现场（v1.0.51 实际发出去了）：`deploy/windows-native/*.cmd` 用 UTF-8 存了中文
注释，而 cmd.exe 按**系统 ANSI 代码页**解析批处理文件——中文 Windows 上是
936/GBK。UTF-8 的多字节序列被当成 GBK 解码后，行边界被吃掉、两行粘成一行，
于是 `REM` 不再位于行首（不再是注释），碎片被当作命令执行：

    '有一…' 不是内部或外部命令，也不是可运行的程序
    'E' 不是内部或外部命令，也不是可运行的程序

更隐蔽的是：**脚本整体可能仍然“能用”**（比如 start.cmd 后面那句 powershell
照样执行），只是每次启动都喷一堆看不懂的报错，用户以为装坏了。所以「能跑通」
不足以说明没问题——必须直接检查字节。

三条机械可查的性质：

1. **纯 ASCII**（.cmd 不能靠 BOM 救：cmd.exe 会把 BOM 当命令的一部分）。
   .ps1 不受影响——PowerShell 认 BOM，本仓的 .ps1 都带 BOM，故不在此列。
2. **CRLF 换行**：cmd 对 LF-only 批处理在 if 块 / goto / 标签边界上有边缘
   解析风险（上游 #163 专门处理过），且脚本是给用户直接运行的。
3. **有 .gitattributes 规则锁住**，否则 git 检出会按平台改写换行符——
   在 Linux 上跑 CI 打包时会把 CRLF 变回 LF，用户下到的包又坏了。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _batch_files() -> list[Path]:
    """仓库里所有面向用户的批处理脚本（跳过 node_modules 与 .git）。"""
    found: list[Path] = []
    for p in _ROOT.rglob('*.cmd'):
        parts = set(p.parts)
        if 'node_modules' in parts or '.git' in parts:
            continue
        found.append(p)
    for p in _ROOT.rglob('*.bat'):
        parts = set(p.parts)
        if 'node_modules' in parts or '.git' in parts:
            continue
        found.append(p)
    return sorted(found)


class BatchScriptEncodingTest(unittest.TestCase):
    def test_found_scripts(self) -> None:
        """先确认扫到了东西，否则下面几条会在空集合上「通过」。"""
        files = _batch_files()
        self.assertGreaterEqual(
            len(files), 3, f'只扫到 {len(files)} 个批处理脚本，扫描逻辑可能失效：{files}',
        )
        names = {p.name for p in files}
        self.assertIn('start-workbuddy2api.cmd', names)
        self.assertIn('stop-workbuddy2api.cmd', names)
        self.assertIn('start.cmd', names)

    def test_ascii_only(self) -> None:
        """不得含非 ASCII 字节。

        cmd.exe 用系统 ANSI 代码页解析批处理，UTF-8 中文注释会被拆成乱码、
        吃掉行边界，然后碎片被当命令执行（详见模块 docstring）。
        注释一律用英文——上游自带的三个 .cmd 也是纯 ASCII。
        """
        for p in _batch_files():
            with self.subTest(script=p.relative_to(_ROOT).as_posix()):
                data = p.read_bytes()
                try:
                    data.decode('ascii')
                except UnicodeDecodeError as exc:
                    bad = data[max(0, exc.start - 30):exc.start + 30]
                    self.fail(
                        f'{p.relative_to(_ROOT)} 含非 ASCII 字节（{exc.reason}，'
                        f'位置 {exc.start}）：{bad!r}\n'
                        '  非 ASCII 注释会让 cmd.exe 解析错乱并执行乱码碎片——'
                        '请把注释改成英文（.cmd 不能靠 BOM 解决，'
                        'cmd.exe 会把 BOM 当命令的一部分）。'
                    )

    def test_no_utf8_bom(self) -> None:
        """不得带 UTF-8 BOM：cmd.exe 会把 BOM 当成命令的第一个字符而报错。"""
        for p in _batch_files():
            with self.subTest(script=p.relative_to(_ROOT).as_posix()):
                self.assertFalse(
                    p.read_bytes().startswith(b'\xef\xbb\xbf'),
                    f'{p.relative_to(_ROOT)} 带 UTF-8 BOM，cmd.exe 会报错',
                )

    def test_crlf_line_endings(self) -> None:
        """必须 CRLF 换行（上游 #163：cmd 对 LF-only 批处理有边缘解析风险）。"""
        for p in _batch_files():
            with self.subTest(script=p.relative_to(_ROOT).as_posix()):
                data = p.read_bytes()
                crlf = data.count(b'\r\n')
                bare_lf = data.count(b'\n') - crlf
                self.assertEqual(
                    bare_lf, 0,
                    f'{p.relative_to(_ROOT)} 有 {bare_lf} 处裸 LF（应全部为 CRLF）',
                )
                self.assertGreater(crlf, 0, f'{p.relative_to(_ROOT)} 没有任何 CRLF')
                self.assertNotIn(
                    b'\r\r\n', data,
                    f'{p.relative_to(_ROOT)} 出现 CRCRLF（重复转换过）',
                )


class GitAttributesTest(unittest.TestCase):
    """.gitattributes 必须锁住 .cmd 的换行符，否则发布包里的脚本会变回 LF。"""

    def test_cmd_locked_to_crlf(self) -> None:
        ga = _ROOT / '.gitattributes'
        self.assertTrue(ga.is_file(), '缺少 .gitattributes——.cmd 的 CRLF 无人保障')
        text = ga.read_text(encoding='utf-8')
        # 形如：*.cmd text eol=crlf
        self.assertRegex(
            text, r'(?m)^\s*\*\.cmd\s+.*\beol=crlf\b',
            '.gitattributes 里没有「*.cmd ... eol=crlf」规则：'
            '在 Linux 上检出/打包时 .cmd 会变成 LF，用户下到的发布包里脚本是坏的',
        )

    def test_rule_is_not_vacuous(self) -> None:
        """反证：确认这条规则真的会作用于我们的脚本路径（而不是没匹配上）。"""
        import subprocess

        r = subprocess.run(
            ['git', 'check-attr', 'eol', '--', 'deploy/windows-native/start-workbuddy2api.cmd'],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            self.skipTest('本环境没有 git，跳过（CI 上会跑）')
        self.assertIn('eol: crlf', r.stdout, f'git 没把该文件判成 crlf：{r.stdout!r}')


if __name__ == '__main__':
    unittest.main()
