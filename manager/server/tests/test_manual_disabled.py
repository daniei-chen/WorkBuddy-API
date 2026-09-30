"""账号「临时停用」优先走上游 manual_disabled 状态位（issue #45）。

## 两条机制为什么必须并存

上游 2026-09-19 才暴露 `POST /admin/accounts/{uid}/disable|enable`（其
`internal/server/admin.go`），语义是**「对话流量摘除」而非「账号冻结」**：

  · 不被选中转发（其 `pick.go` 把 `e.disabled || e.manualDisabled` 一并排除）；
  · 但**签到 / token 保活 / 猫猫旅行 / 活跃上报照常执行**，凭证与积分都是活的；
  · 持久化在 state.json，重启保留运维意图。

而本面板原来的实现是**改文件名**（加 `.disabled` 后缀，issue #21）：账号不再匹配
上游的 `workbuddy*.json` glob，于是**完全退出账号池**——连排程任务都不再跑。
对「这个号在拖后腿，先停一会儿」这种用法，副作用过大：积分不再增长、token
不再续期，回来时可能已经过期。

## 但回退必须保留

上游那组接口**默认不注册**（`admin.enabled` 默认 false；其 `config.go` 的注释写明
关闭时 `/admin/*` 一律 404 而不是 403，理由是「不向外暴露管理面」）。旧版本更是
根本没有这组路由。没有回退的话，这些部署上「停用」按钮会直接失效——比副作用大
更糟。所以：先试状态位，`no_route` 时回退改名，并把降级**如实告诉用户**。

## 这里钉住的几条

  1. **404 要分清两种**：接口没注册（纯文本 `404 page not found`）vs 账号不在池里
     （JSON `{"error":{"code":"not_found"}}`）。前者要回退，后者是真错误——搞反了
     就会在不该回退时改名（用户意图丢失），或在该回退时报错（按钮失效）。
  2. **停用成功后不改文件名**：改了名账号就退出账号池，正好抵消了走状态位的意义。
  3. **启用要两条路都清**：手工改过文件、或跨版本升级后可能出现叠加态。
  4. **状态位透传到前端**：`/status` 的 `manual_disabled` / `manual_reason` 必须
     出现在账号列表里，否则面板看不出被摘过（会显示成「在线」）。
  5. **前端分档**：`manual_disabled` 要单独成一档（与改名的 `disabledByPanel`
     并列），且**不禁用**签到 / 测试 / 刷新按钮——任务照常正是这条路的意义所在。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import wb2api  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
UID = '9b212d8c-f5f7-4ad6-aa20-1d576508c8c1'


class _Resp:
    """假的 httpx 响应（只用到 status_code / text）。"""

    def __init__(self, status: int, text: str = '') -> None:
        self.status_code = status
        self.text = text


class _Client:
    """假的 httpx.AsyncClient：记录请求，返回预置响应。"""

    def __init__(self, resp: _Resp, calls: list) -> None:
        self._resp = resp
        self._calls = calls

    async def __aenter__(self) -> '_Client':
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, **kw: object) -> _Resp:
        self._calls.append((url, kw))
        return self._resp


def _call(resp: _Resp, uid: str = UID, disabled: bool = True,
          reason: str = '') -> tuple[tuple, list]:
    """跑一次 set_manual_disabled，返回 ((ok, msg, code), 请求记录)。"""
    calls: list = []

    def fake_client(*a: object, **kw: object) -> _Client:
        return _Client(resp, calls)

    with mock.patch.object(config, 'http_client', fake_client):
        out = asyncio.run(wb2api.set_manual_disabled(uid, disabled, reason))
    return out, calls


class SetManualDisabledTest(unittest.TestCase):
    def test_success_maps_disable_and_enable_to_right_route(self) -> None:
        ok, msg, code = _call(_Resp(200, '{}'), disabled=True)[0]
        self.assertTrue(ok)
        self.assertEqual(code, 'ok')
        self.assertIn('签到', msg, '成功文案没说清「任务照常」——那正是这条路的意义')
        ok2, _, code2 = _call(_Resp(200, '{}'), disabled=False)[0]
        self.assertTrue(ok2)
        self.assertEqual(code2, 'ok')

    def test_route_missing_plain_text_404_is_no_route(self) -> None:
        """接口没注册时上游返回**纯文本** 404（net/http 默认兜底），要回退。"""
        (ok, _msg, code), _ = _call(_Resp(404, '404 page not found\n'))
        self.assertFalse(ok)
        self.assertEqual(code, 'no_route')

    def test_account_not_found_json_404_is_not_no_route(self) -> None:
        """账号不在池里时是 JSON 404（writeOpenAIError），**不能**当接口缺失回退。

        搞反的后果：一个真错误被静默降级成改名，而改名同样救不回这个账号，
        用户还以为是「旧上游」——排查方向被带偏。
        """
        body = json.dumps({'error': {'message': 'account not found: x',
                                     'type': 'api_error', 'code': 'not_found'}})
        (ok, _msg, code), _ = _call(_Resp(404, body))
        self.assertFalse(ok)
        self.assertEqual(code, 'not_found')

    def test_empty_404_body_treated_as_route_missing(self) -> None:
        """空体判不出来时保守按「接口没注册」——回退改名仍实现用户意图。"""
        (ok, _msg, code), _ = _call(_Resp(404, ''))
        self.assertFalse(ok)
        self.assertEqual(code, 'no_route')

    def test_401_reports_auth_problem(self) -> None:
        (ok, msg, code), _ = _call(_Resp(401, '{"error":{"code":"invalid_api_key"}}'))
        self.assertFalse(ok)
        self.assertEqual(code, 'error')
        self.assertIn('鉴权', msg)

    def test_5xx_is_error_not_no_route(self) -> None:
        (ok, _msg, code), _ = _call(_Resp(500, 'boom'))
        self.assertFalse(ok)
        self.assertEqual(code, 'error', '5xx 不能被当成「接口不存在」而回退改名')

    def test_network_failure_is_error(self) -> None:
        def boom(*a: object, **kw: object) -> object:
            raise OSError('connection refused')

        with mock.patch.object(config, 'http_client', boom):
            ok, _msg, code = asyncio.run(wb2api.set_manual_disabled(UID, True))
        self.assertFalse(ok)
        self.assertEqual(code, 'error')

    def test_empty_uid_short_circuits(self) -> None:
        """uid 为空时不发请求（否则会打到 `/admin/accounts//disable`）。"""
        (ok, _msg, code), calls = _call(_Resp(200, '{}'), uid='')
        self.assertFalse(ok)
        self.assertEqual(code, 'error')
        self.assertEqual(calls, [], 'uid 为空时不该发请求')

    def test_disable_sends_reason_enable_does_not(self) -> None:
        """停用可带 reason（上游会回显在 manual_reason）；启用不带。"""
        _out, calls = _call(_Resp(200, '{}'), disabled=True, reason='在拖后腿')
        url, kw = calls[0]
        self.assertTrue(url.endswith(f'/admin/accounts/{UID}/disable'), url)
        self.assertEqual(kw['json'], {'reason': '在拖后腿'})
        _out2, calls2 = _call(_Resp(200, '{}'), disabled=False)
        url2, kw2 = _call.__name__ and calls2[0]
        self.assertTrue(url2.endswith(f'/admin/accounts/{UID}/enable'), url2)
        self.assertEqual(kw2['json'], {}, '启用不该带 reason（上游会清空它）')

    def test_sends_auth_header(self) -> None:
        """必须带 Bearer（上游 withAuth 与 /status 同源，不配 api_key 时它自己也启动不了）。"""
        with mock.patch.object(config, 'upstream_api_key', lambda: 'k123'):
            _out, calls = _call(_Resp(200, '{}'))
        self.assertEqual(calls[0][1]['headers'].get('Authorization'), 'Bearer k123')


class ReadAnyFormTest(unittest.TestCase):
    """uid 解析要容忍两种文件名形态。

    调用方手里的名字可能与磁盘形态不一致（刚停用/刚启用时界面仍持旧名，
    心跳刷新前一直如此）。只认一个名字就会读不到，uid 解析成空串，
    依赖 uid 的上游状态位那条路被**静默跳过** —— 表现为「点了启用但没恢复」。
    这条测试是上一条集成测试的单元级对应物。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.dir

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _write(self, name: str) -> None:
        (self.dir / name).write_text(json.dumps({
            'account': {'uid': UID}, 'auth': {'accessToken': 't', 'expiresAt': 1},
        }), encoding='utf-8')

    def test_reads_disabled_form_when_asked_for_plain(self) -> None:
        self._write(f'workbuddy-{UID}.json.disabled')
        raw = wb2api.read_account_file_any(f'workbuddy-{UID}.json')
        self.assertEqual(raw['account']['uid'], UID)

    def test_reads_plain_form_when_asked_for_disabled(self) -> None:
        """反向也要通：刚启用、界面还持 `.disabled` 名字。"""
        self._write(f'workbuddy-{UID}.json')
        raw = wb2api.read_account_file_any(f'workbuddy-{UID}.json.disabled')
        self.assertEqual(raw['account']['uid'], UID)

    def test_both_missing_raises(self) -> None:
        """两种形态都没有时仍然报错 —— 容忍不等于放水。"""
        with self.assertRaises(FileNotFoundError):
            wb2api.read_account_file_any(f'workbuddy-{UID}.json')

    def test_traversal_still_rejected(self) -> None:
        """兜底不能成为目录穿越的新入口。"""
        with self.assertRaises(ValueError):
            wb2api.read_account_file_any('../workbuddy-x.json')


