"""workbuddy2api 上游交互：账号文件、状态、模型、容器重启。"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path

from .. import config
from . import realm as _realm
from .realm import realm_of, supports_checkin


def _safe_file(filename: str) -> Path:
    """把请求里的文件名解析为 auths 目录下的真实路径，非法即抛错。

    穿越防线（`/`、反斜杠、`..`、NUL）是根本；此外只接受 `workbuddy*.json`
    这一种形态，避免越权读到目录里的其他文件（例如隐藏文件或临时文件）。

    **通配宽度必须与上游一致**（上游 `auth.AuthFileGlob = "workbuddy*.json"`，
    其注释写明这是它自己踩过的坑：曾用窄模式 `workbuddy-*.json`，导致
    `workbuddy_new.json` 被网关加载却被工具跳过、两边口径对不上）。
    我们此前正是窄模式，于是那种账号**在上游池里能被选中、面板却看不到**——
    与「面板读文件、上游读池」那个不一致是同一个问题的反方向。
    这里的宽化不放松安全：前缀 `workbuddy`、后缀 `.json`、禁止路径分隔符
    与 `..` 三条约束都还在。
    """
    if '/' in filename or '\\' in filename or '..' in filename:
        raise ValueError('非法的文件名')
    if '\x00' in filename:
        raise ValueError('非法的文件名')
    # 白名单形态：账号文件是 workbuddy<后缀>.json（后缀可为空，同上游 glob）；
    # 末尾可带 `.disabled` —— 那是本面板的「临时禁用」标记（改名的产物，
    # 不再匹配上游的 `workbuddy*.json` glob，于是上游不会加载它）。
    if not re.fullmatch(r'workbuddy[0-9A-Za-z_-]{0,80}\.json(\.disabled)?', filename):
        raise ValueError('非法的文件名')
    target = config.AUTH_DIR / filename
    # 结尾必须是 .json 或 .json.disabled（上面正则已保证，这里再兜一层）
    if not (target.name.endswith('.json') or target.name.endswith('.json.disabled')):
        raise ValueError('非法的文件名')
    return target


def read_account_file(filename: str) -> dict:
    return json.loads(_safe_file(filename).read_text(encoding='utf-8'))


def _jwt_times(access_token: str) -> tuple[int, int] | None:
    """从 accessToken（JWT）里读出 (iat, exp)。解不出返回 None。

    纯本地 base64 解码，不发网络请求；仅用于展示，绝不参与鉴权判断。
    """
    try:
        parts = (access_token or '').split('.')
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += '=' * (-len(payload) % 4)  # 补齐 base64url padding
        data = json.loads(base64.urlsafe_b64decode(payload))
        iat = int(data.get('iat') or 0)
        exp = int(data.get('exp') or 0)
        if iat > 0 and exp > iat:
            return iat, exp
    except Exception:  # noqa: BLE001
        return None
    return None


def token_ttl_seconds(access_token: str) -> int | None:
    """该令牌签发的总时长（exp - iat），单位秒。

    用途：界面上的「有效期进度条」需要一个「满格 = 多久」的基准。
    auth 文件里只有 expiresAt，没有签发起始时间，光看文件算不出比例；
    而 JWT 载荷里同时有 iat 与 exp，且只是本地解码、不发网络请求。

    为什么不另存一份 expiresIn：JWT 的 exp - iat 就是该令牌自身的真实寿命，
    且随令牌一起走——刷新换发新令牌时它自动更新，也不会被上游写回时丢掉。
    另存字段反而可能与令牌不一致或过期。

    纯展示用途：解不出来就返回 None，调用方回退到保守的默认窗口，
    绝不影响任何鉴权判断。
    """
    times = _jwt_times(access_token)
    return times[1] - times[0] if times else None


def token_issued_at(access_token: str) -> int | None:
    """令牌签发时间（JWT iat，Unix 秒）。解不出返回 None。

    为什么有用：界面显示的「有效期」是**剩余时间**，刷新会把它重新拉满，
    因此单看剩余天数分不清一个账号是「刚被保活续期」还是「从没刷新过、
    一直用着当初扫码签发的长令牌」。后者才是保活没覆盖到、到期会掉线的
    隐患账号。刷新会换发新令牌，故 iat 近似等于「最近一次刷新时间」。
    """
    times = _jwt_times(access_token)
    return times[0] if times else None


def list_auth_accounts() -> list[dict]:
    """读取 auths/ 目录下的本地账号（与 /status 的运行时状态互补）。

    同时收上游**加载不到**的两类文件，否则它们会在面板上「凭空消失」：
      · `workbuddy*.json.disabled` —— 本面板「临时禁用」改名的产物（见
        `set_account_disabled`）。用户禁用的账号必须仍然看得见、并且能再启用，
        否则「禁用」在使用体验上等同于「删除」。
    """
    out: list[dict] = []
    if not config.AUTH_DIR.is_dir():
        return out
    now = time.time()
    # 上游只加载 workbuddy*.json；我们额外收 .disabled，以便展示与恢复
    files = sorted(config.AUTH_DIR.glob('workbuddy*.json'))
    files += sorted(config.AUTH_DIR.glob('workbuddy*.json.disabled'))
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        acct = raw.get('account', {}) or {}
        auth = raw.get('auth', {}) or {}
        exp = int(auth.get('expiresAt', 0) or 0)
        token = str(auth.get('accessToken') or '')

        # 上游 `Parse` 明确拒绝的情形：accessToken 为空时直接返回
        # `parse_error: missing accessToken`，`LoadDir` 随即静默跳过该文件
        # —— 它**不在账号池里，永远选不中**。这里如实标出原因，前端据此
        # 显示「未加载」而不是「在线」（否则会出现面板全绿、调用却报
        # 「没有健康账号」的矛盾）。判据与上游一致：只判去空白后是否为空。
        invalid_reason = ''
        if not str(auth.get('accessToken') or '').strip():
            invalid_reason = '缺少 accessToken'

        # 总时长：优先用 JWT 自身的 iat→exp（最权威）；JWT 解不出时退回用
        # 文件修改时间推算。上游刷新 token 后会原子写回该文件，因此 mtime
        # 近似等于「最近一次写入/刷新」时刻，于是 exp - mtime ≈ 本次有效期。
        # 这比原来那种「解不出就假定 60 天」的猜测更贴近真实：一个 7 天的
        # 令牌若按 60 天算，进度条只会显示 12%，看着像快过期，属于误报。
        ttl = token_ttl_seconds(token)
        issued = token_issued_at(token)
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            mtime = 0
        if ttl is None and exp > mtime > 0:
            ttl = exp - mtime
        if issued is None and 0 < mtime < exp:
            issued = mtime

        out.append(
            {
                'file': path.name,
                'uid': str(acct.get('uid', '')),
                'nickname': acct.get('nickname') or '未命名',
                'enterprise_id': acct.get('enterpriseId', '') or '',
                'expires_at': exp,
                'is_expired': now >= exp,
                'remain_seconds': max(0, int(exp - now)),
                # 该令牌签发的总时长（供进度条按真实比例展示），解不出为 None
                'ttl_seconds': ttl,
                # 令牌签发时间 ≈ 最近一次刷新时间（刷新会换发新令牌），解不出为 None
                'issued_at': issued,
                # 账号所属版本（cn / global）。上游据此路由到不同上游，
                # 管理端据此做视图过滤与端点分派；存量文件无 realm 字段时
                # 按 domain 回退，domain 也为空则判 cn（行为与升级前一致）
                'realm': realm_of({'realm': raw.get('realm') or auth.get('realm'),
                                   'domain': auth.get('domain')}),
                'domain': str(auth.get('domain') or ''),
                # 该版本是否支持签到体系（国际版没有，调用方据此跳过而不是打 4xx）
                'checkin_supported': supports_checkin(
                    realm_of({'realm': raw.get('realm') or auth.get('realm'),
                              'domain': auth.get('domain')})),
                'source': 'file',
                # 已知上游不会加载该文件时的原因（空 = 没发现明显问题）。
                # 目前只覆盖「accessToken 为空」这一条——那是上游 `Parse`
                # 明确拒绝、且我们能在本地确定判据的情形；其余情况（例如文件
                # 能读但我们没解析出 uid）不臆测原因，交给 in_pool 如实反映。
                'invalid_reason': invalid_reason,
                # 本面板的「临时禁用」标记（文件名带 .disabled 后缀）。
                # 与上游的 disabled 是两回事：那个是上游按错误分类自动禁的，
                # 这个是运维手动停用的，解除方式也不同（见 set_account_disabled）。
                'disabled_by_panel': path.name.endswith('.disabled'),
            }
        )
    return out


def merge_pool_status(accounts: list[dict], status: dict) -> list[dict]:
    """把 /status 的运行时状态合并进账号列表（含积分余额）。

    credits：账号当前可花费积分余额，由上游聚合所有套餐的
    CycleCapacityRemain 得出（见 upstream.UserResource）。

    **in_pool 标记**：该账号是否出现在上游的账号池（`/status.accounts`）里。

    为什么要这个标记：我们读的是 auths/ 目录下的**文件**，上游读的才是**池**。
    两者并不总是一致——上游 `LoadDir` 对解析失败的 auth 文件**静默跳过**
    （`Parse` 在 accessToken 为空时直接报错），那个文件因此不在池里、永远选不中。
    而我们此前照样把它列出来，且因为 `/status` 里没有它，cooling / disabled
    等字段全是 None，前端兜底分支就显示成「● 在线」——**面板全绿、调用却报
    「没有健康账号」**，用户完全无从下手（这正是用户报的现象）。

    `invalid_reason` 用于我们已经能确定「上游不会加载它」的情形，把原因写出来，
    而不是让用户自己去猜文件哪里不对。
    """
    pool: dict[str, dict] = {}
    for item in (status or {}).get('accounts') or []:
        if isinstance(item, dict) and item.get('uid'):
            pool[str(item['uid'])] = item

    for a in accounts:
        p = pool.get(a['uid'])
        a['in_pool'] = p is not None
        if not p:
            # 上游未返回该账号：可能刚添加尚未重载，也可能上游根本没加载成功。
            # 保持其余字段为 None（前端据此单独展示，而不是当成「在线」）。
            a.setdefault('credits', None)
            # 本面板**主动禁用**的账号必然不在池里（改名后上游不再加载它）——
            # 这是预期行为，不是故障。把原因写清楚，否则界面会按「上游没加载它」
            # 报成「账号文件可能有问题」，用户看到自己刚禁用的账号被标成疑似损坏，
            # 反而要去查文件（实测会在界面上产生这种误导）。
            if a.get('disabled_by_panel') and not a.get('invalid_reason'):
                a['invalid_reason'] = '已在本面板临时禁用（不会被上游加载）'
            continue
        credits = p.get('credits')
        a['credits'] = int(credits) if isinstance(credits, (int, float)) else None
        a['cooling'] = bool(p.get('cooling'))
        # 冷却剩余秒数：上游状态机给的是权威值（可能是它解析出的「上游重置时刻」，
        # 也可能是无时间文案时的有界退避）。展示出来，用户就知道还要等多久，
        # 而不是只看到一个「冷却中」干等。
        _remain = p.get('cool_remaining_sec')
        a['cool_remaining_sec'] = int(_remain) if isinstance(_remain, (int, float)) and _remain > 0 else None
        # 被限流的模型清单（上游 issue #36 的限额台账）：多模型限流时，
        # 账号级 until 不等于各模型各自的恢复时刻，需分别展示。
        # 只透传列表形态（前端直接 .map()）；异常类型归空列表，避免整页崩掉。
        _rl = p.get('rate_limited_models')
        a['rate_limited_models'] = _rl if isinstance(_rl, list) else []
        a['disabled'] = bool(p.get('disabled'))
        # 运维手动停用（上游 issue #138/#118，本面板 issue #45）。与 `disabled`
        # **并列独立**：那个是上游按错误分类自动禁的（11140 需重新登录），
        # 这个是运维主动摘的，前者可 revive、后者该 enable。上游对叠加态
        # 两个字段分别透出，我们照搬，前端才能把「系统判定坏了」与
        # 「我主动摘的」分开说——合并成一个字段会互相覆盖（上游注释的原话）。
        a['manual_disabled'] = bool(p.get('manual_disabled'))
        a['manual_reason'] = str(p.get('manual_reason') or '')
        # 禁用原因：上游对 11140（request illegal，需重新 OAuth 登录）会**硬禁用**
        # 账号（到期也不自愈），对 14017（试用未激活）只软冷却。展示原因才能
        # 让用户知道该去重新登录，而不是干等冷却。
        a['disabled_reason'] = str(p.get('disabled_reason') or '')
        a['success_count'] = p.get('success_count')
        a['in_flight'] = p.get('in_flight')
        a['breaker_fails'] = p.get('breaker_fails')
        # 连败降权（上游 issue #114）：连续 N 次「不罚号的失败」后把账号临时移出池
        # （默认阈值 5、降权 10 分钟）。**上游把它计入 cooling**（其 entry.healthy()
        # 的三个截止是或门），所以我们这边必须单独透出，否则「冷却中」里混着两类
        # 原因完全不同的情况：限流退避（等一会儿就好）与连败降权（说明这个号在
        # 持续失败）。只显示「冷却中」时用户无从判断该等还是该处理（实测反馈：
        # 「降权统计这里根本不统计」）。
        #
        # degrade_until 是 Go 的 *time.Time + omitempty：未降权时**整个键都不出现**
        # （指针 nil 才真能被省略，非指针 time.Time 会序列化成 0001-01-01 假真值，
        # 见下面 last_success 的注释），故缺省值按 None 处理即可。
        #
        # 用 isinstance 而不是 `or None`：后者只挡假值，`123` 这类非字符串会被原样
        # 送到前端，而前端要 Date.parse 它。类型不符一律归 None（前端据此当「未降权」
        # 处理，最坏是少显示一个徽章，不会把整页弄崩）。
        _du = p.get('degrade_until')
        a['degrade_until'] = _du if isinstance(_du, str) and _du else None
        _cf = p.get('consecutive_fails')
        a['consecutive_fails'] = _cf if isinstance(_cf, int) and not isinstance(_cf, bool) else None
        a['last_success'] = p.get('last_success')
        # 累计错误数与最后一次错误时刻。为什么要透出：上游对**未命中它那几条
        # 规则**的 4xx（例如被 WAF 拦下的 403）只「换号不罚」——不冷却、不熔断、
        # 不禁用（见其 applyErrorPolicy 的 default 分支）。于是这种账号在面板上
        # 一直显示「正常」，却每次请求都失败、持续几小时。用户报的正是这个
        # （issue #14 第二点）。有了这两个数，界面才能把「一直失败但状态正常」
        # 标出来，用户才知道该重新登录或删掉它。
        a['err_total'] = p.get('err_total')
        a['last_err'] = p.get('last_err')
    return accounts


def delete_auth_account(filename: str) -> bool:
    target = _safe_file(filename)
    if target.exists():
        target.unlink()
        return True
    return False


# 「临时禁用」的文件名标记：加在 `.json` 之后，于是**不再匹配上游的
# `workbuddy*.json` glob**，上游重启后就不会加载它——这是不修改上游代码
# 就能真正停用某个账号的唯一办法（见 set_account_disabled 的说明）。
DISABLED_SUFFIX = '.disabled'
# 兼容旧引用（本文件内部原先叫 _DISABLED_SUFFIX）
_DISABLED_SUFFIX = DISABLED_SUFFIX


def read_account_file_any(filename: str) -> dict:
    """读账号文件，`workbuddy-x.json` 与 `workbuddy-x.json.disabled` **两种形态都试**。

    为什么需要：调用方手里的文件名可能与磁盘上的形态不一致——界面在「停用」
    之后仍然持有旧名字（心跳刷新前），或反过来用户刚停用就点了启用。只按
    传进来的名字读会 FileNotFoundError，于是 uid 解析不出来，依赖 uid 的
    路径（上游状态位）就被静默跳过，功能看着「没生效」。

    两种形态都不存在时抛最后一个异常（FileNotFoundError），语义与
    `read_account_file` 一致。
    """
    candidates = [filename]
    if filename.endswith(DISABLED_SUFFIX):
        candidates.append(filename[: -len(DISABLED_SUFFIX)])
    else:
        candidates.append(filename + DISABLED_SUFFIX)
    last: FileNotFoundError | None = None
    for name in candidates:
        try:
            return read_account_file(name)
        except FileNotFoundError as exc:
            last = exc
    assert last is not None          # 至少两个候选，循环必然执行过
    raise last


def set_account_disabled(filename: str, disabled: bool) -> dict:
    """临时禁用 / 启用一个账号（改名实现）。返回 {file, disabled, ...}。

    实现原理
    --------
    上游 `auth.LoadAuthFiles` 用 glob `workbuddy*.json` 收集账号文件，所以把文件
    改名成 `workbuddy-xxx.json.disabled` 之后它就不再被加载——账号随即从池里消失、
    不会被选中。启用就是改回原名。上游**没有**任何禁用/启用的 HTTP 接口
    （它内部有 `Disable`/`ReviveDisabled`，但只被自身的错误处理调用，未对外暴露），
    而且 `state.json` 每 5 秒被上位机覆盖、改它没有意义，所以改名是唯一可行路径。

    生效方式（新旧上游不同）
    ------------------------
    改名后调用方仍会触发一次重载（`reload.request_restart()`）——这里只做文件操作，
    保持本函数纯粹、可测。两种上游的差别：

      · **新上游**（2026-09-18 起）有 auths 目录热加载：每 5 秒轮询目录指纹，
        改名本身就会让它自动重扫，我们不触发也会生效（触发只是为了不等那 5 秒）。
      · **旧上游**只在启动时扫描一次，必须重启容器才生效——重载就是唯一途径。

    所以「总是触发一次重载」对两种形态都正确，不需要探测上游版本（上游没暴露
    版本号，也探测不了）。

    为什么不用「删掉文件再恢复」
    --------------------------
    删除会丢 token（那份文件里存着 accessToken / refreshToken，删了就再也恢复不了，
    只能重新扫码）。改名是可逆的：启用时原样改回，凭证一个字节都不动。

    边界
    ----
    · 重复禁用/启用是**幂等**的（已是目标状态就直接返回），不报错；
    · 只接受 `workbuddy*.json(.disabled)` 形态（走 `_safe_file` 的校验）；
    · 文件不存在时报错，避免「禁用成功」的假象。
    """
    target = _safe_file(filename)
    # 规范化成「原始账号名」与「禁用名」两种形态
    base = target.name[:-len(_DISABLED_SUFFIX)] if target.name.endswith(_DISABLED_SUFFIX) else target.name
    base_path = _safe_file(base)
    disabled_path = config.AUTH_DIR / (base + _DISABLED_SUFFIX)

    if disabled:
        if disabled_path.exists():
            return {'file': disabled_path.name, 'disabled': True, 'changed': False}
        if not base_path.exists():
            raise ValueError(f'账号文件不存在：{base}')
        base_path.rename(disabled_path)
        return {'file': disabled_path.name, 'disabled': True, 'changed': True}

    if base_path.exists():
        return {'file': base_path.name, 'disabled': False, 'changed': False}
    if not disabled_path.exists():
        raise ValueError(f'账号文件不存在：{base}')
    disabled_path.rename(base_path)
    return {'file': base_path.name, 'disabled': False, 'changed': True}


ASYNC_HEADERS = {'Content-Type': 'application/json'}


def _err_text(exc: Exception) -> str:
    """异常文本可能为空（如 AssertionError），补上类型名便于排查。"""
    detail = str(exc).strip()
    return f'{type(exc).__name__}: {detail}' if detail else type(exc).__name__


def _auth_headers() -> dict:
    key = config.upstream_api_key()
    return {'Authorization': f'Bearer {key}'} if key else {}


async def get_status() -> dict:
    # 连接超时短一些：上游未运行时快速失败，避免拖慢管理端页面
    try:
        async with config.http_client(10, connect=3) as client:
            resp = await client.get(f'{config.WB2API_BASE}/status', headers=_auth_headers())
        if resp.status_code >= 400:
            return {'connected': False, 'error': f'上游返回 {resp.status_code}'}
        data = resp.json()
        data['connected'] = True
        return data
    except Exception as exc:  # noqa: BLE001
        return {'connected': False, 'error': _err_text(exc)}


def _admin_route_missing(resp: object) -> bool:
    """判断这次 404 是「接口没注册」还是「账号不在池里」。

    两者都是 404，但含义完全相反，处理方式也相反——前者要回退到改名，
    后者是真错误（改名同样救不了）。上游对这两种 404 的响应体不同：

      · **接口没注册**（`admin.enabled=false` 或旧版本未实现）：net/http 的
        默认路由兜底，响应体是纯文本 `404 page not found`；
      · **账号不存在**：handler 内 `writeOpenAIError(..., "not_found", ...)`,
        响应体是 JSON `{"error":{"code":"not_found",...}}`。

    所以按「响应体能否解析成带 error.code 的 JSON」来区分。判不出来时
    保守按「接口没注册」处理（回退改名仍然实现了用户意图：不被选中）。
    """
    text = ''
    try:
        text = resp.text  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return True
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return True
    return not (isinstance(data, dict) and isinstance(data.get('error'), dict))


async def set_manual_disabled(uid: str, disabled: bool, reason: str = '') -> tuple[bool, str, str]:
    """用上游的 `manual_disabled` 状态位停用/启用账号。

    返回 `(是否成功, 说明文案, 结果码)`。结果码用于调用方决定是否回退：

      · `ok`        —— 状态位已生效；
      · `no_route`  —— 上游没注册这组接口（旧版本、或 `admin.enabled=false`）；
      · `not_found` —— 接口在，但该 uid 不在池里（文件没被加载等）；
      · `error`     —— 其它失败（网络、5xx、鉴权）。

    ## 为什么优先用它，而不是改文件名（issue #45）

    上游 2026-09-19 暴露了 `POST /admin/accounts/{uid}/disable|enable`，
    语义是**「对话流量摘除」而非「账号冻结」**（其 admin.go 原话）：

      · 停用期间**不参与选号**（`pick.go` 把 `e.disabled || e.manualDisabled`
        一并排除，对话流量不落到它身上）；
      · 但**签到 / token 保活 / 猫猫旅行 / 活跃上报照常执行**；
      · 凭证与积分保持活跃，重启也保留这个意图（持久化在 state.json）。

    而「改文件名」会让账号**完全退出账号池**：既不被选中，也不再执行任何排程任务。
    对「这个号在拖后腿，先停一会儿」这种用法，后者副作用过大——积分不再增长、
    token 不再续期，回来时可能已经过期。所以两条路并存，优先走状态位。

    ## 关于 `admin.enabled` 默认关闭

    上游这组接口**默认不注册**（`admin.enabled` 默认 false，关闭时一律 404，
    其注释写明是「不向外暴露管理面」的有意设计）。所以能拿到 `no_route` 是
    **常见且正常**的，不是故障——调用方据此回退改名即可，并把开启方式告诉用户。

    ## 鉴权

    与 `/status` 同源（`api_key`）。`_auth_headers()` 已带上；上游未配 api_key 时
    它自己的启动校验会拒绝 `admin.enabled=true`，所以这里不需要额外分支。
    """
    if not uid:
        return False, 'uid 为空', 'error'
    action = 'disable' if disabled else 'enable'
    url = f'{config.WB2API_BASE}/admin/accounts/{uid}/{action}'
    body: dict = {'reason': reason} if (disabled and reason) else {}
    try:
        async with config.http_client(10, connect=3) as client:
            resp = await client.post(url, json=body, headers=_auth_headers())
    except Exception as exc:  # noqa: BLE001
        return False, _err_text(exc), 'error'
    if resp.status_code == 404:
        if _admin_route_missing(resp):
            return False, '上游未启用管理接口（admin.enabled=false 或版本较旧）', 'no_route'
        return False, '该账号不在上游账号池里', 'not_found'
    if resp.status_code == 401:
        return False, '上游拒绝了鉴权（api_key 不一致）', 'error'
    if resp.status_code >= 400:
        return False, f'上游返回 {resp.status_code}', 'error'
    return True, ('已通过上游状态位停用（签到与保活照常执行）' if disabled
                  else '已通过上游状态位启用'), 'ok'


def upstream_state_file() -> Path:
    """读取上游 config.json 并解析 state_file 的真实路径。

    上游允许 `state_file` 使用相对路径，且相对的是上游进程的工作目录。
    管理端与上游共享同一个上游仓库目录，因此这里按 `WB_UPSTREAM_DIR`
    解析；配置缺失或非法时拒绝操作，避免猜错路径后改到别的文件。
    """
    try:
        cfg = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise ValueError(f'未找到上游配置文件：{config.UPSTREAM_CONFIG}') from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f'读取上游配置失败：{exc}') from exc
    if not isinstance(cfg, dict):
        raise ValueError('上游配置不是 JSON 对象')
    raw = str(cfg.get('state_file') or 'data/state.json').strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config.UPSTREAM_DIR / path
    return path.resolve()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子写回状态文件，并保留原文件的权限与属主。"""
    st = path.stat()
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, st.st_mode & 0o777)
        try:
            # Windows 上**没有** os.chown（该 API 仅 Unix 提供）——直接调用会抛
            # AttributeError。原生模式部署在 Windows 上也会走到这里（评审实测：
            # 在 Windows 跑本 PR 自带的测试，四条里三条就是这个错）。
            # 权限模式已经 chmod 保留；属主在 Windows 上本来也没有 uid/gid 语义。
            chown = getattr(os, 'chown', None)
            if chown is not None:
                chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            # 非 root 部署若本来就是文件属主，chown 到同一个 uid/gid 也可能在
            # 某些平台上被拒绝；权限模式已经保留，失败不应阻断写入。
            pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def clear_account_cooling_state(uid: str) -> dict:
    """清除一个账号在 state.json 中的冷却、熔断、降权与模型级限流。

    只修改 target uid 对应条目，`disabled` / `manual_disabled` / 凭证 / 积分
    等正交状态保持不变。调用方必须先停止上游，否则周期 Flush 会覆盖本改动。
    """
    uid = str(uid or '').strip()
    if not uid:
        raise ValueError('uid 为空')
    path = upstream_state_file()
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise ValueError(f'未找到上游状态文件：{path}') from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f'上游状态文件不是合法 JSON（{path}）：{exc}') from exc
    accounts = state.get('accounts') if isinstance(state, dict) else None
    if not isinstance(accounts, dict) or not isinstance(accounts.get(uid), dict):
        raise ValueError('该账号不在上游状态文件中')

    # 保留一份最近一次强制清除前的快照，便于人工回退。state.json 不含凭证。
    backup = path.with_name(path.name + '.force-clear.bak')
    shutil.copy2(path, backup)

    account = accounts[uid]
    before = {
        'until': account.get('until'),
        'reason': account.get('reason'),
        'model_cooldowns': account.get('model_cooldowns'),
        'breaker_until': account.get('breaker_until'),
        'degrade_until': account.get('degrade_until'),
    }
    account['until'] = '0001-01-01T00:00:00Z'
    # `cool_kind` 要**删掉**而不是写 0（评审修正）：它在 state.json 里是 int 枚举
    # （0 = hard_credit），而「没有冷却」的规范表示是**不写这个键** —— 上游自己的
    # 落盘逻辑就这么做，其注释写明是为了避免「until 零值 + cool_kind」的不一致快照。
    # 写 0 不会立刻出问题（until 是零值，上游下次 Flush 也会按规范抹掉），但没必要
    # 在别人的文件里留一个自己造的非规范形态。
    account.pop('cool_kind', None)
    account['soft_streak'] = 0
    account.pop('model_cooldowns', None)
    account.pop('breaker_until', None)
    account['retry_count'] = 0
    account.pop('degrade_until', None)
    account['consecutive_fails'] = 0
    # disabled=true 时 reason 是禁用原因，不属于冷却域，不能误清。
    if not bool(account.get('disabled')):
        account.pop('reason', None)

    _atomic_write_json(path, state)
    return {'uid': uid, 'state_file': str(path), 'backup': str(backup), 'before': before}


