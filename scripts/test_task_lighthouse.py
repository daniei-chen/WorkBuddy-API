"""Expert_lighthouse 真实对话链（issue #221）。

服务端对这条任务的判据是「召唤三连 + 带 has_expert 的桌面对话六连 +
expert_actual_use」，且 requestId 必须是 chat 接口返回的服务端 id。这里钉住
最容易回归的两点：SSE 解析与事件形状。
"""
import json
import unittest
from unittest import mock

import task_runner as runner


class _Resp:
    status = 200

    def __init__(self, lines):
        self._lines = [line.encode() for line in lines]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)


class LighthouseTest(unittest.TestCase):
    def test_real_chat_takes_server_request_id(self) -> None:
        rid = 'cmb-' + 'a1' * 16
        resp = _Resp([
            'data: {"choices":[{"delta":{"content":"1"}}]}\n',
            'data: {"id":"%s","choices":[]}\n' % rid,
            'data: [DONE]\n',
        ])
        with mock.patch.object(runner.urllib.request, 'urlopen', return_value=resp):
            conv, got, err = runner._lighthouse_real_chat({'uid': 'u1', 'token': 't'}, 'ex_1')
        self.assertEqual(got, rid)
        self.assertEqual(err, '')
        self.assertTrue(conv.startswith('wb2api-conv-'))

    def test_real_chat_rejects_client_made_id(self) -> None:
        resp = _Resp(['data: {"id":"wb-run-123","choices":[]}\n', 'data: [DONE]\n'])
        with mock.patch.object(runner.urllib.request, 'urlopen', return_value=resp):
            _, got, err = runner._lighthouse_real_chat({'uid': 'u1', 'token': 't'}, 'ex_1')
        self.assertEqual(got, '')
        self.assertIn('requestId', err)

    def test_event_shapes_match_reference(self) -> None:
        expert = ('ex_1', {'name': '腾讯轻量云专家', 'category': '02-Engineering',
                           'expertType': 'agent', 'version': '1.0.2'})
        summon = runner.desktop_expert_summon_sequence(expert)
        self.assertEqual([e['eventCode'] for e in summon],
                         ['web_element_click', 'expert_summon_click', 'expert_summoned'])
        self.assertEqual(summon[0]['elementId'], 'expert_summon_click')
        use = runner.desktop_expert_actual_use_local(expert, 'conv', 'cmb-' + 'b2' * 16)
        self.assertEqual(use['mode'], 'LOCAL')
        self.assertEqual(use['type'], '')
        self.assertEqual(use['cost'], 0)
        self.assertEqual(use['requestId'], 'cmb-' + 'b2' * 16)


if __name__ == '__main__':
    unittest.main()
