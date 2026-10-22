"""双语文档（README.md / README.en.md）的同步守卫。

维护两份 README 最容易出的问题是**只改一份**：改了中文忘了英文，或者反过来。
后果是两种语言的用户看到的内容不一致——尤其安全说明、部署步骤这类内容，
错了会有人照着做然后踩坑。

这里不检查译文质量（那没法自动判定），只锁**结构性对应**：
章节数、图片集合、代码块数量、语言切换链接、以及发布链路是否带上英文版。
结构对不上就说明有一份被改漏了，报红让人去补。

（覆盖面有限、但抓得住最常见的漏改。译文内容是否准确仍需人工审阅。）
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding='utf-8')


def _headings(text: str, level: int) -> list[str]:
    return re.findall(rf'^{"#" * level} (.+)$', text, re.M)


def _images(text: str) -> set[str]:
    return set(re.findall(r'<img src="([^"]+)"', text))


def _code_blocks(text: str) -> int:
    return len(re.findall(r'^```', text, re.M)) // 2


class BilingualReadmeTest(unittest.TestCase):
    """两份 README 必须结构性对应。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cn = _read('README.md')
        cls.en = _read('README.en.md')

    def test_both_exist(self) -> None:
        self.assertTrue((_ROOT / 'README.md').is_file())
        self.assertTrue((_ROOT / 'README.en.md').is_file())

    def test_same_top_level_sections(self) -> None:
        cn, en = _headings(self.cn, 2), _headings(self.en, 2)
        self.assertEqual(len(cn), len(en),
                         f'二级章节数不一致（中文 {len(cn)} / 英文 {len(en)}）—— 有一份漏改了')

    def test_same_sub_sections(self) -> None:
        cn, en = _headings(self.cn, 3), _headings(self.en, 3)
        self.assertEqual(len(cn), len(en),
                         f'三级章节数不一致（中文 {len(cn)} / 英文 {len(en)}）—— 有一份漏改了')

    def test_same_images(self) -> None:
        """截图必须一一对应：既比集合，也比**出现次数**。

        只比集合会有盲点：某处少了一张截图但那张图在别处也用过时，集合不变
        （反证时实测到——注入一张重复引用后集合仍相同）。所以再比一次总数。
        """
        cn, en = _images(self.cn), _images(self.en)
        self.assertEqual(cn, en,
                         f'截图集合不一致：仅中文有 {cn - en}，仅英文有 {en - cn}')
        n_cn = len(re.findall(r'<img src=', self.cn))
        n_en = len(re.findall(r'<img src=', self.en))
        self.assertEqual(n_cn, n_en,
                         f'截图数量不一致（中文 {n_cn} / 英文 {n_en}）—— 某处少了一张')

    def test_images_actually_exist(self) -> None:
        """引用的图片文件必须真实存在（否则 GitHub 上显示裂图）。"""
        missing = [p for p in _images(self.cn) if not (_ROOT / p).is_file()]
        self.assertEqual(missing, [], f'README 引用了不存在的图片：{missing}')

    def test_same_code_block_count(self) -> None:
        cn, en = _code_blocks(self.cn), _code_blocks(self.en)
        self.assertEqual(cn, en,
                         f'代码块数量不一致（中文 {cn} / 英文 {en}）—— 可能漏了某段示例')

    def test_language_links_present(self) -> None:
        """两份文档都要有互跳链接，否则另一语言用户找不到入口。"""
        self.assertIn('README.en.md', self.cn, '中文版没有指向英文版的链接')
        self.assertIn('](README.md)', self.en, '英文版没有指向中文版的链接')

    def test_mutual_links_near_top(self) -> None:
        """切换链接要在开头（而不是埋在文末）——否则用户看不到。"""
        for text, needle, name in ((self.cn, 'README.en.md', '中文'),
                                   (self.en, '](README.md)', '英文')):
            idx = text.find(needle)
            self.assertGreaterEqual(idx, 0)
            line_no = text[:idx].count('\n') + 1
            self.assertLess(line_no, 40,
                            f'{name}版的语言切换链接在第 {line_no} 行，太靠后了')


class ReadmeShippedEverywhereTest(unittest.TestCase):
    """英文版必须跟着发布链路走，否则用户拿到的包里只有中文。"""

    def test_release_package_includes_en(self) -> None:
        wf = (_ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
        self.assertIn('README.en.md', wf, '发布包没打进 README.en.md')

    def test_updater_syncs_en(self) -> None:
        """一键更新会同步文档——英文版也要在列表里，否则更新后还是旧的。"""
        upd = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')
        m = re.search(r"for name in \(([^)]+)\):", upd)
        self.assertIsNotNone(m, '找不到文档同步列表')
        self.assertIn('README.en.md', m.group(1),
                      '更新器的文档同步列表缺 README.en.md')

    def test_installer_copies_en(self) -> None:
        sh = (_ROOT / 'deploy' / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('README.en.md', sh, '安装脚本没复制 README.en.md')

    def test_changelog_stays_chinese_only(self) -> None:
        """变更日志只有中文——这是**有意的**，不是漏译。

        界面里的「更新日志」页直接读它，用户是中文用户；再维护一份英文
        CHANGELOG 会让每次发版多一份必错的同步工作。这里把「有意」钉住，
        免得后人以为是遗漏而去补一份。
        """
        self.assertTrue((_ROOT / 'CHANGELOG.md').is_file())
        self.assertFalse((_ROOT / 'CHANGELOG.en.md').exists(),
                         '新增了 CHANGELOG.en.md？若确要做英文变更日志，'
                         '请同时更新本测试与发版流程')


if __name__ == '__main__':
    unittest.main()
