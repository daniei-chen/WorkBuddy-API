"""Anthropic Messages API 兼容层（/v1/messages）的协议转换测试。

关注点是**翻译是否正确**，不是网络层——鉴权/配额/记账复用网关既有实现，
那部分已有独立用例覆盖。这里只测三个纯函数加一个流式状态机，
全部可离线运行，不需要上游。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server.routers.anthropic import (  # noqa: E402
    _StreamTranslator,
    to_anthropic_response,
    to_openai_request,
)


class RequestTranslationTest(unittest.TestCase):
    """Anthropic 请求 → OpenAI 请求。"""

    def test_thinking_block_kept_as_reasoning_content(self) -> None:
        """thinking 块的推理文本必须**带回** assistant 消息（社区反馈的 11155 死循环）。

        与 Responses 层的 reasoning item 是同一个坑：丢掉推理痕迹 → 上游
        workbuddy2api 的补丁（见到痕迹才给所有 assistant 补 reasoning_content）
        永远不触发 → 腾讯回 11155 → 400 → 账号连败 → 降级冷却 → 池空 → 503 死循环。

        Claude Code 这类客户端会原样回传上一轮的 thinking 块，所以这条路径同样会中招。
        """
        out = to_openai_request({'model': 'x', 'max_tokens': 10, 'messages': [
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': '我的推理过程', 'signature': 'sig'},
                {'type': 'text', 'text': '答复'},
            ]},
        ]})
        msg = out['messages'][0]
        self.assertEqual(msg['role'], 'assistant')
        self.assertEqual(msg['content'], '答复')
        self.assertEqual(msg['reasoning_content'], '我的推理过程')

    def test_thinking_attached_to_tool_use_message(self) -> None:
        """带工具调用的回合也要带上推理（模型先思考再调工具）。"""
        out = to_openai_request({'model': 'x', 'max_tokens': 10, 'messages': [
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': '需要查工具'},
                {'type': 'tool_use', 'id': 't1', 'name': 'f', 'input': {}},
            ]},
        ]})
        msg = out['messages'][0]
        self.assertEqual(msg['tool_calls'][0]['id'], 't1')
        self.assertEqual(msg['reasoning_content'], '需要查工具')

    def test_thinking_only_message_still_emitted(self) -> None:
        """只有 thinking、没有正文时也要落一条 assistant 消息 —— 否则痕迹无处安放。"""
        out = to_openai_request({'model': 'x', 'max_tokens': 10, 'messages': [
            {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': '只有思考'}]},
        ]})
        self.assertEqual(out['messages'][0]['role'], 'assistant')
        self.assertEqual(out['messages'][0]['reasoning_content'], '只有思考')

    def test_empty_thinking_gets_placeholder(self) -> None:
        """thinking 块里没有可用内容时补**一个空格**（不是空串、也不是不挂）。

        判据来自上游 2026-09-19 的 commit 5657229（采纳社区 issue #37 的判定表
        并复验）：部分账号/租户校验 `len(reasoning) > 0` 且**不做 trim** ——
        空白串过闸、空串不过。所以无内容时补空格占位；该字段是转发校验位、
        不是内容消费位，占位不影响模型上下文。
        """
        out = to_openai_request({'model': 'x', 'max_tokens': 10, 'messages': [
            {'role': 'assistant',
             'content': [{'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': '答'}]},
        ]})
        msg = out['messages'][0]
        self.assertEqual(msg.get('reasoning'), ' ', '空 thinking 应补空格占位')
        self.assertEqual(msg.get('reasoning_content'), ' ')
        self.assertGreater(len(msg['reasoning']), 0, '上游校验 len>0')

    def test_redacted_thinking_carries_ciphertext(self) -> None:
        """`redacted_thinking` 只有密文：带上它（字段存在即触发补丁），不能丢。"""
        out = to_openai_request({'model': 'x', 'max_tokens': 10, 'messages': [
            {'role': 'assistant', 'content': [
                {'type': 'redacted_thinking', 'data': 'ENCRYPTED'},
                {'type': 'text', 'text': '答'},
            ]},
        ]})
        self.assertEqual(out['messages'][0]['reasoning_content'], 'ENCRYPTED')

    def test_no_thinking_block_means_no_field(self) -> None:
        """普通对话不得凭空多出 reasoning_content。"""
        out = to_openai_request({'model': 'x', 'max_tokens': 10, 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': '问'}]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': '答'}]},
        ]})
        for m in out['messages']:
            self.assertNotIn('reasoning_content', m)

    def test_system_goes_to_first_message(self) -> None:
        """system 是顶层字段，OpenAI 必须是 messages 里的第一条。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10, 'system': '你是一个助手',
            'messages': [{'role': 'user', 'content': 'hi'}],
        })
        self.assertEqual(out['messages'][0], {'role': 'system', 'content': '你是一个助手'})
        self.assertEqual(out['messages'][1], {'role': 'user', 'content': 'hi'})

    def test_system_as_block_array(self) -> None:
        """system 也允许是 block 数组（带 cache_control 时 SDK 会这么发）。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'system': [{'type': 'text', 'text': '第一段'}, {'type': 'text', 'text': '第二段'}],
            'messages': [{'role': 'user', 'content': 'hi'}],
        })
        self.assertEqual(out['messages'][0]['content'], '第一段\n第二段')

    def test_text_blocks_flatten_to_string(self) -> None:
        """单个纯文本块压平成字符串，多数上游对字符串更宽容。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}],
        })
        self.assertEqual(out['messages'][0]['content'], 'hello')

    def test_image_block_becomes_data_url(self) -> None:
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '看这个'},
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAA'}},
            ]}],
        })
        parts = out['messages'][0]['content']
        self.assertEqual(parts[0], {'type': 'text', 'text': '看这个'})
        self.assertEqual(parts[1]['image_url']['url'], 'data:image/png;base64,AAA')

    def test_tool_use_becomes_tool_calls(self) -> None:
        """assistant 的 tool_use → OpenAI tool_calls，且 input(对象) → arguments(字符串)。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'toolu_1', 'name': 'get_weather', 'input': {'city': '北京'}},
            ]}],
        })
        calls = out['messages'][0]['tool_calls']
        self.assertEqual(calls[0]['id'], 'toolu_1')
        self.assertEqual(calls[0]['function']['name'], 'get_weather')
        # 关键：必须是 JSON 字符串，不是对象
        self.assertIsInstance(calls[0]['function']['arguments'], str)
        self.assertEqual(json.loads(calls[0]['function']['arguments']), {'city': '北京'})

    def test_tool_result_splits_into_own_message(self) -> None:
        """最容易搞错的一处：Anthropic 把工具结果放在 user 消息的 block 里，
        而 OpenAI 要求它是独立的 role:tool 消息，且要排在其余内容之前。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': '25 度'},
                {'type': 'text', 'text': '那要不要带伞'},
            ]}],
        })
        msgs = out['messages']
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]['role'], 'tool')
        self.assertEqual(msgs[0]['tool_call_id'], 'toolu_1')
        self.assertEqual(msgs[0]['content'], '25 度')
        self.assertEqual(msgs[1], {'role': 'user', 'content': '那要不要带伞'})

    def test_tools_schema_mapping(self) -> None:
        """Anthropic 的 {name, input_schema} → OpenAI 的 type:function 包装。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10, 'messages': [],
            'tools': [{'name': 'f', 'description': 'd', 'input_schema': {'type': 'object'}}],
        })
        self.assertEqual(out['tools'][0]['type'], 'function')
        self.assertEqual(out['tools'][0]['function']['name'], 'f')
        self.assertEqual(out['tools'][0]['function']['parameters'], {'type': 'object'})

    def test_tool_choice_variants(self) -> None:
        base = {'model': 'x', 'max_tokens': 10, 'messages': []}
        self.assertEqual(to_openai_request({**base, 'tool_choice': {'type': 'auto'}})['tool_choice'], 'auto')
        self.assertEqual(to_openai_request({**base, 'tool_choice': {'type': 'any'}})['tool_choice'], 'required')
        forced = to_openai_request({**base, 'tool_choice': {'type': 'tool', 'name': 'f'}})
        self.assertEqual(forced['tool_choice']['function']['name'], 'f')

    def test_tool_choice_none_is_forwarded(self) -> None:
        """type=none 是「这一轮别调工具」，漏掉它等于放开了客户端的限制。

        Anthropic 的 none 与 OpenAI 的 'none' 语义一致，直接透传即可。先前只认
        auto/any/tool，none 被静默丢弃——客户端明确要求别调工具，上来的却是默认的
        auto，模型照样可能回 tool_use。
        """
        base = {'model': 'x', 'max_tokens': 10, 'messages': []}
        self.assertEqual(
            to_openai_request({**base, 'tool_choice': {'type': 'none'}})['tool_choice'],
            'none',
        )

    def test_disable_parallel_tool_use_maps_to_parallel_tool_calls(self) -> None:
        """disable_parallel_tool_use 藏在 tool_choice 里，且只在 true 时才写。

        这个开关在 Anthropic 的 tool_choice 对象内（不在顶层），漏掉它的症状是
        「看起来一切正常」：客户端按串行编排，上游却一次回多个 tool_use。

        另一面同样要守：未给该字段时 Anthropic 默认允许并发，若我们无条件写成
        parallel_tool_calls=False，反而把默认行为改掉了——必须只在显式 true 时写。
        """
        base = {'model': 'x', 'max_tokens': 10, 'messages': [], 'tools': [
            {'name': 'f', 'input_schema': {'type': 'object'}},
        ]}

        off = to_openai_request({**base, 'tool_choice': {
            'type': 'auto', 'disable_parallel_tool_use': True,
        }})
        self.assertIs(off['parallel_tool_calls'], False)
        self.assertEqual(off['tool_choice'], 'auto')

        # any / tool 下也要带上该开关（客户端在任何一种选择下都能禁并发）
        repeated = to_openai_request({**base, 'tool_choice': {
            'type': 'any', 'disable_parallel_tool_use': True,
        }})
        self.assertIs(repeated['parallel_tool_calls'], False)

        # 未给该字段、或显式 false：都不该凭空写出 parallel_tool_calls
        for choice in (
            {'type': 'auto'},
            {'type': 'auto', 'disable_parallel_tool_use': False},
        ):
            out = to_openai_request({**base, 'tool_choice': choice})
            self.assertNotIn('parallel_tool_calls', out,
                             f'{choice} 不该改动并发默认值')

    def test_scalar_params(self) -> None:
        out = to_openai_request({
            'model': 'x', 'max_tokens': 55, 'temperature': 0.3, 'top_p': 0.9,
            'stop_sequences': ['\n\n'], 'metadata': {'user_id': 'u1'}, 'messages': [],
        })
        self.assertEqual(out['max_tokens'], 55)
        self.assertEqual(out['temperature'], 0.3)
        self.assertEqual(out['top_p'], 0.9)
        self.assertEqual(out['stop'], ['\n\n'])
        self.assertEqual(out['user'], 'u1')


class ResponseTranslationTest(unittest.TestCase):
    """OpenAI 响应 → Anthropic 响应。"""

    def test_text_and_usage(self) -> None:
        out = to_anthropic_response({
            'choices': [{'message': {'role': 'assistant', 'content': '你好'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 3},
        }, 'claude-x')
        self.assertEqual(out['type'], 'message')
        self.assertEqual(out['role'], 'assistant')
        self.assertEqual(out['model'], 'claude-x')
        self.assertEqual(out['content'], [{'type': 'text', 'text': '你好'}])
        self.assertEqual(out['stop_reason'], 'end_turn')
        # 字段名必须换：Anthropic 用 input/output_tokens
        # 缓存两段是 Anthropic 规范里的并列字段（上游没给时为 0）——
        # 见 anthropic._usage_fields：input_tokens **不含**缓存，客户端会相加。
        self.assertEqual(out['usage'], {
            'input_tokens': 12,
            'cache_read_input_tokens': 0,
            'cache_creation_input_tokens': 0,
            'output_tokens': 3,
        })

    def test_tool_call_becomes_tool_use(self) -> None:
        out = to_anthropic_response({
            'choices': [{
                'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call_1', 'type': 'function',
                     'function': {'name': 'f', 'arguments': '{"a":1}'}},
                ]},
                'finish_reason': 'tool_calls',
            }],
        }, 'm')
        block = out['content'][0]
        self.assertEqual(block['type'], 'tool_use')
        self.assertEqual(block['id'], 'call_1')
        self.assertEqual(block['name'], 'f')
        # 对象，不是字符串
        self.assertEqual(block['input'], {'a': 1})
        self.assertEqual(out['stop_reason'], 'tool_use')

    def test_malformed_arguments_do_not_crash(self) -> None:
        """上游给出非法 JSON 时不能让整次调用失败——包一层 _raw 保留原文。"""
        out = to_anthropic_response({
            'choices': [{'message': {'tool_calls': [
                {'id': 'c', 'function': {'name': 'f', 'arguments': '{"broken'}},
            ]}, 'finish_reason': 'tool_calls'}],
        }, 'm')
        self.assertEqual(out['content'][0]['input'], {'_raw': '{"broken'})

    def test_finish_reason_mapping(self) -> None:
        for oai, anth in (('stop', 'end_turn'), ('length', 'max_tokens'), ('tool_calls', 'tool_use')):
            out = to_anthropic_response(
                {'choices': [{'message': {'content': 'x'}, 'finish_reason': oai}]}, 'm')
            self.assertEqual(out['stop_reason'], anth, oai)

    def test_empty_choices_is_tolerated(self) -> None:
        out = to_anthropic_response({}, 'm')
        self.assertEqual(out['content'], [])
        self.assertEqual(out['stop_reason'], 'end_turn')


def _parse(events: list[bytes]) -> list[tuple[str, dict]]:
    """把事件字节流解析成 (event_name, payload) 列表，便于断言。"""
    out: list[tuple[str, dict]] = []
    for raw in events:
        text = raw.decode('utf-8')
        name = ''
        payload = {}
        for line in text.split('\n'):
            if line.startswith('event:'):
                name = line[6:].strip()
            elif line.startswith('data:'):
                payload = json.loads(line[5:].strip())
        out.append((name, payload))
    return out


class StreamTranslatorTest(unittest.TestCase):
    """OpenAI SSE → Anthropic 事件流。"""

    def test_message_start_emitted_once(self) -> None:
        t = _StreamTranslator('m')
        events = _parse(t.feed({'choices': [{'delta': {'content': 'a'}}]}))
        self.assertEqual(events[0][0], 'message_start')
        # 再喂一片不应重复发 message_start
        events2 = _parse(t.feed({'choices': [{'delta': {'content': 'b'}}]}))
        self.assertNotIn('message_start', [e[0] for e in events2])

    def test_text_delta_sequence(self) -> None:
        t = _StreamTranslator('m')
        events = _parse(t.feed({'choices': [{'delta': {'content': 'hello'}}]}))
        names = [e[0] for e in events]
        self.assertEqual(names, ['message_start', 'content_block_start', 'content_block_delta'])
        self.assertEqual(events[-1][1]['delta'], {'type': 'text_delta', 'text': 'hello'})

    def test_finish_closes_block_and_stops(self) -> None:
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'content': 'x'}}]})
        events = _parse(t.finish('stop'))
        names = [e[0] for e in events]
        self.assertEqual(names, ['content_block_stop', 'message_delta', 'message_stop'])
        self.assertEqual(events[1][1]['delta']['stop_reason'], 'end_turn')

    def test_finish_is_idempotent(self) -> None:
        """收尾只能发生一次——重复调用会在流末尾多吐一份 message_stop。"""
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'content': 'x'}}]})
        first = t.finish('stop')
        second = t.finish('stop')
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_tool_call_uses_input_json_delta(self) -> None:
        """工具参数必须原样分片透传（我们无从判断 JSON 何时拼完整）。"""
        t = _StreamTranslator('m')
        events = _parse(t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'call_1', 'function': {'name': 'f', 'arguments': '{"a'}},
        ]}}]}))
        start = next(e for e in events if e[0] == 'content_block_start')
        self.assertEqual(start[1]['content_block']['type'], 'tool_use')
        self.assertEqual(start[1]['content_block']['name'], 'f')
        delta = next(e for e in events if e[0] == 'content_block_delta')
        self.assertEqual(delta[1]['delta'], {'type': 'input_json_delta', 'partial_json': '{"a'})

    def test_text_to_tool_switches_blocks(self) -> None:
        """文本块与工具块不能并存：切到工具前必须先关掉文本块，索引也不能串。"""
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'content': '先说话'}}]})
        events = _parse(t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'c', 'function': {'name': 'f', 'arguments': '{}'}},
        ]}}]}))
        names = [e[0] for e in events]
        self.assertIn('content_block_stop', names)
        # 文本块 index=0，工具块应是新的 index=1
        tool_start = next(e for e in events if e[0] == 'content_block_start')
        self.assertEqual(tool_start[1]['index'], 1)
        close = next(e for e in events if e[0] == 'content_block_stop')
        self.assertEqual(close[1]['index'], 0)

    def test_usage_captured_from_final_chunk(self) -> None:
        """include_usage 的末帧要喂进 message_delta 的 usage.output_tokens。"""
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'content': 'x'}}], 'usage': {'prompt_tokens': 7, 'completion_tokens': 9}})
        events = _parse(t.finish('stop'))
        msg_delta = next(e for e in events if e[0] == 'message_delta')
        self.assertEqual(msg_delta[1]['usage']['output_tokens'], 9)

    def test_force_finish_infers_tool_use_when_reason_missing(self) -> None:
        """上游没给 finish_reason 但出现过工具调用时，按 tool_use 收尾更贴近实际。"""
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'c', 'function': {'name': 'f', 'arguments': '{}'}},
        ]}}]})
        events = _parse(t.finish(None, force=True))
        msg_delta = next(e for e in events if e[0] == 'message_delta')
        self.assertEqual(msg_delta[1]['delta']['stop_reason'], 'tool_use')


class StreamBufferBoundTest(unittest.TestCase):
    """流式解析缓冲必须有上限。

    正常 SSE 一行一条 `data:`，缓冲里只留半行；但如果上游持续吐出**不含换行**
    的数据，缓冲会一直长下去直至吃光内存。这里把上限钉住。
    """

    def test_buffer_limit_is_defined_and_sane(self) -> None:
        from server.routers.anthropic import MAX_SSE_BUFFER
        # 够大：正常单行 SSE 即使带大段 tool_use 参数也只几十 KB
        self.assertGreaterEqual(MAX_SSE_BUFFER, 256 * 1024)
        # 但有界：不能让单个连接无限占用内存
        self.assertLessEqual(MAX_SSE_BUFFER, 16 * 1024 * 1024)

    def test_over_limit_aborts_instead_of_growing(self) -> None:
        """超限时要中止，而不是继续增长——用源码级断言守住这个分支存在。

        端到端造这个场景需要上游持续输出无换行数据，成本高；这里确认
        保护分支确实写在读取循环里。
        """
        import inspect

        from server.routers import anthropic
        src = inspect.getsource(anthropic.messages)
        self.assertIn('MAX_SSE_BUFFER', src, '读取循环里没有缓冲上限检查')
        # 两处：正常转发分支 + 上游报错分支（报错时也可能持续吐数据）
        self.assertGreaterEqual(src.count('MAX_SSE_BUFFER'), 2,
                                '上游报错分支缺少缓冲上限')


class ModelListShapeTest(unittest.TestCase):
    """`/v1/models` 按请求头分流的形状转换。"""

    def test_openai_shape_to_anthropic(self) -> None:
        from server.routers.gateway import _as_anthropic_models
        out = _as_anthropic_models({'data': [{'id': 'm1', 'created': 0}]})
        self.assertEqual(out['data'][0]['type'], 'model')
        self.assertEqual(out['data'][0]['id'], 'm1')
        self.assertFalse(out['has_more'])
        self.assertEqual(out['first_id'], 'm1')
        self.assertEqual(out['last_id'], 'm1')

    def test_unrecognized_shape_passes_through(self) -> None:
        """认不出的结构原样返回——不在看不懂的响应上动手脚。"""
        from server.routers.gateway import _as_anthropic_models
        weird = {'error': 'nope'}
        self.assertIs(_as_anthropic_models(weird), weird)

    def test_empty_list_is_valid(self) -> None:
        from server.routers.gateway import _as_anthropic_models
        out = _as_anthropic_models({'data': []})
        self.assertEqual(out['data'], [])
        self.assertIsNone(out['first_id'])


class TokenHeaderToleranceTest(unittest.TestCase):
    """令牌取值要宽容——少认一种写法就会表现为「某客户端莫名 401」。

    真实踩到过：Claude Code 配 ANTHROPIC_AUTH_TOKEN 与配 ANTHROPIC_API_KEY
    发的头不同；部分转发工具（ccswitch 等）还会把裸 token 直接放进
    Authorization，或加自己的前缀。这几种都得认。
    """

    def _req(self, headers: dict):
        """构造一个假 request。

        headers 必须模拟 Starlette 的 `Headers`：**大小写不敏感**。
        直接用 dict 会踩坑——dict 的 .get() 区分大小写，而 HTTP 头名不区分，
        于是「X-API-Key」这种真实会被接受的头，在测试里会假失败。
        """
        class _Headers:
            def __init__(self, d): self._d = {k.lower(): v for k, v in d.items()}
            def get(self, key, default=''): return self._d.get(key.lower(), default)
            def keys(self): return list(self._d.keys())

        class _R:
            def __init__(self, h): self.headers = _Headers(h)
        return _R(headers)

    def test_accepts_known_header_shapes(self) -> None:
        # 用列表而非 dict：多个用例的头名只差大小写，放进 dict 会互相覆盖，
        # 表面上写了几条、实际只跑了一条。
        from server.routers.anthropic import _token_candidates
        cases = [
            ({'x-api-key': 'wbk_a'}, 'x-api-key 小写'),
            ({'X-API-Key': 'wbk_a'}, 'x-api-key 大写'),
            ({'x-anthropic-api-key': 'wbk_a'}, 'x-anthropic-api-key'),
            ({'Authorization': 'Bearer wbk_a'}, 'Bearer 规范'),
            ({'authorization': 'bearer wbk_a'}, 'bearer 小写'),
            ({'Authorization': 'wbk_a'}, '裸 token（无 Bearer）'),
        ]
        for headers, label in cases:
            with self.subTest(label=label):
                self.assertEqual(_token_candidates(self._req(headers)), ['wbk_a'], label)

    def test_strips_surrounding_whitespace(self) -> None:
        """复制粘贴常带前后空白，不剥掉就会哈希不匹配。"""
        from server.routers.anthropic import _token_candidates
        self.assertEqual(_token_candidates(self._req({'x-api-key': '  wbk_a\n'})), ['wbk_a'])
        self.assertEqual(_token_candidates(self._req({'Authorization': 'Bearer  wbk_a '})), ['wbk_a'])

    def test_returns_empty_when_absent(self) -> None:
        from server.routers.anthropic import _token_candidates
        self.assertEqual(_token_candidates(self._req({})), [])
        self.assertEqual(_token_candidates(self._req({'Authorization': '   '})), [])

    def test_collects_all_candidates_deduped(self) -> None:
        """候选要**全部**收集并去重，而不是只取第一个。"""
        from server.routers.anthropic import _token_candidates
        got = _token_candidates(self._req({
            'x-api-key': 'sk-other-service',
            'Authorization': 'Bearer wbk_ours',
        }))
        self.assertEqual(got, ['sk-other-service', 'wbk_ours'])
        # 值相同（去掉 Bearer 后）只留一份
        dup = _token_candidates(self._req({
            'Authorization': 'Bearer wbk_same',
            'x-anthropic-api-key': 'wbk_same',
        }))
        self.assertEqual(dup, ['wbk_same'])

    def test_header_names_never_leaks_values(self) -> None:
        """调试日志只记头名——把凭据写进日志等于换个地方泄露。"""
        from server.routers.anthropic import _header_names
        names = _header_names(self._req({'x-api-key': 'wbk_secret_value', 'User-Agent': 'x'}))
        self.assertIn('x-api-key', names)
        self.assertIn('user-agent', names)
        self.assertNotIn('wbk_secret_value', ' '.join(names))


class CountTokensAuthTest(unittest.TestCase):
    """`/v1/messages/count_tokens` 必须鉴权。

    它不产生上游调用、不消耗额度，最容易被当成「无害的估算接口」而漏掉鉴权——
    但那样任何未认证访问者都能借它判断网关是否存活、探测部署形态，也与其余
    端点「一律先验密钥」的约定不一致。这组用例把它钉死。
    """

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        from fastapi.testclient import TestClient

        from server import config, db, keysvc

        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = cls._dir / 'ct.db'
        config.USERS_FILE = cls._dir / 'users.json'
        config.STATIC_DIR = cls._dir / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')

        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app
        cls.key = keysvc.create_key(name='count-tokens-test')['key']
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def test_rejects_missing_token(self) -> None:
        r = self.c.post('/v1/messages/count_tokens', json={'messages': []})
        self.assertEqual(r.status_code, 401)
        # 错误体要是 Anthropic 形状，客户端才读得懂
        self.assertEqual(r.json()['type'], 'error')

    def test_rejects_invalid_token(self) -> None:
        r = self.c.post('/v1/messages/count_tokens', json={'messages': []},
                        headers={'x-api-key': 'wbk_bogus'})
        self.assertEqual(r.status_code, 401)

    def test_accepts_valid_token_via_either_header(self) -> None:
        """两种传法都要能用——Anthropic SDK 用 x-api-key，Claude Code 用 Bearer。"""
        for headers in ({'x-api-key': self.key}, {'Authorization': f'Bearer {self.key}'}):
            r = self.c.post('/v1/messages/count_tokens',
                            json={'messages': [{'role': 'user', 'content': 'hi'}]},
                            headers=headers)
            self.assertEqual(r.status_code, 200, headers)
            self.assertIn('input_tokens', r.json())

    def test_messages_endpoint_also_requires_token(self) -> None:
        """主端点同理——防止将来重构时把它俩中的一个漏掉。"""
        r = self.c.post('/v1/messages',
                        json={'model': 'm', 'max_tokens': 1, 'messages': []})
        self.assertEqual(r.status_code, 401)

    def test_picks_the_credential_that_resolves(self) -> None:
        """客户端同时带多份凭据时，要挑出**能解析**的那一份。

        实测踩到：Claude Code 既发 `x-api-key`（值是另一个服务、`sk-` 开头）
        又发 `Authorization: Bearer`（值才是本网关的 `wbk_`）。只取第一个就会
        拿到不相干的那份，表现为「配对了却一直 401」，而客户端侧完全看不出问题。
        """
        r = self.c.post('/v1/messages/count_tokens',
                        json={'messages': [{'role': 'user', 'content': 'hi'}]},
                        headers={
                            'x-api-key': 'sk-some-other-service-key',
                            'Authorization': f'Bearer {self.key}',
                        })
        self.assertEqual(r.status_code, 200, '应回退到 authorization 里那份有效凭据')
        self.assertIn('input_tokens', r.json())

    def test_bogus_x_api_key_alone_still_rejected(self) -> None:
        """只有无效凭据时依然拒绝——挑不出来就老实 401，不能放行。"""
        r = self.c.post('/v1/messages/count_tokens',
                        json={'messages': []},
                        headers={'x-api-key': 'sk-some-other-service-key'})
        self.assertEqual(r.status_code, 401)

    def test_disabled_key_cannot_reach_count_tokens(self) -> None:
        """停用的密钥必须被 count_tokens 拒绝（审计发现的真漏洞）。

        这个端点早先只挑了个「能解析的密钥」就放行，**从不调 keysvc.validate**
        —— 管理员停用泄露的密钥后，`/v1/messages` 返回 403，`count_tokens`
        却仍然 200，等于吊销不彻底。
        """
        from server import keysvc

        created = keysvc.create_key(name='to-disable')
        kid, token = created['id'], created['key']
        try:
            r = self.c.post('/v1/messages/count_tokens', json={'messages': []},
                            headers={'x-api-key': token})
            self.assertEqual(r.status_code, 200, '启用状态下应可用')

            keysvc.update_key(kid, {'enabled': False})

            r = self.c.post('/v1/messages/count_tokens', json={'messages': []},
                            headers={'x-api-key': token})
            self.assertEqual(r.status_code, 403, '停用的密钥仍能调用 count_tokens')
        finally:
            keysvc.delete_key(kid)

    def test_count_tokens_rejects_oversized_body(self) -> None:
        """count_tokens 同样受请求体上限约束（审计发现的真漏洞）。

        早先它直接 `await request.json()`，没有 `gateway._read_json_body` 的
        体积检查 —— 持密钥者发一个超大 body 就能把网关内存打满。

        体积按**当前生效的上限**算，不写死数字：上限的默认值会随上游变化调整
        （上游 9d1a21b 移除 max_body_mb 后，本端默认从 8MB 提到 32MB），写死的话
        这条测试会静默失效 —— 而它测的是内存防护，失效了没人会注意到。
        """
        from server.routers import gateway
        limit_mb = gateway.max_body_bytes() // 1024 // 1024
        big = 'x' * ((limit_mb + 1) * 1024 * 1024)   # 超过当前上限 1 MiB
        r = self.c.post('/v1/messages/count_tokens',
                        json={'messages': [{'role': 'user', 'content': big}]},
                        headers={'x-api-key': self.key})
        self.assertEqual(r.status_code, 413, '超大请求体应被拒（413）而不是先读进内存')


class ToolResultImageTest(unittest.TestCase):
    """tool_result 内嵌图片：旧实现把 base64 序列化成文本发给上游。

    事故现场：Claude Code 的 Read 读一张 2.3MB 的 PNG，tool_result 里只有
    image 块没有 text 块 → `_text_of(...) or str(content)` 落到兜底分支，
    整段 base64 连同结构标记被压成 Python repr 文本发上游。上游按文本 BPE
    分词，实测 3.36MB 的图算出 2,349,045 token（> 1M 上限）报 prompt too long；
    且该 tool_use/tool_result 对被自动压缩原样保留，会话彻底卡死。
    修法：tool 消息只留文字，图片提升为紧随其后的 user 消息。
    """

    def _convert(self, tool_content: list, extra_blocks: list | None = None) -> list:
        blocks = [{'type': 'tool_result', 'tool_use_id': 'toolu_x',
                   'content': tool_content}]
        blocks.extend(extra_blocks or [])
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': blocks}],
        })
        return out['messages']

    def test_image_only_result_does_not_leak_base64(self) -> None:
        """纯图片：tool.content 必须是占位文字，图片以 data URL 提升到 user 消息。"""
        data = 'iVBORw0KGgo' + 'A' * 100000
        msgs = self._convert([
            {'type': 'image', 'source': {
                'type': 'base64', 'media_type': 'image/png', 'data': data}}])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]['role'], 'tool')
        self.assertEqual(msgs[0]['content'], '[图片]')
        self.assertEqual(msgs[1]['role'], 'user')
        self.assertEqual(msgs[1]['content'][0]['image_url']['url'],
                         f'data:image/png;base64,{data}')

    def test_text_and_image_keeps_both(self) -> None:
        """文字 + 图片：文字留在 tool 消息，图片提升到 user 消息。"""
        msgs = self._convert([
            {'type': 'text', 'text': '截图如下'},
            {'type': 'image', 'source': {
                'type': 'base64', 'media_type': 'image/jpeg', 'data': 'BBBB'}}])
        self.assertEqual(msgs[0]['content'], '截图如下')
        self.assertEqual(msgs[1]['content'][0]['image_url']['url'],
                         'data:image/jpeg;base64,BBBB')

    def test_image_merges_with_following_text(self) -> None:
        """提升的图片应与同条消息的后续文本合并为一条 user 消息。"""
        msgs = self._convert(
            [{'type': 'image', 'source': {
                'type': 'base64', 'media_type': 'image/png', 'data': 'CCCC'}}],
            [{'type': 'text', 'text': '这张图什么颜色'}])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]['content'][0]['type'], 'image_url')
        self.assertEqual(msgs[1]['content'][1],
                         {'type': 'text', 'text': '这张图什么颜色'})

    def test_unknown_shape_still_stringified(self) -> None:
        """既无文字也无图片的未知形态：保持旧兜底，不因本次修复丢内容。"""
        msgs = self._convert([{'type': 'document', 'data': 'weird'}])
        self.assertEqual(len(msgs), 1, '无图片则不应产生额外的 user 消息')
        self.assertIn('document', msgs[0]['content'])

    def test_large_image_never_becomes_text(self) -> None:
        """回归底线：3MB 级图片不得以字符串形态出现在任何消息里。"""
        data = 'A' * 3_000_000
        msgs = self._convert([
            {'type': 'image', 'source': {
                'type': 'base64', 'media_type': 'image/png', 'data': data}}])
        tool_len = len(msgs[0]['content'])
        self.assertLess(tool_len, 1000, f'tool 消息疑似携带图片数据（{tool_len} 字符）')
        self.assertIsInstance(msgs[1]['content'], list,
                              '图片须以结构化 image_url 块承载，而非字符串')
        url = msgs[1]['content'][0]['image_url']['url']
        self.assertTrue(url.startswith('data:image/png;base64,'))


if __name__ == '__main__':
    unittest.main()