class StatusPassthroughTest(unittest.TestCase):
    """`/status` 的手动停用位必须透传到账号列表（否则面板看不出被摘过）。"""

    def test_flags_copied_from_pool(self) -> None:
        accounts = [{'uid': UID, 'file': 'workbuddy-x.json'}]
        wb2api.merge_pool_status(accounts, {'accounts': [
            {'uid': UID, 'manual_disabled': True, 'manual_reason': '在拖后腿'},
        ]})
        self.assertTrue(accounts[0]['manual_disabled'])
        self.assertEqual(accounts[0]['manual_reason'], '在拖后腿')

    def test_absent_fields_default_to_false(self) -> None:
        """上游对**叠加态**两个字段分别透出；缺省时按未停用处理，不能是 None。

        前端判据是 `=== true`，None 也能过；但统一成 bool 让类型契约干净，
        也避免别的调用方（首页快照）拿到 None 后各写各的兜底。
        """
        accounts = [{'uid': UID, 'file': 'workbuddy-x.json'}]
        wb2api.merge_pool_status(accounts, {'accounts': [{'uid': UID}]})
        self.assertIs(accounts[0]['manual_disabled'], False)
        self.assertEqual(accounts[0]['manual_reason'], '')

    def test_manual_and_auto_disabled_are_independent(self) -> None:
        """两个位独立：上游明确要求分开透出（叠加态下用户要能同时看到两种原因）。"""
        accounts = [{'uid': UID, 'file': 'workbuddy-x.json'}]
        wb2api.merge_pool_status(accounts, {'accounts': [
            {'uid': UID, 'manual_disabled': True, 'manual_reason': '我摘的',
             'disabled': True, 'disabled_reason': '11140 request illegal'},
        ]})
        self.assertTrue(accounts[0]['manual_disabled'])
        self.assertTrue(accounts[0]['disabled'])
        self.assertEqual(accounts[0]['manual_reason'], '我摘的')
        self.assertEqual(accounts[0]['disabled_reason'], '11140 request illegal')

    def test_not_in_pool_keeps_none(self) -> None:
        """账号不在池里时没有这两个字段可读——不能凭空断言「没被停用」。"""
        accounts = [{'uid': UID, 'file': 'workbuddy-x.json'}]
        wb2api.merge_pool_status(accounts, {'accounts': []})
        self.assertIsNone(accounts[0].get('manual_disabled'))


