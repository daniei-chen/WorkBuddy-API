"""任务 / 日志列表的时间显示要能分出「昨天」（issue #44）。

## 现场

任务页与日志页都有「近 24 小时」这个范围，它**必然跨天**。此前两处都只渲染
`fmtDateTime`（`2026/09/20 14:03:11`），于是列表里今天的 14:03 与昨天的 14:03
长得一模一样，扫一眼分不出哪条属于哪天，只能挨个去数字段里的日期。用户的原话：

> 有的时候在翻看 24 小时的时候，会能翻到前一天的记录 不方便观察各个账号运行状态

## 修法

新增 `fmtDateTimeMarked`：**今天的行保持原样**，非今天的行把日期顶到最前面并
标出「昨天」。不逐行标「今天」——列表里绝大多数行都是今天的，逐行标只是噪音；
真正需要一眼分辨的是那几条不属于今天的。

## 这里钉住的几条

  1. **两处列表都换掉了**：任务页 4 个渲染点（签到/任务各一套桌面+移动），
     日志页 1 个。漏掉任意一处，用户仍会在那一处遇到同一个问题。
  2. **抽屉详情保持完整格式**：单条详情没有「分不清哪天」的问题，不需要标记。
  3. **跨天按本地日历日判**，不是「距今 24 小时内」——凌晨 1 点看 23 小时前的
     记录，用户会认为那是「昨天」。用 `Date.UTC` 归一化再相减，也顺手避开了
     夏令时（某些时区一天是 23/25 小时）导致的取整错误。
  4. **五种语言都有 `format.yesterday`**，缺一条就会在界面上显示成键名本身。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class FormatterSourceTest(unittest.TestCase):
    """TS 侧跑不了，故按源码断言关键结构（与项目里既有的静态检查同一手法）。"""

    def _fmt(self) -> str:
        return (ROOT / 'web/lib/format.ts').read_text(encoding='utf-8')

    def test_marked_formatter_exists(self) -> None:
        src = self._fmt()
        self.assertIn('export function fmtDateTimeMarked', src)

    def test_uses_calendar_day_not_24h_window(self) -> None:
        """判据必须是**本地日历日**之差，不能是「距今 24 小时内」。

        按 24 小时判的话，凌晨 1 点看 23 小时前的记录会被算成「今天」——
        而用户翻日历时明明认为那是昨天，标记反而是错的。
        """
        src = self._fmt()
        seg = src[src.index('function calendarDaysAgo'):]
        seg = seg[:seg.index('\n}')]
        self.assertIn('Date.UTC', seg, '没有把本地日期归一到日历日再比')
        self.assertIn('getFullYear', seg)
        self.assertIn('getMonth', seg)
        self.assertIn('getDate', seg)
        self.assertNotIn('Date.now() -', seg, '用了「距今多少毫秒」，那是 24 小时窗口而非日历日')

    def test_today_rows_are_not_prefixed(self) -> None:
        """今天的行不加前缀——否则每行都挂一个「今天」，纯噪音。"""
        src = self._fmt()
        seg = src[src.index('export function fmtDateTimeMarked'):]
        seg = seg[:seg.index('\n}')]
        self.assertIn('if (dayDiff === 0) return full;', seg)
        self.assertIn("t('format.yesterday')", seg)

    def test_non_today_rows_keep_full_datetime(self) -> None:
        """非今天的行仍要带完整日期时间：只写「昨天 14:03」在 30 天范围里不够用。"""
        src = self._fmt()
        seg = src[src.index('export function fmtDateTimeMarked'):]
        seg = seg[:seg.index('\n}')]
        self.assertIn('return `${t(', seg)
        self.assertIn('${full}`', seg, '非今天的行丢掉了完整日期时间')


class WiringTest(unittest.TestCase):
    def test_tasks_page_all_time_columns_marked(self) -> None:
        src = (ROOT / 'web/app/(main)/tasks/page.tsx').read_text(encoding='utf-8')
        self.assertNotIn('{fmtDateTime(l.ts)}', src,
                         '任务页还有未换的渲染点 —— 那一处仍会分不清昨天')
        self.assertEqual(src.count('{fmtDateTimeMarked(l.ts)}'), 4,
                         '任务页有 4 个渲染点（签到/任务 × 桌面/移动）')

    def test_logs_page_rows_marked(self) -> None:
        src = (ROOT / 'web/app/(main)/logs/page.tsx').read_text(encoding='utf-8')
        self.assertIn('{fmtDateTimeMarked(l.ts)}', src)

    def test_drawer_detail_keeps_plain_format(self) -> None:
        """详情抽屉是单条记录，不需要日期标记；但它必须仍有完整日期时间。"""
        src = (ROOT / 'web/app/(main)/logs/page.tsx').read_text(encoding='utf-8')
        self.assertIn('fmtDateTime(detail.ts)', src)

    def test_yesterday_key_in_all_locales(self) -> None:
        base = ROOT / 'web/lib/i18n/locales'
        for f in sorted(base.glob('*.json')):
            data = json.loads(f.read_text(encoding='utf-8'))
            val = data.get('format', {}).get('yesterday')
            self.assertTrue(val and str(val).strip(),
                            f'{f.name} 缺 format.yesterday（界面会显示成键名）')


if __name__ == '__main__':
    unittest.main()
