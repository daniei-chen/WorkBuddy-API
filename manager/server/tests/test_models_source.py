"""模型列表来源判定的回归测试。

背景：上游 `/v1/models` 优先用池里随机一个健康账号**动态拉取**（成功缓存 1h）；
**老版本上游**在失败时回退到编译进二进制的静态表，两者外观一样但静态表数量少
得多。曾出现「只显示 6 个模型」的情况，界面却标「来自上游实时列表」，让人以为
是自己账号或配置坏了——实际是上游在走回退。

上游 2026-09-15（commit 1b7ce4a）起**删除了静态兜底表**，改为纯动态：取不到就是
空列表。所以这里保留的 `static` 判定只为兼容仍在跑老版本上游的部署——那种情况下
如实标出「非实时」依然是对的，不能因为新上游没有这条路就把判据一并删掉。

判据取上游内部实现细节：动态条目带 `max_output_tokens`（上游 modelList 的
动态分支才写该键），静态表条目没有。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import wb2api  # noqa: E402


def dyn(mid: str) -> dict:
    """动态条目：带 max_output_tokens"""
    return {'id': mid, 'object': 'model', 'created': 1753600000,
            'owned_by': 'workbuddy', 'context_length': 131072,
            'max_output_tokens': 8192}


def static(mid: str) -> dict:
    """静态回退条目：没有 max_output_tokens"""
    return {'id': mid, 'object': 'model', 'created': 1753600000,
            'owned_by': 'workbuddy', 'context_length': 131072}


class ModelsSourceTest(unittest.TestCase):
    def test_dynamic_when_any_entry_has_max_output_tokens(self) -> None:
        self.assertEqual(wb2api.models_source([dyn('glm-5.2'), dyn('kimi-k2.7')]), 'dynamic')

    def test_dynamic_even_if_only_one_entry_has_the_key(self) -> None:
        """上游动态条目都带该键；只要有一条带，整体就是动态。"""
        self.assertEqual(wb2api.models_source([dyn('a'), static('b')]), 'dynamic')

    def test_static_when_no_entry_has_the_key(self) -> None:
        items = [static(x) for x in ('glm-5.2', 'glm-5.1', 'glm-5v-turbo',
                                     'kimi-k2.7', 'minimax-m3', 'hy3')]
        self.assertEqual(wb2api.models_source(items), 'static')

    def test_empty_is_unknown_not_static(self) -> None:
        """空列表不能判定成静态回退——那会把「取不到」误报成「上游在回退」。"""
        self.assertEqual(wb2api.models_source([]), 'unknown')

    def test_non_list_and_garbage_are_unknown(self) -> None:
        for bad in (None, {}, 'x', 42, [1, 2, 3], ['a', None]):
            self.assertEqual(wb2api.models_source(bad), 'unknown', repr(bad))

    def test_entries_without_id_are_unknown(self) -> None:
        """结构不像模型条目时不下结论，交给前端用中性文案。"""
        self.assertEqual(wb2api.models_source([{'foo': 'bar'}]), 'unknown')

    def test_global_entries_are_dynamic_even_without_max_output_tokens(self) -> None:
        """国际版条目可能不带 max_output_tokens，不能因此判成静态回退。

        上游 modelList 的 international 分支只在探测拿到富条目时才写该键；
        而老版本上游的静态表**只覆盖 CN**（裸名 / cn: 前缀），所以出现
        `global:` 前缀即证明这是真实探测结果。误判会让界面错误地告诉用户
        「上游动态拉取失败，已回退静态表」——而实际一切正常。
        """
        items = [{'id': 'global:gpt-5.6-sol', 'object': 'model', 'created': 1753600000,
                  'owned_by': 'workbuddy', 'context_length': 131072}]
        self.assertEqual(wb2api.models_source(items), 'dynamic')

    def test_cn_bare_names_without_key_still_static(self) -> None:
        """老上游 CN 静态表的形态：裸名 + 无 max_output_tokens → 仍判 static。"""
        items = [{'id': 'glm-5.2', 'object': 'model', 'created': 1753600000,
                  'owned_by': 'workbuddy', 'context_length': 131072}]
        self.assertEqual(wb2api.models_source(items), 'static')

    def test_dynamic_entry_with_name_but_no_max_output_tokens(self) -> None:
        """动态条目可能整表都没有 `max_output_tokens`，靠 `name` 仍能认出是动态。

        `max_output_tokens` 是**四级查找全不命中就省略**（上游 handler.go 的
        `MaxOutputTokensListingV4`，兜底是省略字段而不是填默认值），所以理论上存在
        整张表都不带它的部署。而 `name` 的写出条件宽得多（模型对象自带 Name 即可，
        见 `applyModelInfoFields`）。只认前者会在这类部署上误报「上游动态拉取失败，
        已回退静态表」——用户会以为是自己账号或配置坏了。
        """
        items = [{'id': 'cn:glm-5.2', 'object': 'model', 'created': 1753600000,
                  'owned_by': 'workbuddy', 'context_length': 262144,
                  'name': 'GLM-5.2'}]
        self.assertEqual(wb2api.models_source(items), 'dynamic')

    def test_static_table_has_no_name_field(self) -> None:
        """判据的前提：老上游的静态条目**不带** `name`。

        若哪天静态表也带上 name，上面那条判据就会把「回退」误报成「实时」，
        所以把这个前提也钉住（对照上游 `1b7ce4a~1` 的 staticModels 定义：
        条目只有 id/object/created/owned_by/context_length）。
        """
        static_entry = {'id': 'glm-5.2', 'object': 'model', 'created': 1753600000,
                        'owned_by': 'workbuddy', 'context_length': 131072}
        self.assertNotIn('name', static_entry)
        self.assertEqual(wb2api.models_source([static_entry]), 'static')


if __name__ == '__main__':
    unittest.main()
