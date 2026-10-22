"""密钥列表对损坏数据的容错（用户报告的「新建报 500 但实际创建成功」）。

**根因**：`ip_allowlist` / `models` 是库里的 JSON 文本列，而 `_parse` 直接
`json.loads` 且无保护。只要**任意一把**密钥的这两列存了非法 JSON，整个
`GET /api/keys` 就抛 JSONDecodeError → 500。

**为什么伪装成「创建失败」**：前端点「创建」后先 POST（成功、密钥已入库），
紧接着刷新列表才炸。用户看到 Internal Server Error，以为没建成，再点一次
就多一把重复的——数据明明进去了，症状极具误导性。

坏数据从哪来：历史版本写入过、手工改过库、写入被截断、编码变更。都不需要
是「恶意」的，所以这里一律容错。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc  # noqa: E402


class CorruptKeyDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _corrupt(self, key_id: int, column: str, value: str) -> None:
        assert column in ('ip_allowlist', 'models')
        db.execute(f'UPDATE api_keys SET {column} = ? WHERE id = ?', (value, key_id))

    def test_list_survives_broken_ip_json(self) -> None:
        """核心回归：一列坏数据不能让整个列表 500。"""
        k = keysvc.create_key('正常')
        self._corrupt(k['id'], 'ip_allowlist', '["10.0.0.0/8"')   # 少一个右括号
        out = keysvc.list_keys()
        self.assertEqual(len(out), 1, '列表应仍能返回，而不是抛异常')

    def test_list_survives_broken_models_json(self) -> None:
        k = keysvc.create_key('正常')
        self._corrupt(k['id'], 'models', 'glm-5.2, kimi-k2.7')     # 根本不是 JSON
        self.assertEqual(len(keysvc.list_keys()), 1)

    def test_other_keys_still_readable(self) -> None:
        """坏数据只影响它自己那一把，不牵连其他密钥。"""
        bad = keysvc.create_key('坏的')
        good = keysvc.create_key('好的', ip_allowlist=['1.2.3.4'], models=['glm-5.2'])
        self._corrupt(bad['id'], 'ip_allowlist', '{{{')
        by_id = {k['id']: k for k in keysvc.list_keys()}
        self.assertEqual(by_id[good['id']]['ip_allowlist'], ['1.2.3.4'])
        self.assertEqual(by_id[good['id']]['models'], ['glm-5.2'])

    def test_create_still_works_next_to_corrupt_row(self) -> None:
        """创建不受既有坏数据影响——这正是「报错但创建成功」的现场。"""
        bad = keysvc.create_key('坏的')
        self._corrupt(bad['id'], 'ip_allowlist', 'oops')
        created = keysvc.create_key('新的', ip_allowlist=['10.0.0.0/8'])
        self.assertEqual(created['ip_allowlist'], ['10.0.0.0/8'])
        self.assertEqual(len(keysvc.list_keys()), 2)

    def test_corrupt_value_degrades_to_empty(self) -> None:
        k = keysvc.create_key('t', ip_allowlist=['1.2.3.4'])
        self._corrupt(k['id'], 'ip_allowlist', 'not json')
        row = keysvc.resolve(keysvc.create_key('probe')['key'])  # 触发一次解析路径
        self.assertIsInstance(row, dict)
        got = [x for x in keysvc.list_keys() if x['id'] == k['id']][0]
        self.assertEqual(got['ip_allowlist'], [], '坏数据降级为空列表（不限制）')

    def test_various_malformed_shapes(self) -> None:
        """各种坏形态都不能炸：非 JSON、JSON 但不是数组、null、空、超长垃圾。"""
        for i, bad in enumerate(['[', 'null', '{}', '"str"', '123', 'x' * 5000, '[]']):
            k = keysvc.create_key(f'k{i}')
            self._corrupt(k['id'], 'models', bad)
        out = keysvc.list_keys()
        self.assertEqual(len(out), 7, f'有坏数据时列表不该失败：{out}')

    def test_valid_values_unchanged(self) -> None:
        """正常数据必须原样读回——容错不能把好数据也吞了。"""
        k = keysvc.create_key('t', ip_allowlist=['10.0.0.0/8', '1.2.3.4'],
                              models=['glm-5.2', 'kimi-k2.7'])
        got = [x for x in keysvc.list_keys() if x['id'] == k['id']][0]
        self.assertEqual(got['ip_allowlist'], ['10.0.0.0/8', '1.2.3.4'])
        self.assertEqual(got['models'], ['glm-5.2', 'kimi-k2.7'])

    def test_json_list_helper_directly(self) -> None:
        """直接测辅助函数：覆盖库列可能出现的各种类型。"""
        f = keysvc._json_list
        self.assertEqual(f('["a","b"]'), ['a', 'b'])
        self.assertEqual(f(['a', 'b']), ['a', 'b'])
        self.assertEqual(f(''), [])
        self.assertEqual(f(None), [])
        self.assertEqual(f('broken'), [])
        self.assertEqual(f('{"a":1}'), [])       # JSON 对象不是列表
        self.assertEqual(f('"just-a-string"'), [])
        self.assertEqual(f(123), [])
        self.assertEqual(f('["ok", "", null]'), ['ok'])   # 剔空值

    def test_validate_does_not_crash_on_degraded_data(self) -> None:
        """降级后的数据要能安全地走完校验（不能在校验里再炸一次）。"""
        k = keysvc.create_key('t', ip_allowlist=['1.2.3.4'], models=['glm-5.2'])
        self._corrupt(k['id'], 'ip_allowlist', 'bad')
        self._corrupt(k['id'], 'models', 'bad')
        row = keysvc.resolve(k['key'])
        self.assertIsNotNone(row, '密钥仍应能解析（用于鉴权）')
        # 坏数据降级为空 = 不限制，因此这个来源 IP 与模型应被放行
        self.assertIsNone(keysvc.validate(row, '9.9.9.9', 'other-model'))


if __name__ == '__main__':
    unittest.main()
