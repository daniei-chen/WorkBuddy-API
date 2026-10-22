"""token 自动续期（issue #40）。

## 背景：为什么需要这个模块

用户报的是「登录的账号那个 token 不会自动续期，到期了需要重新扫码或登录」。
排查后确认这不是我们的 bug，而是**没有被任何一方覆盖到的空档**：

上游 workbuddy2api **有**刷新能力，但只在三个**内部**时机触发：

  · 保活排程：`schedule.keepalive_hours`，**默认每天只有 22 点一次**；
  · 每次 chat 选号后、token 距到期不足 `RefreshSkew`（默认 10 分钟）时；
  · 签到前 token 临近过期时。

后两条都要求**真的有人在用这个号**。于是下面这几种情况下 token 会一路走到
过期而无人续期：

  · 账号长期闲置、没有对话流量（第二、三条永不触发）；
  · 上游容器停机、或调度器没跑（第一条也断了）；
  · 保活时刻那一刻网络抖动，而下次要等一整天。

而 accessToken 的有效期是**有限的**（实测签发 60 天；短的可到几天）。一旦过期，
该账号就彻底不可用了——只能重新扫码。用户看到的正是这个。

## 本模块做什么

进程内定时巡检，**只做上游没覆盖的那部分**：账号的 token 剩余时间低于阈值时
主动调腾讯的刷新接口续期，并把新 token 原子写回账号文件（上游热加载会自己
捡起来）。

刻意**不重复上游的保活排程**：
  · 不按整点跑（上游已有 keepalive_hours），而是按**剩余寿命**判断——
    「快过期了才刷」，与「到点就刷一遍」是两件事，前者才能覆盖长期闲置的号；
  · 不看上游的开关（`keepalive_enabled=false` 是用户对**上游排程**的选择，
    而 token 过期会让账号彻底报废，属于我们不能放任的那类问题）；
  · 不做签到、不做任何业务动作，只续期。

## 阈值为什么是「剩余 3 天」

太短（比如上游选号路径的 10 分钟）要求巡检足够频繁才赶得上，而巡检间隔本身
有几小时的粒度；太长会在 token 刚签发时白白多刷几次。3 天给了足够余量：
巡检间隔（1 小时）远小于它，且一个 60 天有效期的令牌只会在最后 5% 寿命里被刷。

刷新失败时**不做惩罚**（不冷却、不禁用账号）：续期是尽力而为的辅助动作，
失败可能只是网络抖动；真正的可用性判断仍由上游的选号与保活负责。失败会记在
任务日志里（kind=keepalive），用户能看见。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .. import config, db
from . import tencent, wb2api

logger = logging.getLogger(__name__)

# 巡检间隔（秒）。1 小时：与「剩余 3 天」的阈值配合，最多晚 1 小时发现
# 待续期账号，仍远早于过期。
_CHECK_INTERVAL_SECONDS = 3600

# 剩余寿命低于此值即续期（秒）。见模块文档「阈值为什么是剩余 3 天」。
_RENEW_BEFORE_SECONDS = 3 * 86400

# 连续写入失败（磁盘只读等）时不刷屏的间隔
_LOG_THROTTLE_SECONDS = 6 * 3600

_loop_task: asyncio.Task | None = None
_last_fail_log: dict[str, float] = {}


def _needs_renew(expires_at: int, now: int | None = None) -> bool:
    """该到期时刻是否已进入续期窗口。"""
    if expires_at <= 0:
        return False          # 解不出到期时间：不臆测，交给上游
    now = int(time.time()) if now is None else now
    return expires_at - now <= _RENEW_BEFORE_SECONDS


def _account_payload(raw: dict) -> dict:
    """账号文件 → `tencent.refresh_token` 需要的扁平结构。"""
    acct = raw.get('account') or {}
    auth = raw.get('auth') or {}
    return {
        'access_token': auth.get('accessToken', ''),
        'refresh_token': auth.get('refreshToken', ''),
        'uid': acct.get('uid', ''),
        'enterprise_id': acct.get('enterpriseId', ''),
        'domain': auth.get('domain', ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
    }


async def renew_once(now: int | None = None) -> dict:
    """巡检一遍所有账号，返回统计（`{checked, renewed, failed, skipped}`）。

    逐个串行：账号数是个位数到几十，且腾讯那边对高频请求有风控——并发刷新
    得不偿失（上游保活也是串行）。
    """
    stats = {'checked': 0, 'renewed': 0, 'failed': 0, 'skipped': 0}
    for acct in wb2api.list_auth_accounts():
        # 面板停用（改名）的账号不续期：它已退出账号池，用户明确表示不用它。
        # 但**状态位停用（manual_disabled）要照常续期**——那条路的语义是
        # 「只摘对话流量，凭证与积分保持活跃」（issue #45）。
        if acct.get('disabled_by_panel'):
            stats['skipped'] += 1
            continue
        if not _needs_renew(int(acct.get('expires_at') or 0), now):
            continue
        stats['checked'] += 1
        name = acct['file']
        try:
            raw = wb2api.read_account_file_any(name)
        except Exception as exc:  # noqa: BLE001
            _note_failure(name, f'读取失败: {exc}')
            stats['failed'] += 1
            continue
        payload = _account_payload(raw)
        if not payload['refresh_token']:
            # 没有 refreshToken 就是真的续不了（要么重新扫码，要么它本就只有
            # 一个长期令牌）。记一次日志，别静默——用户看任务记录能明白原因。
            _note_failure(name, '缺少 refreshToken，无法续期（需重新扫码或登录）')
            stats['failed'] += 1
            continue
        ok, message, fields = await tencent.refresh_token(payload)
        if not ok:
            _note_failure(name, message)
            stats['failed'] += 1
            continue
        try:
            tencent.update_auth_tokens(name, fields)
        except Exception as exc:  # noqa: BLE001
            _note_failure(name, f'刷新成功但写入失败: {exc}')
            stats['failed'] += 1
            continue
        stats['renewed'] += 1
        _last_fail_log.pop(name, None)
        logger.info('token 已续期: %s（%s）', name, message)
        _record(acct, 1, f'token 自动续期成功（{message}）')
    return stats


def _note_failure(name: str, message: str) -> None:
    """记失败：日志与任务记录都写，但**限频**避免每小时刷屏。"""
    now = time.time()
    last = _last_fail_log.get(name, 0.0)
    if now - last < _LOG_THROTTLE_SECONDS:
        return
    _last_fail_log[name] = now
    logger.warning('token 续期失败: %s（%s）', name, message)
    _record({'uid': '', 'nickname': name, 'file': name}, 0, f'token 自动续期失败：{message}')


def _record(acct: dict, credits: float, message: str) -> None:
    """落一条任务日志（复用「令牌保活」这一类，用户能在任务页看到）。

    为什么单独记一条而不是只打日志：用户排查「为什么这个号掉线了」时看的是
    任务记录页，不是容器日志（容器日志重建即丢）。续期是账号存活的关键动作，
    必须留痕。
    """
    try:
        uid = str(acct.get('uid') or '') or str(acct.get('file') or '')
        db.add_task_logs([{
            'ts': int(time.time()),
            'uid': uid,
            'kind': 'keepalive',
            'level': 'credit' if credits else 'warn',
            'credits': credits,
            'message': message,
            # 同一账号同一秒内重复写会被去重；续期事件天然低频，加时间戳即可
            'dedup_key': f'renew:{uid}:{int(time.time())}',
        }])
    except Exception as exc:  # noqa: BLE001
        logger.warning('写续期任务日志失败: %s', exc)


async def _loop() -> None:
    """定时巡检（首轮延迟一点，避免与启动期的其他初始化抢资源）。"""
    await asyncio.sleep(30)
    while True:
        try:
            stats = await renew_once()
            if any(stats[k] for k in ('renewed', 'failed')):
                logger.info('token 续期巡检：%s', stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning('token 续期巡检异常: %s', exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start_scheduler() -> bool:
    """启动续期巡检（幂等）。无事件循环时返回 False（如 CLI 场景）。"""
    global _loop_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _loop_task is None or _loop_task.done():
        _loop_task = loop.create_task(_loop())
    return True


def stop_scheduler() -> None:
    global _loop_task
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