async def _set_upstream_running(running: bool) -> tuple[bool, str]:
    """停止或启动上游，供需要离线修改 state.json 的运维动作使用。"""
    if config.WB2API_MODE == 'native':
        script = config.WB2API_START_SCRIPT if running else config.WB2API_STOP_SCRIPT
        if not script.is_file():
            return False, f'未找到原生上游脚本：{script}'
        cmd = (('cmd.exe', '/d', '/c', str(script)) if os.name == 'nt' else (str(script),))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(script.parent),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=_NATIVE_RESTART_TIMEOUT)
            if proc.returncode != 0:
                return False, f'{script.name} 退出码 {proc.returncode}'
            return True, f'{script.name} 执行成功'
        except asyncio.TimeoutError:
            _kill_quietly(proc)
            return False, f'{script.name} 执行超时'
        except Exception as exc:  # noqa: BLE001
            return False, f'执行 {script.name} 失败：{exc}'

    action = 'start' if running else 'stop'
    cmd = ['docker', action]
    if not running:
        cmd.extend(['--time', '15'])
    cmd.append(config.WB2API_CONTAINER)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode == 0:
            return True, f'容器已{"启动" if running else "停止"}'
        detail = err.decode(errors='ignore').strip()
        return False, detail or f'docker {action} 退出码 {proc.returncode}'
    except FileNotFoundError:
        return False, '未找到 docker 命令'
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def _wait_for_upstream(timeout: float = 35.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {'connected': False, 'error': '等待上游重启超时'}
    while time.monotonic() < deadline:
        last = await get_status()
        if last.get('connected'):
            return last
        await asyncio.sleep(1)
    return last


async def force_clear_account_cooling(uid: str) -> tuple[bool, str, dict]:
    """强制清除账号冷却与模型级限流，并重启上游使离线修改生效。"""
    uid = str(uid or '').strip()
    if not uid:
        return False, 'uid 为空', {}

    initial = await get_status()
    if initial.get('connected'):
        account = next(
            (x for x in initial.get('accounts') or [] if str(x.get('uid') or '') == uid),
            None,
        )
        if account is None:
            return False, '该账号不在上游账号池里', {}
        if not account.get('cooling') and not account.get('rate_limited_models'):
            return True, '该账号当前没有冷却或模型级限流状态，无需清除', {}

    stopped, stop_message = await _set_upstream_running(False)
    if not stopped:
        return False, f'停止上游失败：{stop_message}', {}

    try:
        changed = clear_account_cooling_state(uid)
    except Exception as exc:  # noqa: BLE001
        # 修改失败也必须尽力把上游拉回来，不能让它停在 maintenance 状态。
        await _set_upstream_running(True)
        return False, f'清除状态失败：{exc}', {}

    started, start_message = await _set_upstream_running(True)
    if not started:
        return False, f'状态已清除，但启动上游失败：{start_message}', changed

    status = await _wait_for_upstream()
    if not status.get('connected'):
        return False, f'上游重启后未就绪：{status.get("error") or "未知错误"}', changed
    account = next(
        (x for x in status.get('accounts') or [] if str(x.get('uid') or '') == uid),
        None,
    )
    if account is None:
        return False, '状态已清除且上游已启动，但账号尚未进入账号池', changed
    if account.get('cooling') or account.get('rate_limited_models'):
        return False, '上游已重启，但冷却或模型限流状态仍然存在', changed

    return True, '已强制清除冷却与模型限流状态，上游已重启生效', changed


def admin_enabled_in_config() -> tuple[bool | None, str]:
    """本面板读到的上游配置里 `admin.enabled` 是否为真。返回 `(值, 说明)`。

    值 `None` = 读不到，`说明` 里带原因与路径；否则 `说明` 是配置文件的路径。

    为什么需要它（issue #45 的追问）：状态位路径拿到 `no_route` 时有两种成因，
    处理方式完全相反，而用户从界面上看不出是哪一种：

      · 配置里**没开** → 去「设置 → 账号管理接口」打开；
      · 配置里**已开** → 说明**运行中的上游没加载到它**。上游只在启动时读这个
        开关，改完配置必须重启容器；若已重启仍如此，说明上游镜像早于
        2026-09-19（那版还没有这组接口）。实测有用户手改了配置文件里的一处，
        面板读到的却是另一处，于是「明明开了却还是不行」——所以这条文案里
        必须带上**面板实际读的那个路径**，用户一对就知道是不是同一个文件。
    """
    path = config.UPSTREAM_CONFIG
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None, f'未找到上游配置文件 {path}'
    except OSError as exc:
        return None, f'读取上游配置文件失败 {path}：{exc}'
    try:
        cfg = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return None, f'上游配置文件不是合法 JSON（{path}）：{exc}'
    if not isinstance(cfg, dict):
        return None, f'上游配置文件不是 JSON 对象（{path}）'
    admin = cfg.get('admin')
    return bool(isinstance(admin, dict) and admin.get('enabled')), str(path)


# 上游统计里**允许下发**的字段（白名单）。为什么不整包透传：与
# `load_upstream_config` 那条同源的理由（见那里的注释）——透传的失效模式是
# 「上游给统计载荷加了新字段 → 原样下发给每个登录用户（含只读账号）」，
# 而且**不会有任何报错**；白名单的失效模式相反：新字段不显示（界面少一列），
# 这个方向的失效是可见、可控的。下面就是界面要显示的计数字段，多一个都不带。
_STATS_ROW_FIELDS = ('requests', 'success', 'failed', 'streaming',
                     'prompt_tokens', 'completion_tokens', 'total_tokens',
                     'cache_hit_tokens', 'cache_miss_tokens', 'cache_write_tokens',
                     'cache_hit_rate', 'credit', 'credit_per_req')
_STATS_MODEL_FIELDS = ('model',) + _STATS_ROW_FIELDS


def _pick_stats_row(row: object, fields: tuple[str, ...]) -> dict:
    """从上游的统计行里只挑白名单字段。"""
    if not isinstance(row, dict):
        return {}
    return {k: row[k] for k in fields if k in row}


async def get_upstream_stats() -> dict:
    """读上游自己的 `/v1/stats`（它按模型累计的官方统计，issue #59）。

    **与面板自己的统计不是一回事，别混着看**：

      · 面板的用量统计（`/api/stats/*`）统计的是**经过本网关**的调用，
        按密钥归属、可按时段筛选；
      · 上游这份是「上游进程自己看到的全部调用」——**直连 7863 的调用只在这里**，
        而且它是**自上游进程启动以来**的累计，没有时段概念。

    用户要的「原有密钥的用量」只能在后者里看到（那把密钥直连上游，面板看不见它），
    所以如实说明口径比数字本身更重要。

    返回 `{'available': False, 'error': ...}` 表示取不到（上游没起来、版本太旧没有
    这个端点、或 api_key 不一致）——界面据此说明情况，而不是显示一片空白。
    """
    try:
        async with config.http_client(10, connect=3) as client:
            resp = await client.get(f'{config.WB2API_BASE}/v1/stats',
                                    headers=_auth_headers())
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'error': _err_text(exc)}
    if resp.status_code == 401:
        return {'available': False, 'error': '上游拒绝了鉴权（api_key 不一致）'}
    if resp.status_code == 404:
        return {'available': False,
                'error': '该上游版本没有这个端点（需要较新的上游镜像）'}
    if resp.status_code >= 400:
        return {'available': False, 'error': f'上游返回 {resp.status_code}'}
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'error': f'上游返回的不是 JSON：{_err_text(exc)}'}
    if not isinstance(data, dict):
        return {'available': False, 'error': '上游返回的结构无法识别'}
    # 只挑白名单字段下发（见 _STATS_ROW_FIELDS 的说明）
    out: dict = {
        'available': True,
        'enabled': data.get('enabled'),
        'since': data.get('since'),
        'uptime_sec': data.get('uptime_sec'),
    }
    if isinstance(data.get('message'), str):
        out['message'] = data['message']
    if isinstance(data.get('total'), dict):
        out['total'] = _pick_stats_row(data['total'], _STATS_ROW_FIELDS)
    models = data.get('models')
    if isinstance(models, list):
        out['models'] = [_pick_stats_row(m, _STATS_MODEL_FIELDS)
                         for m in models if isinstance(m, dict)]
    return out


