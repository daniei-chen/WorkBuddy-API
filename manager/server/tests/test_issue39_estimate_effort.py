"""issue #39 的另外两项：count_tokens 估算偏低、推理档位被吃掉。

## 一、`count_tokens` 的「除 3」对中文是严重低估

原实现是 `(字符数 + 2) // 3`，而它的注释同时写着「宁可高估（客户端会保守地留
更多余量），也不能低估」——**两者是矛盾的**。除 3 对 ASCII 大致合理（英文约
4 字符/token），但汉字通常 1–1.5 字符/token，除 3 会把一段中文算成实际的约 1/3。

方向性错误在这里很要紧：低估会让客户端以为还能塞更多，请求真发出去时被上游以
「上下文过长」拒绝，而用户完全看不出是估算接口给了错数字。

## 二、`output_config.effort` / `thinking.budget_tokens` 被吃掉

`to_openai_request` 此前把 `thinking` 整个吃掉、只用于「要不要回 thinking 块」，
`output_config` 更是完全没读。上游（workbuddy2api）接受顶层 `reasoning_effort`
并按模型能力降级（其 `payload.go` 的 `normalizeReasoningEffort` 与
`thinking.go` 的默认档）。不映射的话：用户选了档位、实际发的是默认档，
现象是「调了没反应」，且无从自查。

这里钉住两条 + 一个**不做重复实现**的边界：档位合法性交给上游降级管线，
我们只做档位名的映射（名字重合的同名透传，**不把 `max` 降成 `xhigh`** —— 上游
`payload.go` 的 `effortRank` 把 `xhigh` 排在 `max` 之前，两者是不同的档）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.routers import anthropic  # noqa: E402


class EstimateTokensTest(unittest.TestCase):
    def test_chinese_not_underestimated(self) -> None:
        """中文必须按 ~1 字符 1 token 估，而不是除 3。

        这是本条的全部要点：除 3 会把 300 字的文本说成 100 token，
        真实消耗约 300，客户端据此留的余量少了三倍。
        """
        text = '这是一段中文测试文本' * 30          # 300 汉字
        est = anthropic._estimate_tokens(text)
        self.assertGreaterEqual(est, 300 * 0.9, f'低估了中文：{est} < 270')
        self.assertLessEqual(est, 300 * 1.6, f'高估过头：{est}')

    def test_ascii_roughly_four_chars_per_token(self) -> None:
        text = 'hello world ' * 100                 # 1200 ASCII 字符
        est = anthropic._estimate_tokens(text)
        self.assertGreaterEqual(est, 250)
        self.assertLessEqual(est, 400, f'ASCII 估得不像 4 字符/token：{est}')

    def test_mixed_text_is_between(self) -> None:
        cjk = anthropic._estimate_tokens('中文' * 100)
        ascii_ = anthropic._estimate_tokens('ab' * 100)
        mixed = anthropic._estimate_tokens(('中文' + 'ab') * 100)
        self.assertGreater(mixed, ascii_)
        self.assertLess(mixed, cjk + ascii_ + 1)

    def test_old_div3_would_have_been_lower(self) -> None:
        """反证：确认新口径确实比旧的「除 3」高（中文场景）。"""
        text = '这是一段中文测试文本' * 30
        old = (len(text) + 2) // 3
        self.assertGreater(anthropic._estimate_tokens(text), old,
                           '新估算没有比旧的除 3 更高 —— 那这次改动就没意义')

    def test_empty_is_zero(self) -> None:
        self.assertEqual(anthropic._estimate_tokens(''), 0)
        self.assertEqual(anthropic._estimate_tokens(None), 0)  # type: ignore[arg-type]

    def test_japanese_and_korean_counted_as_cjk(self) -> None:
        for text in ('ひらがなカタカナ' * 20, '한국어텍스트' * 30):
            est = anthropic._estimate_tokens(text)
            self.assertGreaterEqual(est, len(text) * 0.9, f'{text[:6]} 被低估：{est}')


class ReasoningEffortTest(unittest.TestCase):
    def test_output_config_effort_mapped(self) -> None:
        self.assertEqual(
            anthropic._reasoning_effort({'output_config': {'effort': 'high'}}), 'high')
        self.assertEqual(
            anthropic._reasoning_effort({'output_config': {'effort': 'LOW'}}), 'low')

    def test_anthropic_max_stays_max(self) -> None:
        """`max` **不能**被降成 `xhigh` —— 上游两者是不同的档，`max` 更强。

        上游 `payload.go` 的 `effortRank` 是
        `off < minimal < low < medium < high < xhigh < max`，
        而 `effort_catalog.go` 里 gpt-5.6 系同时声明了 `xhigh` 与 `max`。

        降档的后果正是这个映射要消除的「设了没用」：用户选最高档，实际拿到次高档。
        （这条断言早期版本写反了，是核对上游 effortRank 时发现的。）
        """
        self.assertEqual(
            anthropic._reasoning_effort({'output_config': {'effort': 'max'}}), 'max')

    def test_upstream_native_levels_pass_through(self) -> None:
        """上游合法但 Anthropic 文档没写的档位（`xhigh`）要能透传。

        不认识就丢弃，等于客户端设了上游支持的档位却被我们吃掉。
        """
        self.assertEqual(
            anthropic._reasoning_effort({'output_config': {'effort': 'xhigh'}}), 'xhigh')

    def test_budget_tokens_buckets(self) -> None:
        # 最高一档用 max（不是 xhigh）：预算 ≥64k 表达的是「不限思考」，对应最强档。
        cases = [(1024, 'low'), (2048, 'low'), (8192, 'medium'),
                 (32768, 'high'), (128000, 'max')]
        for budget, want in cases:
            got = anthropic._reasoning_effort(
                {'thinking': {'type': 'enabled', 'budget_tokens': budget}})
            self.assertEqual(got, want, f'budget={budget} → {got}，期望 {want}')

    def test_explicit_effort_wins_over_budget(self) -> None:
        """两个都给了时以 output_config.effort 为准（它是显式档位）。"""
        got = anthropic._reasoning_effort({
            'output_config': {'effort': 'low'},
            'thinking': {'type': 'enabled', 'budget_tokens': 128000},
        })
        self.assertEqual(got, 'low')

    def test_unknown_effort_is_ignored_not_forwarded(self) -> None:
        """不认识的档位**不要**透传——上游对它没定义，透传可能被拒。

        回退到 budget（这里没有）→ None → 不写字段 → 上游用模型默认档。
        """
        self.assertIsNone(anthropic._reasoning_effort({'output_config': {'effort': 'turbo'}}))

    def test_malformed_inputs_return_none(self) -> None:
        for body in ({}, {'output_config': None}, {'output_config': 'high'},
                     {'output_config': {'effort': ''}}, {'output_config': {'effort': 3}},
                     {'thinking': None}, {'thinking': {'type': 'enabled'}},
                     {'thinking': {'budget_tokens': 0}},
                     {'thinking': {'budget_tokens': True}},
                     {'thinking': {'budget_tokens': 'big'}}):
            self.assertIsNone(anthropic._reasoning_effort(body), repr(body))

    def test_no_duplicate_effort_catalog(self) -> None:
        """**不该**在映射层再抄一张「每个模型支持哪些档」的表。

        那是上游 `effort_catalog.go` / `normalizeReasoningEffort` 的职责，
        我们重复实现就会有两份事实来源、早晚不一致。这里只做档位名的语义映射。
        """
        src = (ROOT / 'server/routers/anthropic.py').read_text(encoding='utf-8')
        seg = src[src.index('_EFFORT_MAP'):src.index('def _reasoning_effort')]
        self.assertNotIn('deepseek', seg, '映射表里出现了具体模型名 —— 那是上游的表')
        self.assertNotIn('glm', seg)


class RequestWiringTest(unittest.TestCase):
    def _req(self, body: dict) -> dict:
        return anthropic.to_openai_request(body)

    def test_effort_reaches_upstream_field(self) -> None:
        out = self._req({
            'model': 'deepseek-v4.1-flash',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'output_config': {'effort': 'high'},
        })
        self.assertEqual(out.get('reasoning_effort'), 'high')

    def test_budget_tokens_reaches_upstream_field(self) -> None:
        out = self._req({
            'model': 'deepseek-v4.1-flash',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'thinking': {'type': 'enabled', 'budget_tokens': 8000},
        })
        self.assertEqual(out.get('reasoning_effort'), 'medium')

    def test_absent_effort_omits_field(self) -> None:
        """没给档位时**不写**该字段 —— 让上游用它自己的默认档。"""
        out = self._req({
            'model': 'deepseek-v4.1-flash',
            'messages': [{'role': 'user', 'content': 'hi'}],
        })
        self.assertNotIn('reasoning_effort', out)

    def test_thinking_disabled_does_not_send_effort(self) -> None:
        """显式 disabled 且没给 budget 时不该带档位（客户端明确不要思考）。"""
        out = self._req({
            'model': 'deepseek-v4.1-flash',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'thinking': {'type': 'disabled'},
        })
        self.assertNotIn('reasoning_effort', out)


class CountTokensRouteSourceTest(unittest.TestCase):
    def test_route_uses_estimator_not_div3(self) -> None:
        src = (ROOT / 'server/routers/anthropic.py').read_text(encoding='utf-8')
        self.assertNotIn('(total + 2) // 3', src,
                         'count_tokens 还在用「除 3」——中文会被低估')
        seg = src[src.index("async def count_tokens"):]
        seg = seg[:seg.index('return JSONResponse')]
        self.assertIn('_estimate_tokens', seg)

    def test_structure_overhead_added(self) -> None:
        """每个 block 要加一点结构开销：消息边界在真实分词里要额外占位。

        纯按字符数算必然偏低——这是「宁可高估」的落点之一。
        """
        src = (ROOT / 'server/routers/anthropic.py').read_text(encoding='utf-8')
        seg = src[src.index("async def count_tokens"):]
        # 取到该函数的 return 行**结束**为止：结构开销项就写在那行里
        end = seg.index('return JSONResponse')
        seg = seg[:seg.index(chr(10), end)]
        self.assertIn('blocks', seg)
        self.assertIn('* 4', seg, '没有结构开销项')


if __name__ == '__main__':
    unittest.main()
