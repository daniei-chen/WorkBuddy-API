"""issue 回复必须**面向用户**——与更新日志同一条规矩，但读者更杂。

回复挂在公开 issue 下面，看它的不只是报告者本人，更多是后来搜到自己那条报错、点
进来的普通用户。他要的只有两件事：**这问题修了没有**、**我要不要做什么**。

为什么要有这条守卫：我的回复惯性是「排障笔记」——刚写完代码和提交信息，顺手把
内部函数名、检讨式叙事、测试与验收清单都搬进去。#45、#46 两条就是典型：报告者看
得懂，搜进来的用户一脸茫然。这与 1.0.42 ~ 1.0.49 的更新日志是同一个毛病，那边已
经用测试钉住了，这里同样钉住（规矩见 `docs/release-process.md`）。

反例是**真实发布过的那条回复原文**（#46，2026-09-20），所以这条测试不会因为规范
与实例脱节而空转；正例是照规范改写的短回复，也就是后来补发的那条。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]

# #46 回复原文（节选，逐字未改）——负面样例
BAD_REPLY = """\
三条建议都采纳了（commit `f20e6f9`，下个版本生效）。这份分析很准——你指的两处
（`_scope_models` 只读 `key['realm']`、`validate` 里两处都带 `not is_model_list`）
完全正确，我核对了一遍就按你的做法 1 + 3 改了，做法 2 也一并做了。

## 1. 列表按白名单裁剪，判据与调用侧共用

抽出 `keysvc.model_allowed`，调用侧与列表裁剪都用它（你强调的「不要另写一遍」）
——原来两处各写一遍正是这个 issue 的成因。`_scope_models` 现在叠加白名单裁剪，
白名单为空时行为不变。

**顺带发现两个你没提到的边界，只按字面裁会出错：**

**① 别名**（设置页「模型映射」）。白名单可以写下游熟悉的别名，而别名**不在上游
清单里**——只按清单字面裁，这类密钥的列表会被**裁成空**，比不裁更糟。

**② `cn:` 前缀**：存量密钥的白名单里可能写着 `cn:glm-5.2`，与 `glm-5.2` 等价
（这条判据本来就在 `model_allowed` 里，沿用即可）。

## 2. 填错当场提示（你的做法 2）

新增 `POST /api/keys/check-models` + 密钥页白名单输入框失焦时的提示。

**这里有个反直觉的坑，差点做成假警报**：第一版只去了一边的前缀，于是正确的名字
被判成「找不到」。是浏览器验收脚本报出来的。

另外两条约束：**每个名字只跟自己版本的清单比**；**清单拿不到就不判**。

## 3. 不变量测试

按你说的加了「列表内容 ↔ 调用是否放行」的绑定，而且是双向的。e2e（真 socket）里
也加了一条。做了两处反证确认测试有效：退回「只裁版本」→ 5 条变红。

## 一个自己踩的 UI 坑，记一下

提示挂在弹窗里最后一个字段下面，实测会被滚动容器裁在可视区之下——元素渲染了、
`isVisible()` 也是真，但用户一个像素都看不到。

---

验证：全量 1222 条通过；e2e 全过；浏览器验收 14 项全过。
"""

# 照规范改写的版本 —— 正面样例（即 #46 补发的那条）
GOOD_REPLY = """\
已经改好了，**下个版本（1.0.61）生效**，不需要你做任何操作。

- 白名单现在会同时**收窄 `/v1/models` 返回的列表**，与调用时的放行判据完全一致。
  此前只管调用、不管列表，两者会对不上：客户端看到「能选」，选中却报「模型不在
  密钥白名单内」。
- 在「密钥」页填写白名单时输错了名字，输入框下方会**当场提示**；模型清单还没取到
  时不提示，避免误报。

两处容易踩的边界也一并处理了：白名单里写**别名**（设置页「模型映射」里配过的名字）
会保留在列表里；`cn:` / `global:` 前缀可写可不写。