async def get_models() -> tuple[bool, list | dict]:
    try:
        async with config.http_client(15, connect=3) as client:
            resp = await client.get(f'{config.WB2API_BASE}/v1/models', headers=_auth_headers())
        if resp.status_code >= 400:
            return False, {'error': f'上游返回 {resp.status_code}'}
        body = resp.json()
        return True, body.get('data', body)
    except Exception as exc:  # noqa: BLE001
        return False, {'error': _err_text(exc)}


def models_source(items: list) -> str:
    """判断这份模型列表来自上游「动态拉取」还是「静态回退」。

    注意（上游 2026-09-15 起，commit 1b7ce4a）：上游已**删除** CN/global 的静态
    兜底表，改为纯动态——动态拉取失败或池中无对应账号时返回**空列表**，不再回退
    到编译进二进制的固定名单。因此下面的 `static` 只可能来自仍在跑老版本上游的
    部署（那种情况下如实标「非实时」依然有用）。

    判据（取上游内部实现细节）：
      * 动态条目经 `applyModelInfoFields` 写出 `name`（上游模型对象带 Name 时必写）
        与 `max_output_tokens`（四级查找命中时写）；
      * 老版本上游的静态表**只覆盖 CN**，条目为裸名或 `cn:` 前缀；
      * 故「没有动态特征键 + 没有 global 条目」才判 static——国际版的探测结果在窄表
        形态下同样不带这些键，若不加前缀约束会被误报成静态回退。

    为什么同时看 `name` 和 `max_output_tokens`：后者是**四级查找全不命中就省略**
    （上游 handler.go 的 `MaxOutputTokensListingV4`，兜底是「省略字段」而非填默认值），
    理论上存在整张表都没这个键的情况；`name` 的写出条件宽得多（模型对象自带 Name 即可）。
    两个信号任一命中即判动态，比只认一个稳。

    判不出来返回 'unknown'，前端据此回退到中性文案，绝不因此报错。
    """
    if not isinstance(items, list) or not items:
        return 'unknown'
    dicts = [x for x in items if isinstance(x, dict)]
    if not dicts:
        return 'unknown'
    if any('max_output_tokens' in x or 'name' in x for x in dicts):
        return 'dynamic'
    if not all('id' in x for x in dicts):
        return 'unknown'
    # 老上游静态表不含 international 条目；有 global: 说明这次拉取是真实探测结果
    if any(str(x.get('id') or '').startswith('global:') for x in dicts):
        return 'dynamic'
    return 'static'


