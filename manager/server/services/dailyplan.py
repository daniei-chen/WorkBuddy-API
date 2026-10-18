"""每日任务计划：到点自动跑「签到 / 领奖 / 做任务」这三个管理端动作。

## 与上游调度器的关系（重要，别混）

上游 workbuddy2api 自带一套调度器（`config.json` 的 `schedule`：checkin /
travel / activity / school / cat / keepalive），**每天已经在自动签到**。
本模块是**管理端自己**的调度器，跑的是管理端的「一键」动作（复用
`accounts.checkin_all` 与 `taskrun`），两者互相独立：

  · 签到会**重叠**（上游 9/21 点一次 + 这里一次）——幂等，不会出错，但没必要；
    界面上必须说清「上游本来就签到」，别让用户以为点了这里才签。
  · 领奖 / 做任务是管理端 `task_runner.py` 的能力，上游没有，所以它们**只能**
    靠这里定时。

## 四个任务的安全等级（决定了默认值与界面措辞）

| 任务 | 安全性 | 说明 |
|---|---|---|
| `checkin` 签到 | 安全 | 幂等（腾讯对重复签到回 10001） |
| `compensation` 领取补偿 | 安全 | 有则领、无则空请求（幂等）；**日常自动由上游负责**，这里是手动兜底 |
| `claim` 领奖 | 安全 | 只认领**已完成**任务的奖励，不伪造任何行为 |
| `full` 做任务 | **有风险** | 会伪造活跃上报（造画布 / 连续对话 / 批量用专家）——上游脚本自己都标「慎用」 |

因此 `full` **默认关闭**，且在界面上带明确警告；四个任务默认都不开，由用户显式启用
（`compensation` 虽然最安全，但上游每天都在领同一笔，默认开只是每天多一次空请求）。

`taskrun._claim_loop`（上游注释里说明「有意不做前端入口」的那套）只管 claim，
且默认关闭、无界面；本模块是**唯一**做了界面的管理端调度器。两者若同时开，
claim 会被触发两次（幂等，无害）——实际不会发生，因为前者默认关。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .. import db
from . import taskrun

logger = logging.getLogger(__name__)

_KEY = 'daily_tasks_plan'
# 顺序即界面顺序
_TASKS = ('checkin', 'compensation', 'claim', 'full')
_DEFAULTS: dict[str, dict] = {
    'checkin': {'enabled': False, 'hour': 9, 'last_run': ''},
    # compensation **默认关闭**，与「上游会不会领」这件事绑定，别只看面板：
    #   · 上游调度器的活跃上报（默认就开着）每天已经会给每个国内版账号领一次补偿
    #     （runActivity → claimGrowthRewards → ClaimCompensation），所以**不差这一条**；
    #   · 面板这一条的价值是「**马上领**」——补偿公告都带截止日期，等到下一个整点
    #     或下游版本没开活跃上报时，用户点一下就有结果，不用等；
    #   · 换句话说：日常自动由上游负责，这里提供的是手动兜底与「领没领到」的回执。
    #     默认开会让每个账号每天多一次注定「无可领」的写请求，没必要。
    'compensation': {'enabled': False, 'hour': 10, 'last_run': ''},
    'claim': {'enabled': False, 'hour': 10, 'last_run': ''},
    'full': {'enabled': False, 'hour': 10, 'last_run': ''},
}
# 巡检间隔（秒）：与触发粒度（整点）配合，最多晚 1 分钟发现
_TICK_SECONDS = 60
_loop_task: asyncio.Task | None = None

# 公开别名：路由层要用它校验「立即执行」的 task 参数与本文件保持一致。
# 让路由自己再写一份清单，加任务时必然漏掉一处，而漏掉的症状是「计划里能开、
# 手动执行报 400」——两边都不报错的静默不一致。
TASKS = _TASKS


def _normalise(raw: object) -> dict:
    """把库里的值收敛成合法计划：字段缺失 / 被手改成非法值时逐项兜底。"""
    plan = {t: dict(v) for t, v in _DEFAULTS.items()}
    if isinstance(raw, dict):
        for t in _TASKS:
            v = raw.get(t)
            if not isinstance(v, dict):
                continue
            if isinstance(v.get('enabled'), bool):
                plan[t]['enabled'] = v['enabled']
            h = v.get('hour')
            if isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23:
                plan[t]['hour'] = h
            if isinstance(v.get('last_run'), str):
                plan[t]['last_run'] = v['last_run']
    return plan


def get_plan() -> dict:
    return _normalise(db.get_setting(_KEY))


def set_plan(body: object) -> dict:
    """保存计划；校验失败抛 ValueError / TypeError（路由转 400）。

    `last_run` 保持不变：改时刻不该把「今天已经跑过」这件事抹掉，否则改完设置
    会在当天的目标小时里**又跑一次**。
    """
    if not isinstance(body, dict):
        raise TypeError('body 必须是对象')
    plan = get_plan()
    for t in _TASKS:
        v = body.get(t)
        if not isinstance(v, dict):
            raise TypeError(f'{t} 必须是对象')
        enabled, hour = v.get('enabled'), v.get('hour')
        if not isinstance(enabled, bool):
            raise TypeError(f'{t}.enabled 必须是布尔值')
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValueError(f'{t}.hour 必须是 0-23 的整点')
        plan[t]['enabled'] = enabled
        plan[t]['hour'] = hour
    db.set_setting(_KEY, plan)
    return plan


def _stamp(now: int) -> str:
    """「哪一天的哪个整点」——同一天同一小时只跑一次的去重键。"""
    lt = time.localtime(now)
    return f'{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}-{lt.tm_hour:02d}'


def is_due(task: str, plan: dict | None = None, now: int | None = None) -> bool:
    """该任务现在是否到点该跑。

    两条同时成立：①已启用；②当前整点 == 配置的整点，且**这一小时还没跑过**。
    按整点对齐而非精确到秒——巡检本就是一分钟一跳，秒级对齐永远命中不了。
    """
    plan = get_plan() if plan is None else _normalise(plan)
    if task not in plan:
        raise ValueError(f'未知任务：{task}')
    cfg = plan[task]
    if not cfg['enabled']:
        return False
    now = int(time.time()) if now is None else now
    if time.localtime(now).tm_hour != cfg['hour']:
        return False
    return cfg['last_run'] != _stamp(now)


def mark_ran(task: str, now: int | None = None) -> dict:
    """记下该任务这一小时已经跑过（无论成功失败，避免同一小时反复重试）。"""
    plan = get_plan()
    plan[task]['last_run'] = _stamp(int(time.time()) if now is None else now)
    db.set_setting(_KEY, plan)
    return plan


async def run_task(task: str) -> tuple[bool, str]:
    """执行一次任务，返回 `(是否触发成功, 说明)`。

    `taskrun` 一次只允许一个任务在跑：claim / full 若撞上正在执行的任务会返回
    False（如实转达，不假装成功）。
    """
    if task not in _TASKS:
        raise ValueError(f'未知任务：{task}')
    if task == 'checkin':
        # 延迟导入：routers.accounts 顶层 import 了 taskrun，这里再顶层
        # import accounts 会成环。同理这里的 `user` 只是路由的函数签名要求，
        # 函数体不用它（依赖注入由框架负责，后台任务直接给个管理员身份）。
        from ..routers import accounts as accounts_router  # noqa: PLC0415
        out = await accounts_router.checkin_all(upstream_id=None,
                                                user={'role': 'admin'})
        total = out.get('total', 0)
        ok_n = out.get('succeeded', 0)
        already = out.get('already', 0)
        return True, f'签到 {ok_n}/{total} 成功' + (f'（{already} 个今日已签）' if already else '')
    if task == 'compensation':
        from ..routers import accounts as accounts_router  # noqa: PLC0415
        out = await accounts_router.claim_compensation_all(upstream_id=None,
                                                           user={'role': 'admin'})
        parts = []
        if out.get('claimed'):
            parts.append(f"{out['claimed']} 个领到 +{out.get('credit', 0)} 积分")
        if out.get('already'):
            parts.append(f"{out['already']} 个已领过")
        if out.get('nothing'):
            parts.append(f"{out['nothing']} 个无可领")
        if out.get('failed'):
            parts.append(f"{out['failed']} 个失败")
        message = '；'.join(parts) or '没有可执行的账号'
        # 全都「无可领」是正常态（补偿是一次性的），不算失败
        return not out.get('failed'), message
    mode = 'claim' if task == 'claim' else 'full'
    return await taskrun.start_async(mode, 'ALL')


async def _loop() -> None:
    """定时巡检（首轮稍延迟，避开启动期的其他初始化）。"""
    await asyncio.sleep(20)
    while True:
        try:
            plan = get_plan()
            for task in _TASKS:
                if not is_due(task, plan):
                    continue
                logger.info('每日任务触发：%s（%s 点档）', task, plan[task]['hour'])
                try:
                    ok, msg = await run_task(task)
                    logger.info('每日任务 %s：ok=%s %s', task, ok, msg)
                except Exception as exc:  # noqa: BLE001
                    # 单个任务出错不该拖垮整轮巡检（下一个任务照常判）
                    logger.warning('每日任务 %s 执行异常：%s', task, exc)
                # 无论成败都记「这一小时跑过」：否则失败会每分钟重试一次，刷屏
                mark_ran(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning('每日任务巡检异常：%s', exc)
        await asyncio.sleep(_TICK_SECONDS)


def start_scheduler() -> bool:
    """启动巡检（幂等）。无事件循环时返回 False（如 CLI 场景）。"""
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