感谢你把三处问题定位得这么清楚，尤其「列表与调用必须是同一份判据」这点。
"""


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        'check_issue_reply', _ROOT / 'dev' / 'check_issue_reply.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReplyCheckerBitesTest(unittest.TestCase):
    """先证明检查器**会咬人**，否则下面「正例通过」毫无意义。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_checker()

    def test_fixtures_are_substantial(self) -> None:
        """样例本身要够长，否则检查器可能是在空转。"""
        self.assertGreater(len(BAD_REPLY.splitlines()), 15)
        self.assertGreater(len(GOOD_REPLY.splitlines()), 5)

    def test_bad_reply_is_flagged(self) -> None:
        findings = self.mod.check(BAD_REPLY)
        self.assertGreaterEqual(len(findings), 8,
                                f'反例只报出 {len(findings)} 处 —— 检查器退化了')
        rules = {f.rule for f in findings}
        for want in ('内部标识符', '测试/验收清单', '检讨式叙事'):
            self.assertIn(want, rules, f'「{want}」这一类没报出来：{sorted(rules)}')

    def test_bad_reply_flagged_on_shape(self) -> None:
        """篇幅单独一条：超长本身就违背判据（真实那条 23 行、5 个小标题）。"""
        shape = self.mod.check_shape(BAD_REPLY)
        self.assertTrue(shape, '反例没触发篇幅检查')
        self.assertTrue(any('行' in f.token for f in shape))
        self.assertTrue(any('小标题' in f.token for f in shape))

    def test_good_reply_passes(self) -> None:
        findings = self.mod.check(GOOD_REPLY) + self.mod.check_shape(GOOD_REPLY)
        self.assertEqual([f'{f.line}:{f.rule}:{f.token}' for f in findings], [],
                         '正例被误报了 —— 误报的检查会被无视')

    def test_user_visible_quote_is_exempt(self) -> None:
        """**例外通道必须真的通**：报告/文档里出现过的原文不算违规。

        用户界面上的报错、报告者自己贴的字段名，回复里引用它们是必要的（用户就是
        拿那句话来搜的），不能一律拦下。
        """
        draft = '你看到的报错来自 `POST /api/accounts/refresh`，这条路径现在会真正续期了。'
        self.assertTrue(self.mod.check(draft), '没豁免之前应当先报出来')
        self.assertEqual(
            self.mod.check(draft, issue_text='点刷新后返回 404：POST /api/accounts/refresh failed'),
            [], '引用了报告原文却被拦下 —— 例外通道失效')

    def test_env_vars_are_allowed(self) -> None:
        """环境变量不算内部标识符 —— 规范明写「用户要动手的东西可以写」。

        这里钉住的是**检查器自己的一个 bug**：它一度把 `WB_GATEWAY_RATE_PER_MIN`
        这类环境变量也拦下来，而那正是用户必需的一步（回复里省掉它，用户就配不成）。
        环境变量是全大写、内部标识符是小写蛇形/点号路径，按形状区分得开。

        已知残留风险：内部的全大写常量（如 `RATE_MAX_PER_MIN`）会一起放行。
        可接受——它极少出现在回复里，也不是这套规矩要治的毛病。
        """
        for text in ('设置 `WB_GATEWAY_RATE_PER_MIN` 后重启容器',
                     '设置 WB_GATEWAY_RATE_PER_MIN 后重启容器',
                     '用它提供的 `TW2A_API_KEY`'):
            with self.subTest(text=text):
                self.assertEqual(self.mod.check(text), [],
                                 f'环境变量被误拦：{text}')

    def test_internal_ids_still_caught_after_env_carveout(self) -> None:
        """放行环境变量**不能**把内部标识符一起放过（否则守卫形同虚设）。"""
        for text in ('修了 `_scope_models` 的判据',
                     '走 `keysvc.model_allowed` 判定',
                     '看 `server/main.py` 那行',
                     '提交 `f20e6f9` 已包含'):
            with self.subTest(text=text):
                self.assertTrue(self.mod.check(text), f'内部标识符漏放了：{text}')

    def test_ai_boilerplate_is_flagged(self) -> None:
        """客套与模板腔要拦下（维护者反馈：「回复口吻不要太 ai」）。

        这类句子对用户零信息量，还会把真话稀释掉。判据写进
        docs/release-process.md 的「口吻：像维护者本人在说话，别像客服」。
        """
        for text in ('感谢您的反馈！已修复。',
                     '希望这能帮助到您。',
                     '如有任何疑问，请随时与我们联系。',
                     '需要注意的是，该行为已改变。',
                     '首先列出结论，其次说明步骤。',
                     '给您带来不便，敬请谅解。'):
            with self.subTest(text=text):
                self.assertTrue(self.mod.check(text), f'模板腔漏放了：{text}')

    def test_plain_voice_passes(self) -> None:
        """人话版要放行 —— 这条规则**不能**把正常的技术说明也一起拦掉。"""
        for text in ('修好了，1.0.63 里。',
                     '只读账号现在跟管理员看到的是同一份积分，查不到时标「上游快照」。',
                     '有问题再开。',
                     '设 WB_SYNC_DEPLOY=0 就保持不动。'):
            with self.subTest(text=text):
                self.assertEqual(self.mod.check(text), [], f'误拦了正常回复：{text}')

    def test_urls_are_not_mistaken_for_paths(self) -> None:
        """URL 里的点号/斜杠不能被当成模块路径，否则每条带链接的回复都误报。"""
        self.assertEqual(self.mod.check('详见 https://github.com/ithtelab/workbuddy-manager/issues/46'), [])


class ReplyStandardIsDocumentedTest(unittest.TestCase):
    """规范本身也要在文档里存在 —— 删掉文档，测试跟着红。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (_ROOT / 'docs/release-process.md').read_text(encoding='utf-8')

    def test_section_exists(self) -> None:
        self.assertIn('## issue 回复的写法', self.text)

    def test_states_criteria_and_limits(self) -> None:
        self.assertIn('**判据**：一个搜到这条 issue、但不读代码的用户', self.text)
        for want in ('内部标识符', '排障过程与自我检讨', '测试与验收清单', '**长度**'):
            self.assertIn(want, self.text, f'规范里少了「{want}」这一条')

    def test_points_at_the_checker(self) -> None:
        self.assertIn('dev/check_issue_reply.py', self.text)
        self.assertTrue((_ROOT / 'dev/check_issue_reply.py').exists())


if __name__ == '__main__':
    unittest.main()