# 原生启停脚本的执行上限（秒）。
#
# 为什么要设：脚本可能挂住（等交互输入、端口被占用、启动时卡在依赖上）。没有超时
# 的话这个协程永不返回，而 reload 的状态机会一直停在 running=True —— 后续所有
# 「保存配置后自动重载」都**静默失效**（不报错、不重试），调用方也一直挂着。
# 60 秒对启停一个本地进程足够宽裕（正常是秒级）。
_NATIVE_RESTART_TIMEOUT = 60


def _kill_quietly(proc) -> None:
    """尽力结束子进程；失败也不抛（调用方已经在处理错误路径了）。"""
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


async def restart_container() -> tuple[bool, str]:
    """重启上游；native 模式走启停脚本，其余部署保持 Docker 行为。"""
    if config.WB2API_MODE == 'native':
        scripts = (config.WB2API_STOP_SCRIPT, config.WB2API_START_SCRIPT)
        missing = [str(path) for path in scripts if not path.is_file()]
        if missing:
            return False, f'未找到原生启停脚本：{"、".join(missing)}'
        for script in scripts:
            cmd = (
                ('cmd.exe', '/d', '/c', str(script))
                if os.name == 'nt'
                else (str(script),)
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(script.parent),
                    # 后台服务可能继承 PIPE，导致 communicate() 永远等不到 EOF。
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                # **必须有超时**：脚本可能挂住（等交互输入、被占用的端口、
                # 启动时卡在依赖上）。没有超时的话：
                #   · 这个协程永不返回 → reload 的状态机一直停在 running=True，
                #     后续所有「保存配置后自动重载」都会静默失效（不报错、不重试）；
                #   · 调用方（保存设置接口）也一直挂着。
                # 超时后杀掉进程并如实回报，用户至少知道「重启没成功」。
                await asyncio.wait_for(proc.communicate(), timeout=_NATIVE_RESTART_TIMEOUT)
            except asyncio.TimeoutError:
                _kill_quietly(proc)
                return False, (
                    f'{script.name} 执行超过 {_NATIVE_RESTART_TIMEOUT} 秒未结束，已终止。'
                    f'请手动确认上游状态，或把 WB2API_START_SCRIPT / WB2API_STOP_SCRIPT '
                    f'指向不会挂住的脚本'
                )
            except Exception as exc:  # noqa: BLE001
                return False, f'执行 {script.name} 失败：{exc}'
            if proc.returncode != 0:
                return False, f'{script.name} 退出码 {proc.returncode}'
        return True, '原生 workbuddy2api 已重启'

    name = config.WB2API_CONTAINER
    try:
        proc = await asyncio.create_subprocess_exec(
            'docker', 'restart', name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode == 0:
            return True, f'容器 {name} 已重启'
        return False, (err.decode(errors='ignore').strip() or f'docker 退出码 {proc.returncode}')
    except FileNotFoundError:
        return False, '未找到 docker 命令'
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def read_container_logs(limit: int = 200, timestamps: bool = True) -> list[str]:
    """读取上游日志（原生日志文件或 Docker，失败返回空列表）。

    默认带 `--timestamps`：docker 会在每行前面加上精确到纳秒的 RFC3339 时间，
    自动任务日志据此获得准确时间并据此去重（上游自己的 log 前缀精度只到秒）。
    """
    if config.WB2API_MODE == 'native':
        try:
            count = max(1, min(5000, limit))
            with config.WB2API_LOG_FILE.open('r', encoding='utf-8', errors='replace') as fh:
                return [ln.rstrip('\r\n') for ln in deque(fh, maxlen=count) if ln.strip()]
        except Exception:  # noqa: BLE001
            return []

    import subprocess

    cmd = ['docker', 'logs', '--tail', str(max(1, min(5000, limit)))]
    if timestamps:
        cmd.append('--timestamps')
    cmd.append(config.WB2API_CONTAINER)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        # docker logs 把应用日志写到 stderr
        raw = (proc.stdout or '') + (proc.stderr or '')
        return [ln for ln in raw.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


# 管理端**允许读写**的上游配置段。既是 `save_upstream_config` 的写入白名单，
# 也是 `load_upstream_config` 的**下发白名单**——两处必须是同一份，否则会出现
# 「能保存但读不回来」或「读得到却存不回去」的不一致。顶层其余键（api_key、
# auth_dir、state_file 等）一律不下发：它们是凭据或部署路径，界面不使用。
#
# `admin` 段：上游的运维管理端点开关（`admin.enabled`，默认 false），
# 面板的「临时停用」优先走它——它只摘对话流量，签到与保活照常。
# 必须可由界面开启：否则用户只能手改上游 config.json，而这条路的收益
# （保留签到与保活）正需要一个「顺手就能开」的入口，否则没人会去开。
_EDITABLE_SECTIONS = ('schedule', 'pool', 'cooldown', 'features',
                      'session_sticky', 'prompt', 'server', 'upstream', 'global',
                      'admin')


def _mask(v: str) -> str:
    if not v:
        return ''
    return v[:6] + '*' * max(0, len(v) - 10) + v[-4:] if len(v) > 12 else '******'


def load_upstream_config() -> dict:
    """读取 workbuddy2api 的 config.json，敏感字段一律掩码。

    读不到时返回 available=False 并附带原因，供前端明确提示并禁止保存，
    避免把空配置写回真实文件。

    注意：不返回原始配置对象。原始配置含上游 API Key、Upstash token 与
    设备风控 token 的明文，前端并不需要它们，不应通过接口下发。
    """
    path = config.UPSTREAM_CONFIG
    cfg: dict | None = None
    error: str | None = None

    if not path.is_file():
        error = f'未找到上游配置文件 {path}'
    else:
        try:
            loaded = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                cfg = loaded
            else:
                error = f'上游配置文件不是合法的 JSON 对象: {path}'
        except Exception as exc:  # noqa: BLE001
            error = f'上游配置文件解析失败: {exc}'

    if cfg is None:
        return {
            'available': False,
            'config_path': str(path),
            'auth_dir': str(config.AUTH_DIR),
            'error': error or '无法读取上游配置',
        }

    # **白名单**式往外发，而不是 `dict(cfg)` 之后逐个 pop 敏感键。
    #
    # 为什么必须反过来写：denylist 的失效模式是「上游加了一个新的密钥字段 →
    # 原样下发给任何登录用户（含只读的 viewer）」，而且**不会有任何报错**。
    # 白名单的失效模式则相反：新字段不显示，用户去上游改 —— 安全得多。
    # （实测确认过 denylist 的后果：往配置里塞一个未知的 *_secret 键，
    # 它会出现在接口响应里。）
    view: dict = {
        # 界面确实要用的非敏感顶层项
        k: cfg[k] for k in ('listen',) if k in cfg
    }
    # 只放界面能编辑的那些配置段（与 save_upstream_config 的允许集合一致）
    for section in _EDITABLE_SECTIONS:
        if section in cfg and isinstance(cfg[section], dict):
            view[section] = cfg[section]

    if 'api_key' in cfg:
        view['api_key_masked'] = _mask(str(cfg.get('api_key') or ''))
    # 账号列表实际读取的是管理端自己的 AUTH_DIR，以此为准；上游若声明了不同目录则一并暴露
    upstream_auth_dir = cfg.get('auth_dir')
    view['auth_dir'] = str(config.AUTH_DIR)
    if upstream_auth_dir and str(upstream_auth_dir) != str(config.AUTH_DIR):
        view['upstream_auth_dir'] = str(upstream_auth_dir)

    # Upstash：token 属敏感信息，只回传「是否已配置」，不回传内容
    up = cfg.get('upstash')
    up = up if isinstance(up, dict) else {}
    token = str(up.get('token') or '')
    view['upstash'] = {
        'url': str(up.get('url') or ''),
        'has_token': bool(token),
        'token_masked': _mask(token) if token else '',
    }

    # 出站设备风控 token（upstream.device_token）同样是凭据：
    # 它相当于把一台可信设备的身份借出去，泄露可被他人复用。
    # 与 Upstash token 一样只回传「是否已配置」+ 掩码。
    upst = cfg.get('upstream')
    upst = upst if isinstance(upst, dict) else {}
    dev = str(upst.get('device_token') or '')
    if 'upstream' in view and isinstance(view['upstream'], dict):
        view['upstream'] = dict(view['upstream'])
        view['upstream'].pop('device_token', None)
    view['upstream'] = {
        **(view.get('upstream') if isinstance(view.get('upstream'), dict) else {}),
        'has_device_token': bool(dev),
        'device_token_masked': _mask(dev) if dev else '',
    }

    view['available'] = True
    view['config_path'] = str(path)
    return view


# 上游 config.json 的可视化字段类型约束：
#   *_hours 是 []int（整点数组），cooldown.* 是时长字符串（30s/10m/2h/1d）
def _has_control_chars(v: str) -> bool:
    """是否含换行或控制字符（路径 / UA 这类单行文本不允许）。"""
    return any(ord(ch) < 32 for ch in v)


# 整点数组字段。上游 2026-09-14 起把 school（开学季）与 cat（夜猫）从宿主机
# crontab 迁入内置调度器，任务类型由 4 类变 6 类——这里必须同步，
# 否则设置页保存这两项会被当成未知键丢弃（白名单外的字段静默忽略）。
_HOURS_KEYS = ('checkin_hours', 'travel_hours', 'activity_hours', 'keepalive_hours',
               'school_hours', 'cat_hours')

# upstream 段里的单行文本字段（会做控制字符与长度校验）
_UPSTREAM_TEXT_KEYS = (
    'user_agent',          # 出站 UA 显式覆盖
    'client_version',      # WorkBuddy 客户端版本段
    'cli_version',         # CLI 版本段
    'client_name',         # 用量归属头 X-Product/X-IDE-Name/X-IDE-Type
    'device_token_file',   # 设备 token 文件路径
)
_UPSTREAM_TEXT_MAX = 512

# 整数/小数字段的取值范围：键 -> (最小, 最大, 单位)
# 上限不是洁癖——这些值直接决定上游的行为强度与成本（例如
# activity_report_count 决定每号每天发多少条对话）。前端的 max 只是
# 输入框属性，拦不住直接调接口，必须服务端兜底。
_INT_RANGES: dict[str, tuple[int, int, str]] = {
    'activity_report_count': (1, 50, '条'),
    'max_in_flight': (0, 64, '个'),
    # 国际版在途上限分档（上游 2680f4c）。**语义与 max_in_flight 不同**：那边
    # 0 = 不限制，这边的 0（含负数）在上游 config 归一化时被改成默认值 **2**
    # ——既不是「不限」，也不是「跟随 max_in_flight」（上游池子层的注释这么写，
    # 但归一化在它之前就把 0 换成了 2，实际生效的是 2）。前端文案按这个口径写。
    # 单独登记是为了享受同样的区间校验 —— 不登记的话它会被归到「未知键」
    # 原样透传，用户填个负数或超大值也能写进上游配置。
    'max_in_flight_global': (0, 64, '个'),
    'breaker_threshold': (1, 100, '次'),
    # 连败降权阈值（上游 cf1e7e5 新增 pool.degrade_threshold，默认 5）：ErrClient 与
    # 传输层失败连续达此次数即临时出池。登记以享受同样的区间校验。
    # 另外两个同批新增的键（degrade_cooldown / degrade_cooldown_max）是时长字符串，
    # 已被下面「按 _cooldown 后缀走时长格式校验」那条规则覆盖，无需单独登记。
    'degrade_threshold': (1, 100, '次'),
    'idle_weight_max': (0, 1000, ''),
    'max_body_mb': (1, 256, 'MB'),
}

_FLOAT_RANGES: dict[str, tuple[float, float, str]] = {
    'idle_weight_per_hour': (0.0, 100.0, ''),
}


def _check_int(key: str, raw: object) -> int:
    lo, hi, unit = _INT_RANGES[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f'{key} 必须是整数')
    if not lo <= raw <= hi:
        raise ValueError(f'{key} 必须在 {lo}-{hi}{unit} 之间（收到 {raw}）')
    return raw


def _check_float(key: str, raw: object) -> float:
    lo, hi, unit = _FLOAT_RANGES[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f'{key} 必须是数字')
    val = float(raw)
    if not lo <= val <= hi:
        raise ValueError(f'{key} 必须在 {lo}-{hi}{unit} 之间（收到 {raw}）')
    return val
_DURATION_RE = re.compile(r'^\d+\s*(s|m|h|d)$', re.IGNORECASE)


def _sanitize_section(section: str, incoming: dict) -> dict:
    """校验并归一化要写入的字段，挡住会把配置写坏的非法值。

    前端已经做了校验，这里再做一层兜底：错的数据宁可拒绝（抛错），
    也不要写进上游配置触发容器启动失败。
    """
    out = dict(incoming)
    for key, raw in incoming.items():
        if key in _HOURS_KEYS:
            if not isinstance(raw, list) or not all(
                isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 23 for x in raw
            ):
                raise ValueError(f'{key} 必须是 0-23 的整点数组，例如 [9, 21]')
            if not raw:
                raise ValueError(f'{key} 至少要有一个时刻')
            out[key] = sorted({int(x) for x in raw})
        elif isinstance(raw, str) and (
            key.endswith(('_rate', '_rate_max', '_cooldown', '_cooldown_max'))
            or key in ('ttl', 'gc_interval')
        ):
            if not _DURATION_RE.match(raw.strip()):
                raise ValueError(f'{key} 时长格式有误，应为 30s / 10m / 2h / 1d')
            out[key] = raw.strip()
        elif section == 'prompt' and key == 'mode':
            # 上游对非法值是**启动报错**（cmd/server/config.go:429
            # 「prompt.mode: %q 不是合法值」），所以这里必须拦——填错就保存成功、
            # 然后上游起不来，正是本节注释里点名的最坏形态。
            # 可达路径：`POST /api/settings/upstream` 直接透传 body，不经前端表单。
            val = str(raw or '').strip().lower()
            if val not in ('passthrough', 'custom', 'append'):
                raise ValueError('系统提示词模式只能是 passthrough / custom / append')
            out[key] = val
        elif section == 'prompt' and key == 'file':
            # 这是**文件路径**，不是提示词正文（issue #62）。上游对它是 fail-fast：
            # 路径非空但读不到 → 启动直接报错退出（其 normalizePrompt 注释写明
            # 「避免静默回落到内置默认」）。把正文粘进来会让上游进入 Restarting
            # 崩溃循环，整个反代不可用——实测就有用户这么踩了。
            #
            # 两条判据都来自「文件名不是正文」这个事实：
            #   · 含换行/控制字符 —— 路径不可能有；
            #   · 超过 255 字节 —— 文件名的硬上限（用户看到的报错就是 file name too long）。
            # 长度按**字节**算：中文一个字三字节，几十个字的提示词就超了。
            val = str(raw or '')
            if any(ch in val for ch in ('\n', '\r', '\x00')):
                raise ValueError(
                    '这一栏要填文件路径，不是提示词正文。正文请先写进一个文件，'
                    '再填该文件在上游容器内的路径，例如 /app/data/prompt-custom.txt'
                )
            if len(val.encode('utf-8')) > 255:
                raise ValueError(
                    '文件路径不能超过 255 字节（文件名上限）——看起来是把提示词正文'
                    '粘进来了。正文请先写进一个文件，再填它的路径'
                )
            out[key] = val.strip()
        elif section == 'pool' and key == 'cost_explore_interval':
            # 成本档位条件探索的周期（上游 2026-09-17 新增，默认 "30m"）。
            #
            # 必须在这里拦：上游对它是 `time.ParseDuration` 失败即**启动报错**
            # （cmd/server/config.go:383 的 fail fast）。而它既不匹配上面那条按
            # `_cooldown`/`_rate` 后缀的规则，也不在下面两张区间表里 —— 不补这条
            # 就等于「填错也保存成功，然后上游起不来」，正是本节注释里点名的
            # 最坏形态。可达路径是 `POST /api/settings/upstream` 直接透传 body，
            # 不经过前端表单。
            #
            # **"0" 是合法值**（关停该特性，与上游 `CostExploreIntervalDur = 0` 一致），
            # 所以不能要求必须匹配时长格式。
            val = str(raw or '').strip()
            if val and val != '0' and not _DURATION_RE.match(val):
                raise ValueError('cost_explore_interval 时长格式有误，应为 30m / 1h；0 = 关停')
            out[key] = val
        elif section == 'pool' and key == 'expiring_soon':
            # 快过期积分窗口（上游 2026-09-14 新增）：选号时优先消耗窗口内到期的
            # 积分。语义与普通时长不同——**空串或 "0" 表示禁用分桶**，不是非法值，
            # 所以不能套上面那条「必须匹配时长格式」的规则（否则用户没法关掉）。
            val = str(raw or '').strip()
            if val and val != '0' and not _DURATION_RE.match(val):
                raise ValueError('expiring_soon 时长格式有误，应为 168h / 7d；留空或 0 = 禁用')
            out[key] = val
        elif key in _INT_RANGES:
            # 统一区间校验（activity_report_count 等；见 _INT_RANGES 注释）
            out[key] = _check_int(key, raw)
        elif key in _FLOAT_RANGES:
            out[key] = _check_float(key, raw)
        elif section == 'prompt' and key == 'mode':
            mode = str(raw or '').strip().lower()
            # 取值必须与上游 `normalizePrompt` 的白名单**保持一致**：上游对非法值
            # 是**启动即报错**（fail fast），所以这里拦不住的话，用户会存进一份让
            # 上游起不来的配置——表现为「保存成功，然后上游挂了」，比当场报错难查得多。
            # `append` 是上游 2026-09-17 新增（issue #129）：开头连续 system/developer
            # 块后插网关 system，既有消息逐字不动。我们此前只认 custom/passthrough，
            # 会把用户填的合法值拒掉（上游支持、面板说不合法）。
            if mode not in ('custom', 'append', 'passthrough'):
                raise ValueError('prompt.mode 只能是 custom、append 或 passthrough')
            out[key] = mode
        elif section == 'prompt' and key == 'file':
            # 路径非空但不可读会让上游启动直接失败（fail fast），
            # 因此这里做基础合法性检查，并明确提示风险
            path = str(raw or '').strip()
            if _has_control_chars(path):
                raise ValueError('prompt.file 不能包含换行或控制字符')
            out[key] = path
        elif section == 'admin' and key == 'enabled':
            # 上游对这组配置有 **fail-fast**：`admin.enabled=true` 且 `api_key`
            # 为空时 normalize() 直接返回错误、拒绝启动（其 config.go 原话：
            # 「admin.enabled=true 但 api_key 为空：请设置 api_key 或将
            # admin.enabled 置 false」）。开关本意是「管理端点必须有鉴权」——
            # 未鉴权的 disable/revive 比读泄漏危险（可用性操作）。
            #
            # 所以这里必须拦：放行会得到「保存成功、然后上游起不来」这个最难查的
            # 形态（本函数注释里点名的正是它）。可达路径是直接 POST
            # /api/settings/upstream 透传 body，不经过前端表单。
            #
            # 判据用**落盘后的实际状态**：api_key 不在下发/写入白名单里，所以
            # 这里永远读现有配置来判，而不是假定它为空。
            if raw is True and not config.upstream_api_key().strip():
                raise ValueError(
                    '开启账号管理接口需要上游已设置 api_key（上游要求管理端点必须鉴权，'
                    '否则拒绝启动）。请先在上游 config.json 里设置 api_key')
            out[key] = bool(raw)
        elif section == 'upstream' and key in _UPSTREAM_TEXT_KEYS:
            # 单行文本：UA、客户端版本、用量归知名度、设备 token 文件路径
            val = str(raw or '').strip()
            if _has_control_chars(val):
                raise ValueError(f'upstream.{key} 不能包含换行或控制字符')
            if len(val) > _UPSTREAM_TEXT_MAX:
                raise ValueError(f'upstream.{key} 过长（上限 {_UPSTREAM_TEXT_MAX} 字符）')
            out[key] = val
        elif section == 'upstream' and key == 'passthrough_ip':
            if not isinstance(raw, bool):
                raise ValueError('upstream.passthrough_ip 必须是布尔值')
            out[key] = raw
        elif section == 'global' and key == 'enabled':
            # 逃生门开关：false = 锁死纯 CN（上游语义），必须是真布尔
            if not isinstance(raw, bool):
                raise ValueError('global.enabled 必须是布尔值')
            out[key] = raw
        elif section == 'global' and key in ('chat_base', 'billing_base'):
            # Base 地址：单行文本。留空 = 用内置默认（www.workbuddy.ai）
            val = str(raw or '').strip().rstrip('/')
            if _has_control_chars(val):
                raise ValueError(f'global.{key} 不能包含换行或控制字符')
            if len(val) > _UPSTREAM_TEXT_MAX:
                raise ValueError(f'global.{key} 过长（上限 {_UPSTREAM_TEXT_MAX} 字符）')
            if val and not val.startswith(('http://', 'https://')):
                # 上游是按 base + 路径拼接的，缺协议会拼出非法 URL
                raise ValueError(f'global.{key} 需以 http:// 或 https:// 开头')
            out[key] = val
        elif section == 'upstream' and key == 'device_token':
            # 敏感凭据，三种语义要分清：
            #   null   → 显式清除（配置里删掉该键）
            #   非空串 → 设为该值
            #   空串   → 保持原值不变（前端回显的是掩码，留空不能被当成清空）
            if raw is None:
                out['__delete__'] = True
                out.pop(key, None)
                continue
            tok = str(raw or '').strip()
            if _has_control_chars(tok):
                raise ValueError('upstream.device_token 不能包含换行或控制字符')
            # 上游读该文件时也有大小限制（>1KB 忽略），这里给个更保守的上限
            if len(tok) > _UPSTREAM_TEXT_MAX:
                raise ValueError(f'upstream.device_token 过长（上限 {_UPSTREAM_TEXT_MAX} 字符）')
            if tok:
                out[key] = tok
            else:
                out.pop(key, None)
    return out


def save_upstream_config(patch: dict) -> dict:
    """仅允许改写 schedule / pool / cooldown / features / upstash 等非敏感段。

    配置读不到时直接拒绝，绝不基于空 dict 生成新文件覆盖真实配置。
    """
    path = config.UPSTREAM_CONFIG
    if not path.is_file():
        raise FileNotFoundError(f'未找到上游配置文件 {path}，已取消保存')

    try:
        cfg = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f'上游配置文件解析失败，已取消保存: {exc}') from exc
    if not isinstance(cfg, dict):
        raise ValueError('上游配置文件不是合法的 JSON 对象，已取消保存')

    for field in _EDITABLE_SECTIONS:
        if field in patch and isinstance(patch[field], dict):
            cfg.setdefault(field, {})
            clean = _sanitize_section(field, patch[field])
            # __delete__ 表示调用方要求显式删除某些敏感键（见 _sanitize_section）
            if clean.pop('__delete__', False):
                cfg[field].pop('device_token', None)
            cfg[field].update(clean)

    if 'upstash' in patch and isinstance(patch['upstash'], dict):
        incoming = patch['upstash']
        current = cfg.get('upstash')
        current = current if isinstance(current, dict) else {}

        if incoming.get('clear'):
            # 显式关闭：清空 url 与 token
            current = {'url': '', 'token': ''}
        else:
            if 'url' in incoming:
                current['url'] = str(incoming.get('url') or '').strip()
            # token 只在传入非空值时替换：前端回显的是掩码，
            # 留空即表示「保持不变」，避免误清空已配置的凭据
            if str(incoming.get('token') or '').strip():
                current['token'] = str(incoming['token']).strip()

        cfg['upstash'] = {
            'url': str(current.get('url') or ''),
            'token': str(current.get('token') or ''),
        }

    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    # 保存后立即让 realm 的配置缓存失效：否则 global.enabled / 两个 base 的改动
    # 最长要 10 秒后才生效，用户会以为没保存成功
    _realm.invalidate()
    return load_upstream_config()


# ── Upstash 连通性检测 ───────────────────────────────────
def _upstash_rest_base(url: str) -> str | None:
    """把各种写法归一化为 Upstash REST 根地址。

    支持：https://xxx.upstash.io / xxx.upstash.io / rediss://default:tok@xxx.upstash.io:6379
    与 workbuddy2api 的 normalizeURL 保持一致的思路。

    **只做字符串归一化，不做安全判定**：调用方（test_upstash）必须再过一道
    `_reject_internal_host`，否则这里返回的任意主机会被服务端真的请求出去。
    """
    raw = (url or '').strip()
    if not raw:
        return None
    # 去掉 scheme
    if '://' in raw:
        scheme, rest = raw.split('://', 1)
        if scheme.lower() in ('rediss', 'redis'):
            # rediss://user:pass@host:port -> 取 host
            host = rest.rsplit('@', 1)[-1]
            host = host.split(':', 1)[0]
            return f'https://{host}' if host else None
        # https://host/... -> 取 host
        host = rest.split('/', 1)[0].split(':', 1)[0]
        return f'https://{host}' if host else None
    host = raw.split('/', 1)[0].split(':', 1)[0]
    return f'https://{host}' if host else None


# 明确禁止的主机名（云平台元数据服务：SSRF 的头号目标）
_BLOCKED_HOSTNAMES = (
    'metadata.google.internal',
    'metadata.tencentyun.com',
    'metadata',
    'instance-data',
)


def _is_internal_addr(addr: ipaddress._BaseAddress) -> bool:
    """回环 / 私有 / 链路本地（含云元数据 169.254.169.254）/ 保留 / 组播 / 未指定。"""
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _reject_internal_host(host: str) -> str | None:
    """判断主机是否指向内网/本机/元数据服务；是则返回拒绝原因，否则 None。

    为什么必须拦：这是个**服务端代发起请求**的接口（SSRF）。它拿用户给的地址
    去 POST，再把响应片段回显给调用方。若不拦，管理员账号（或被提权到此的
    攻击者）就能用它探测内网、甚至读取云元数据端点（`169.254.169.254` /
    `metadata.tencentyun.com` —— 后者常能拿到实例临时凭证）。

    管理员权限不等于「可以随便发请求」：这类探测是典型的**提权后利用**步骤，
    纵深防御应当在这里就断掉。

    注意允许自定义 Redis 服务商（如自建 Upstash 兼容服务）：所以不是白名单
    域名，而是**排除内网与元数据**——公网主机名/IP 一律放行。

    实现要点（逐条都对应一个实测可绕过的写法，别简化回去）：

      1. **先剥 userinfo**：`https://evil@127.0.0.1` 里真正被连接的是 `127.0.0.1`
         （httpx 会把 `evil@` 当认证信息），而按字符串看它不是 IP 字面量 ——
         不剥就会放行。
      2. **`localhost` 与 `*.localhost` 必须显式拦**：它不是 IP 字面量，
         但解析到回环（RFC 6761 规定 localhost 恒为回环）。
      3. **域名要真的解析再判断**：`127.0.0.1.nip.io` 这类通配 DNS 指向内网，
         纯字符串判断看不出来。
      4. **解析失败按拒绝处理**（fail-closed）：拿不准就不要发请求。
         `test_upstash` 本来就是要探测连通性，拒掉一个解析不出的域名不损失功能。

    已知取舍：

      * 解析与请求之间理论上有 TOCTOU 窗口（DNS 可返回不同结果）。这里不做
        「解析后固定 IP 再连接」——那需要自己管连接池，复杂度远高于收益；
        攻击者要利用它得先控制被解析域名的 DNS，而那已超出本接口的威胁边界。
      * **自建在私网里的 Upstash 兼容服务会被拒**。这是有意的：本接口的职责
        是探测公网 Redis 服务，放行私网地址就等于给出一个内网探针。原实现
        本来就拦私网 IP 字面量（只是漏了「域名解析到私网」这条），所以这不算
        能力回退，只是把同一个口径补齐。确有私网需求时应改用部署侧的网络策略，
        而不是放开这里。
      * DNS 查询失败时**放行**（fail-open），而不是拒绝。这一点与直觉相反，
        但在这里是对的：解析不出来的域名，紧接着的 httpx 请求会**用同一个解析器**
        再解析一次、同样失败 —— 也就是说根本连不上，放行不产生 SSRF 风险。
        若改成 fail-closed，代价是「DNS 一时抽风 → 连通性测试报无法解析」，
        以及**测试环境/离线环境里任何域名都测不了**（我们的假主机名就因此挂掉），
        换来的是一个不存在攻击面。已知取舍里 DNS rebinding 的窗口本就不在本
        接口的威胁边界内（需要攻击者控制域名解析）。
    """
    # 剥 userinfo（取最后一个 @ 之后的部分）与端口；去掉 IPv6 字面量的方括号
    h = (host or '').strip().rsplit('@', 1)[-1].strip()
    h = h.strip('[]').lower()
    if not h:
        return '地址为空'
    # 端口：IPv6 已去括号，剩下的冒号只可能是「host:port」
    if h.count(':') == 1:
        h = h.split(':', 1)[0]
    if not h:
        return '地址为空'
    if h in _BLOCKED_HOSTNAMES or h.endswith('.internal') or h.endswith('.local'):
        return f'{host} 是不允许探测的内部地址'
    if h == 'localhost' or h.endswith('.localhost'):
        return f'{host} 是不允许探测的内部地址'

    # 明文 IP：直接判段
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        addr = None
    if addr is not None:
        if _is_internal_addr(addr):
            return f'{host} 是不允许探测的内部地址'
        return None

    # 域名：解析后逐个地址判断（任一落在内网即拒）。
    #
    # 解析失败 → 放行（见 docstring 的说明：解析不出的域名，接下来那次请求也会
    # 解析失败，连不上就不存在 SSRF）。这里只关心「解析出来的地址是否内网」。
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception:  # noqa: BLE001
        return None
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if _is_internal_addr(resolved):
            return f'{host} 解析到内部地址 {resolved}，不允许探测'
    return None


async def test_upstash(url: str, token: str | None = None) -> tuple[bool, str]:
    """用 Upstash REST 接口探测连通性（PING）。token 留空时取配置文件中的值。

    安全：地址经 `_reject_internal_host` 过滤——这是服务端代发起请求的接口，
    不能让它打到内网或云元数据端点（SSRF）。
    """
    base = _upstash_rest_base(url)
    if not base:
        return False, '请先填写 Upstash 地址'
    host = base.split('://', 1)[-1].split('/', 1)[0]
    blocked = _reject_internal_host(host)
    if blocked:
        return False, blocked

    if not token:
        try:
            cfg = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
            token = str((cfg.get('upstash') or {}).get('token') or '')
        except Exception:  # noqa: BLE001
            token = ''
    if not token:
        return False, '缺少 Upstash Token'

    try:
        async with config.http_client(10, connect=5) as client:
            resp = await client.post(
                f'{base}/ping',
                headers={'Authorization': f'Bearer {token}'},
            )
        if resp.status_code == 401:
            return False, 'Token 无效（401）'
        if resp.status_code >= 400:
            return False, f'Upstash 返回 {resp.status_code}'
        body = resp.text.strip()
        if 'PONG' in body.upper():
            return True, '连接正常（PONG）'
        return True, f'已连通，响应：{body[:60]}'
    except Exception as exc:  # noqa: BLE001
        return False, f'无法连接：{_err_text(exc)}'