class FrontendContractTest(unittest.TestCase):
    """前端契约（TS 侧无法在这里跑，故按源码断言关键结构，防回归）。"""

    def _read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding='utf-8')

    def test_status_module_has_dedicated_tier(self) -> None:
        """`manual_disabled` 必须单独成一档，不能并进 disabledByPanel。

        两者文案不同（任务停不停），并档就意味着其中一条说谎。
        """
        src = self._read('web/lib/account-status.ts')
        self.assertIn("| 'manualDisabled'", src, '没有 manualDisabled 分档')
        self.assertIn("if (a.manual_disabled === true) return 'manualDisabled';", src)
        # 必须在 disabled 之前判：它也是「主动停用」，落到 disabled 会被标红成故障
        seg = src[src.index('export function availabilityOf'):]
        seg = seg[:seg.index('\n}')]
        self.assertLess(seg.index("'disabledByPanel'"), seg.index("'manualDisabled'"))
        self.assertLess(seg.index("'manualDisabled'"), seg.index("'disabled'"),
                        'manualDisabled 要在 disabled 之前判，否则被当成「系统禁用」')

    def test_label_and_title_keys_wired(self) -> None:
        src = self._read('web/lib/account-status.ts')
        self.assertIn("'accounts.badgeManualDisabled'", src)
        self.assertIn("'accounts.badgeManualDisabledTitle'", src)

    def test_availability_title_includes_manual_tier(self) -> None:
        """提示文案要能查到（返回 null 会让界面没有悬停解释）。"""
        src = self._read('web/lib/account-status.ts')
        seg = src[src.index('export function availabilityTitleKey'):]
        seg = seg[:seg.index('\n}')]
        self.assertIn("case 'manualDisabled':", seg)
        self.assertIn("case 'disabledByPanel':", seg)

    def test_actions_stay_available_for_manual_disable(self) -> None:
        """状态位停用时**不能**把签到/测试/刷新按钮藏起来。

        那条路的重点就是「任务照常」；藏掉按钮会让用户以为签到也停了，
        正好把它与改名那条的区别抹掉（这正是 issue #45 要解决的问题）。
        """
        src = self._read('web/app/(main)/accounts/page.tsx')
        self.assertIn('const viaBit = a.manual_disabled === true;', src)
        self.assertIn('(!off || viaBit)', src,
                      '动作按钮的显示条件没给状态位停用留出口')

    def test_dashboard_counts_the_new_tier(self) -> None:
        """首页快照的计数表要覆盖新分档，否则 TS 的 Record 类型不完整。"""
        src = self._read('web/app/(main)/dashboard/page.tsx')
        self.assertIn('manualDisabled: 0', src)

    def test_all_locales_define_the_keys(self) -> None:
        """五个语言都要有这两条文案（缺一条会在界面上显示成 key 本身）。"""
        base = ROOT / 'web/lib/i18n/locales'
        for f in sorted(base.glob('*.json')):
            data = json.loads(f.read_text(encoding='utf-8'))
            acct = data.get('accounts', {})
            for key in ('badgeManualDisabled', 'badgeManualDisabledTitle'):
                self.assertIn(key, acct, f'{f.name} 缺 {key}')
                self.assertTrue(str(acct[key]).strip(), f'{f.name} 的 {key} 是空的')


class RouteIntegrationTest(unittest.TestCase):
    """路由层：先试状态位、失败回退改名，且回退要如实告知。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.dir
        self.fname = f'workbuddy-{UID}.json'
        (self.dir / self.fname).write_text(json.dumps({
            'account': {'uid': UID, 'nickname': '测试号', 'enterpriseId': 'e'},
            'auth': {'accessToken': 'tok', 'refreshToken': 'ref',
                     'expiresAt': 4102444800, 'domain': 'copilot.tencent.com'},
        }, ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _run(self, disabled: bool, resp: _Resp) -> tuple[dict, list]:
        from server.routers import accounts as acct_router
        calls: list = []

        def fake_client(*a: object, **kw: object) -> _Client:
            return _Client(resp, calls)

        with mock.patch.object(config, 'http_client', fake_client), \
             mock.patch.object(acct_router.reload, 'request_restart', lambda: True):
            out = asyncio.run(acct_router.account_set_disabled(
                self.fname, {'disabled': disabled}, {'username': 'admin'}))
        return out, calls

    def test_state_bit_success_does_not_rename(self) -> None:
        """走状态位成功后**不能**改文件名——改了就等于退出账号池，白走这条路。"""
        out, calls = self._run(True, _Resp(200, '{}'))
        self.assertEqual(out['via'], 'manual_disabled')
        self.assertFalse(out['reload_triggered'], '状态位由上游自己管，不需要重载')
        self.assertTrue((self.dir / self.fname).exists(), '文件被改名了（账号会退出池）')
        self.assertEqual([p.name for p in self.dir.iterdir()], [self.fname])
        self.assertTrue(any('/disable' in u for u, _ in calls))

    def test_fallback_to_rename_and_says_so(self) -> None:
        """接口没注册时回退改名，且消息要说明代价（任务会一并停）。"""
        out, _calls = self._run(True, _Resp(404, '404 page not found\n'))
        self.assertEqual(out['via'], 'rename')
        self.assertEqual(out['bit_code'], 'no_route')
        self.assertTrue((self.dir / (self.fname + '.disabled')).exists())
        self.assertIn('改名', out['message'], '降级没告诉用户用了哪种机制')
        self.assertIn('签到', out['message'], '没说明代价：任务会一并停止')

    def test_enable_clears_both_mechanisms(self) -> None:
        """启用要两条路都清：叠加态（改过名 + 状态位）也点了就能真的启用。"""
        # 造出叠加态：文件处于改名状态，同时上游状态位也是停用
        (self.dir / self.fname).rename(self.dir / (self.fname + '.disabled'))
        out, _calls = self._run(False, _Resp(200, '{}'))
        self.assertEqual(out['via'], 'manual_disabled')
        self.assertTrue((self.dir / self.fname).exists(), '改名标记没清掉')
        self.assertFalse((self.dir / (self.fname + '.disabled')).exists())

    def test_reason_forwarded_to_upstream(self) -> None:
        _out, calls = self._run(True, _Resp(200, '{}'))
        self.assertEqual(calls[0][1]['json'], {'reason': '面板手动停用'})

    def test_non_bool_disabled_rejected(self) -> None:
        from server.routers import accounts as acct_router
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            asyncio.run(acct_router.account_set_disabled(
                self.fname, {'disabled': 'yes'}, {'username': 'admin'}))

    def test_fallback_message_says_where_to_enable(self) -> None:
        """回退提示必须给出**可操作的下一步**，不能只说「未启用」。

        实测反馈（issue #45 的追加评论）：用户看到
        「该上游未启用管理接口，已改用改名方式…」之后不知道去哪儿开，
        只能回来问。提示里要写明开关位置与生效条件。
        """
        out, _calls = self._run(True, _Resp(404, '404 page not found\n'))
        self.assertEqual(out['bit_code'], 'no_route')
        msg = out['message']
        self.assertIn('设置', msg, '提示里没说要到哪里开启')
        self.assertIn('重启', msg, '没说清改动需要重启才生效')
        self.assertIn('改名', msg, '没说明用了哪种方式')
        self.assertIn('签到', msg, '没说明代价（任务会停）')


class AdminSectionEditableTest(unittest.TestCase):
    """上游的 `admin.enabled` 必须能在面板里开（issue #45 的追加反馈）。

    为什么必须做成开关：上游这组管理接口默认不注册，而它带来的正是 issue #45
    想要的效果（停用只摘流量、签到与保活照常）。只能在 config.json 里手改的话，
    绝大多数用户不会去开——于是「临时停用」永远走回退路径，issue #45 的诉求
    实际上没被满足。用户反馈的原话就是「我更新了最新版本，点击临时停用，显示
    （回退提示）」。

    另有一条**安全约束**必须一起守：上游对 `admin.enabled=true` 且 `api_key`
    为空是 **fail-fast 拒绝启动**（它要求管理端点必须鉴权）。放行这种组合会得到
    「保存成功，然后上游起不来」这个最难查的形态。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / 'config.json'
        self._orig_cfg = config.UPSTREAM_CONFIG
        self._orig_key = config.WB2API_KEY
        config.UPSTREAM_CONFIG = self.cfg

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig_cfg
        config.WB2API_KEY = self._orig_key
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _write(self, api_key: str, enabled: bool = False) -> None:
        self.cfg.write_text(json.dumps({'api_key': api_key,
                                        'admin': {'enabled': enabled}}),
                            encoding='utf-8')

    def _read_admin(self) -> dict:
        return json.loads(self.cfg.read_text(encoding='utf-8'))['admin']

    def test_admin_is_in_editable_sections(self) -> None:
        self.assertIn('admin', wb2api._EDITABLE_SECTIONS,
                      'admin 段不在白名单里 —— 界面无法开启管理接口')

    def test_admin_section_is_delivered_to_frontend(self) -> None:
        self._write('k123')
        view = wb2api.load_upstream_config()
        self.assertIn('admin', view, 'admin 段没下发给界面，开关显示不出来')
        self.assertIn('enabled', view['admin'])

    def test_enable_allowed_when_api_key_present(self) -> None:
        self._write('k123')
        wb2api.save_upstream_config({'admin': {'enabled': True}})
        self.assertTrue(self._read_admin()['enabled'])

    def test_enable_rejected_when_api_key_empty(self) -> None:
        """api_key 为空时开启必须被拒——否则上游会拒绝启动。"""
        self._write('')
        config.WB2API_KEY = ''
        with self.assertRaises(ValueError) as ctx:
            wb2api.save_upstream_config({'admin': {'enabled': True}})
        self.assertIn('api_key', str(ctx.exception))
        # 配置没被改坏
        self.assertFalse(self._read_admin()['enabled'])

    def test_disable_still_allowed_without_api_key(self) -> None:
        """关掉永远允许：否则用户会被卡在「开了但起不来」的状态里出不去。"""
        self._write('', enabled=True)
        config.WB2API_KEY = ''
        wb2api.save_upstream_config({'admin': {'enabled': False}})
        self.assertFalse(self._read_admin()['enabled'])

    def test_frontend_has_the_toggle(self) -> None:
        """设置页要有这个开关，并且写清两条路的差别。

        文案必须讲清「开与不开分别会怎样」——用户在账号页点停用时看到的提示
        会随这个开关变化，不说清楚会以为是 bug。
        """
        src = (ROOT / 'web/app/(main)/settings/page.tsx').read_text(encoding='utf-8')
        self.assertIn('ADMIN_FIELDS', src)
        self.assertIn("id: 'admin'", src)
        seg = src[src.index('const ADMIN_FIELDS'):]
        seg = seg[:seg.index('];')]
        self.assertIn('签到', seg, '没说明开启后签到照常')
        self.assertIn('重启', seg, '没说明需要重启上游才生效')


if __name__ == '__main__':
    unittest.main()
