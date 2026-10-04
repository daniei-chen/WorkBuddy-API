"""用量统计聚合。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from .. import db, security
from ..services import wb2api

router = APIRouter(prefix='/api/stats', tags=['stats'])


# days 的取值范围。**必须有上限**：超大整数在 SQLite 绑定时溢出抛错
# （实测 /api/logs?days=999999999999999 返回 500）。
# 10 年足够覆盖任何正常查询，同时避免溢出与全表扫描。
_DAYS_MAX = 3650


def _clamp_days(days: int | None, default: int) -> int:
    """把 days 钳到 [1, _DAYS_MAX]；非法值退回默认。"""
    try:
        d = int(days) if days is not None else default
    except (TypeError, ValueError):
        return default
    return min(_DAYS_MAX, max(1, d))


def _since(days: int) -> str:
    """这段时间范围的**起始日期**（本地时区，含首日）。

    语义是「近 N 天」且**含今天**：`days=1` 正好就是「今天一天」（起始日 = 今天），
    `days=7` 是今天与前 6 天。所以界面上的「今日」传 1 就够，不必另开参数——
    趋势图、按模型、按密钥三处与时段选择器因此天然同一口径（issue #53）。
    """
    d = _clamp_days(days, 30)
    return time.strftime('%Y-%m-%d', time.localtime(time.time() - (d - 1) * 86400))


@router.get('/summary')
def summary(realm: str | None = None,
            user: dict = Depends(security.current_user)) -> dict:
    """总览。

    realm（cn / global）非空时只统计该版本——界面按版本切换时用。

    附带的 `usage_health` 用于暴露「统计根本没在累计」这种情况：曾经有个缺陷
    （issue #9）让存量库上一行都写不进 usage_daily，而表现是完全静默的
    （页面照常轮询、数字只是不动），拖了几个版本才被发现。判据是**今天的
    请求日志有内容但今天的统计为 0**——正常部署不该出现这种组合。

    另外附带 `failures`（失败请求数，按 4xx / 5xx 分档）。**它单独取自
    request_logs，不能从 usage_daily 算**——后者只累计有 token 或有扣费的请求，
    被拒绝的调用与全池不可用（503，零 token）在里面**根本不存在**。实测线上
    一次凌晨的全池中断：31 次请求全部失败，而趋势图上显示为「没有请求」，
    用户只能去翻日志才知道出过事。详见 `_failures`。
    """
    today = time.strftime('%Y-%m-%d')
    week = _since(7)
    # 过滤片段：realm 为空不过滤（保持既有行为），非空则按列匹配
    rf = ' AND realm = ?' if realm in ('cn', 'global') else ''
    rargs: tuple = (realm,) if rf else ()

    def agg(where: str, args: tuple) -> tuple[int, int, float]:
        row = db.query_one(
            f'SELECT COALESCE(SUM(requests),0) AS r, '
            f'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t, '
            f'COALESCE(SUM(credit),0) AS c FROM usage_daily WHERE {where}{rf}',
            args + rargs,
        )
        return int(row['r']), int(row['t']), float(row['c'] or 0)

    t_req, t_tok, t_credit = agg('day = ?', (today,))
    w_req, w_tok, w_credit = agg('day >= ?', (week,))
    a_req, a_tok, a_credit = agg('1=1', ())

    # 活跃密钥：密钥本身**不分版本**（同一把密钥可调两个版本，由模型名决定路由），
    # 所以按版本时应统计「**该版本有调用**的密钥数」，而不是「属于该版本的密钥数」
    # ——后者在架构上不存在。如实标注口径，避免让人以为密钥是隔离的。
    if realm in ('cn', 'global'):
        active_keys = db.query_one(
            'SELECT COUNT(DISTINCT key_id) AS c FROM usage_daily WHERE realm = ?',
            (realm,),
        )['c']
    else:
        active_keys = db.query_one('SELECT COUNT(*) AS c FROM api_keys WHERE enabled = 1')['c']
    top = db.query_one(
        'SELECT model, SUM(requests + prompt_tokens + completion_tokens) AS score '
        f'FROM usage_daily WHERE 1=1{rf} GROUP BY model ORDER BY score DESC LIMIT 1',
        rargs,
    )
    return {
        'today_requests': t_req,
        'today_tokens': t_tok,
        # 实际扣费（上游 usage.credit 合计）；上游未返回该字段时恒为 0
        'today_credit': t_credit,
        'week_credit': w_credit,
        'total_credit': a_credit,
        'week_requests': w_req,
        'week_tokens': w_tok,
        'total_requests': a_req,
        'total_tokens': a_tok,
        'active_keys': int(active_keys),
        'top_model': top['model'] if top else None,
        'usage_health': _usage_health(today, t_req, realm),
        'failures': _failures(realm),
    }


def _failures(realm: str | None = None) -> dict:
    """今天 / 近 7 天的失败请求数，按 4xx 与 5xx 分档。

    为什么必须单独算：`usage_daily` 只累计**有 token 或有扣费**的请求
    （`bump_usage` 的调用条件是 `if total or credit`，刻意如此——否则「只有被
    拒绝的调用」的部署会被 `_usage_health` 误报成「统计没在累计」）。代价是
    失败请求在用量表里**完全不存在**：

      · 403 / 429（密钥被拒、配额用尽、限流）→ 零 token，不累计；
      · 503（池里没有可用账号）→ 零 token，不累计；
      · 502（上游不可用）→ 同样。

    于是界面上「请求数」只反映成功的量，趋势图里失败的那段是**空白**——用户
    看不到"出过事"，只能去翻日志。实测线上凌晨一次全池中断：31 次请求全部
    失败，趋势图上却显示为没有请求。

    `request_logs` 是**无条件**记录每一笔的（见 `gateway._record`），所以它是
    失败数的唯一可靠来源。这里不做任何「是否该累计」的过滤：凡是被拒或被
    上游打回的都是失败，正是用户想看到的。

    口径说明：只统计**带了密钥**的请求（`key_id IS NOT NULL`）。没有密钥的
    401 是扫描器/配置错误打进来的噪声，混进来会让失败数失去信号价值。
    """
    out: dict = {
        'today_4xx': 0, 'today_5xx': 0,
        'week_4xx': 0, 'week_5xx': 0,
    }
    try:
        today = time.strftime('%Y-%m-%d')
        week = _since(7)
        # realm 过滤：日志表的 realm 列可能是历史 NULL（该列是后加的），按 cn 归类
        # ——**必须写成 `COALESCE(realm,'cn') = ?`，不能写成 `COALESCE(realm,?) = ?`**：
        # 后者会把 NULL 兜成「当前查询的那个版本」，于是看国际版时历史 NULL 行
        # 也被算进来（实测：只看 global 却统计到 cn 的历史记录）。
        rf = " AND COALESCE(realm, 'cn') = ?" if realm in ('cn', 'global') else ''
        for label, since in (('today', today), ('week', week)):
            # 用 `ts >= 当地零点` 而不是 `day_sql(ts) >= 日期串`：两者逐行等价
            # （见 test_day_filter_equivalence.py 的边界样本），但后者把 ts 包在
            # 函数里，查询计划退化成 SCAN（扫整个索引），前者是 SEARCH（按范围
            # 定位）。本接口在总览页被 30 秒轮询，是热路径。
            args = (db.day_start_ts(since),) + ((realm,) if rf else ())
            row = db.query_one(
                f"SELECT SUM(CASE WHEN status >= 400 AND status < 500 THEN 1 ELSE 0 END) AS c4, "
                f"SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS c5 "
                f"FROM request_logs WHERE ts >= ? AND key_id IS NOT NULL{rf}",
                args,
            )
            if row:
                out[f'{label}_4xx'] = int(row['c4'] or 0)
                out[f'{label}_5xx'] = int(row['c5'] or 0)
    except Exception:  # noqa: BLE001
        # 统计属旁路：任何一个数算不出来都不该让总览接口失败（失败计数只是
        # 锦上添花，总览本身有更多关键数字）。返回全 0，前端按「无失败」显示。
        pass
    return out


def _usage_health(today: str, today_requests: int, realm: str | None = None) -> dict:
    """检测「用量统计没有在累计」——本该累计的请求有日志、但统计为 0。

    为什么值得单独做：统计写入是旁路（失败只记 warning，不影响转发），
    所以一旦写入路径坏了，**用户侧完全看不出异常**：页面照常刷新、数字只是
    停着不动。issue #9 就是这个形态，拖了几个版本才有人发现。

    **判据必须与「什么请求才会累计用量」完全对齐**，否则会误报。这是被两次
    真实误报教会的：

      1. **版本口径**：日志按全版本数、用量按当前版本过滤 → 只有国内版流量时
         切到国际版必然触发（实测报告过 605 次那个数字）；
      2. **请求类型**：`_record` 里 `request_logs` 是**无条件**写的，而
         `bump_usage` 只在 `key 且 (tokens 或 credit)` 时调用。于是「今天只有
         被拒绝的调用」（403 / 429 / 无 key）或「只调了 /v1/models」（0 token）
         会留下日志却没有用量行 —— 那种部署**完全健康**，却被报「统计可能没有
         正常写入」（实测复现过）。

    所以这里只数**本该产生用量行**的那部分日志：有 key、且 token 或扣费非 0。
    这与 gateway._record 的判断条件是同一份口径；那边改了这里也要跟着改。

    返回里带上结构化数字，**不要让前端去正则解析这句中文**（曾经这么做，
    改文案时前端静默失效、译文形同虚设）。
    """
    try:
        # 与用量同一口径：看某版本时只看该版本（COALESCE 把历史 NULL 归 cn）
        rgt = ' AND COALESCE(realm, ?) = ?' if realm in ('cn', 'global') else ''
        # 只数「本该累计」的：与 _record 的 `if key:` 且 `if total or credit:` 对齐
        should = (
            'key_id IS NOT NULL'
            " AND (COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0) > 0"
            '      OR COALESCE(credit,0) > 0)'
        )
        # 用 ts 的半开区间 [当地零点, 次日零点) 代替 `day_sql(ts) = 日期串`：
        # 逐行等价（见 test_day_filter_equivalence.py），但前者走索引。
        #
        # **等值必须给上下界**：只写 `ts >= 零点` 会把次日、下月、明年的记录
        # 全算成今天 —— 那比慢更糟（数字直接错）。所以这里明确写 `< 次日零点`。
        logs_today = db.query_one(
            f'SELECT COUNT(*) AS c FROM request_logs '
            f'WHERE ts >= ? AND ts < ? AND {should}{rgt}',
            (db.day_start_ts(today), db.day_start_ts(today) + 86400)
            + (('cn', realm) if rgt else ()),
        )
        n = int(logs_today['c']) if logs_today else 0
    except Exception:  # noqa: BLE001
        return {'ok': True, 'detail': '', 'logs_today': 0}
    if n > 0 and today_requests == 0:
        scope = {'cn': '国内版', 'global': '国际版'}.get(str(realm or ''), '')
        return {
            'ok': False,
            'logs_today': n,
            'detail': (f'今天{scope}已有 {n} 次调用记录，但用量统计为 0——统计可能没有 '
                       f'正常写入。可尝试「修复统计」；若仍为 0，请查看服务端日志。'),
        }
    return {'ok': True, 'detail': '', 'logs_today': 0}


@router.post('/repair-usage')
def repair_usage(user: dict = Depends(security.require_admin)) -> dict:
    """按请求日志回填用量统计的缺口（幂等，可重复执行）。

    用于修复历史缺陷导致的部分调用未计入统计。
    """
    return db.backfill_usage_from_logs()


@router.post('/rebuild-usage')
def rebuild_usage(user: dict = Depends(security.require_admin)) -> dict:
    """以请求日志为准重建用量统计（清理时区口径不一致造成的重复计数）。

    与 /repair-usage 的区别：repair 只补缺口（增量、幂等），
    本接口是**重建**——会替换 usage_daily 的内容，能删除此前多出来的行。
    """
    return db.rebuild_usage_from_logs()


@router.get('/daily')
def daily(days: int = 30, realm: str | None = None,
          user: dict = Depends(security.current_user)) -> list[dict]:
    """按天聚合。realm 非空时只统计该版本。

    `failed` 是当天的失败请求数（4xx / 5xx 合计），**来自 request_logs 而不是
    usage_daily**：后者不含被拒绝与零 token 的失败请求，失败数在其中恒为 0
    （详见 `_failures`）。趋势图用它把「失败」画出来——否则全池中断那天的
    曲线是空的，看不出出过事。
    """
    rf = ' AND realm = ?' if realm in ('cn', 'global') else ''
    args = (_since(max(1, days)),) + ((realm,) if rf else ())
    rows = db.query(
        'SELECT day, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(credit),0) AS credit, '
        'COALESCE(SUM(cache_hit_tokens),0) AS cache_hit_tokens, '
        'COALESCE(SUM(cache_miss_tokens),0) AS cache_miss_tokens '
        f'FROM usage_daily WHERE day >= ?{rf} GROUP BY day ORDER BY day ASC',
        args,
    )
    failures = _daily_failures(days, realm)
    days_seen = {r['day'] for r in rows}
    out = [
        {
            'day': r['day'],
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'credit': float(r['credit'] or 0),
            'failed': failures.get(r['day'], 0),
            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),
            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),
        }
        for r in rows
    ]
    # **只有失败、没有成功**的日子也要出现：那一天正是最该被看到的。
    # 只按 usage_daily 出行的话，全池中断日会整条缺失（它没有任何成功请求），
    # 趋势图上等于那天不存在——比画成 0 更糟。
    for day, n in sorted(failures.items()):
        if day not in days_seen and n > 0:
            out.append({
                'day': day, 'requests': 0, 'prompt_tokens': 0,
                'completion_tokens': 0, 'credit': 0.0, 'failed': n,
            })
    out.sort(key=lambda d: d['day'])
    return out


def _daily_failures(days: int, realm: str | None = None) -> dict[str, int]:
    """按天统计失败请求数（4xx + 5xx）。口径与 `_failures` 一致。"""
    try:
        # 同 `_failures`：NULL 固定归 cn，不能把 NULL 兜成查询目标版本
        rf = " AND COALESCE(realm, 'cn') = ?" if realm in ('cn', 'global') else ''
        # 过滤用 ts 范围（走索引），**分组**仍必须用日期表达式（要按天聚合，
        # 这个没法避免）。两者分开：过滤是热路径上的大头，分组只作用于过滤后的行。
        args = (db.day_start_ts(_since(max(1, days))),) + ((realm,) if rf else ())
        rows = db.query(
            f"SELECT {db.day_sql('ts')} AS day, COUNT(*) AS n FROM request_logs "
            f"WHERE ts >= ? AND status >= 400 AND key_id IS NOT NULL{rf} "
            f'GROUP BY day',
            args,
        )
        return {str(r['day']): int(r['n']) for r in rows if r['day']}
    except Exception:  # noqa: BLE001
        return {}


@router.get('/hourly')
def hourly(day: str | None = None, realm: str | None = None,
           user: dict = Depends(security.current_user)) -> list[dict]:
    """某天**按小时**聚合（默认今天）。「今日」趋势图用它 —— 范围只有一天时
    按天聚合只会得到一根柱子，看不出「今天什么时候忙」。

    与 `/daily` 同一套口径：
      · 用量取自 `usage_hourly`，它与 `usage_daily` 由**同一处写入**累计，
        所以这里的合计与页头卡片天然一致（清日志、日志保留期裁剪都不影响）；
      · `failed` 仍来自 `request_logs`（`usage_*` 只累计有 token 或有扣费的请求，
        被拒绝与零 token 的失败在里面根本不存在）。

    **固定返回 24 个桶（补零）**：X 轴长度不随「今天走到几点」变化，图上不会
    因为还没到的时段而抖动；没数据的时段就是 0，读起来也清楚。
    """
    d = (day or '').strip() or time.strftime('%Y-%m-%d')
    # 只接受 YYYY-MM-DD：这是个查询参数，写错形态就回落到今天，而不是抛错
    # （趋势图不该因为一个手改的 URL 变成红色错误态）。
    if len(d) != 10 or d[4] != '-' or d[7] != '-':
        d = time.strftime('%Y-%m-%d')
    rf = ' AND realm = ?' if realm in ('cn', 'global') else ''
    rargs: tuple = (realm,) if rf else ()
    rows = db.query(
        'SELECT hour, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, '
        'SUM(completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(credit),0) AS credit '
        f'FROM usage_hourly WHERE day = ?{rf} GROUP BY hour ORDER BY hour ASC',
        (d,) + rargs,
    )
    by_hour = {int(r['hour']): r for r in rows}
    failures = _hourly_failures(d, realm)
    out: list[dict] = []
    for h in range(24):
        r = by_hour.get(h)
        out.append({
            'day': d,
            'hour': h,
            'requests': int(r['requests'] or 0) if r else 0,
            'prompt_tokens': int(r['prompt_tokens'] or 0) if r else 0,
            'completion_tokens': int(r['completion_tokens'] or 0) if r else 0,
            'credit': float(r['credit'] or 0) if r else 0.0,
            'failed': failures.get(h, 0),
        })
    return out


def _hourly_failures(day: str, realm: str | None = None) -> dict[int, int]:
    """某天**按小时**的失败请求数（4xx + 5xx）。口径与 `_failures` 一致。"""
    try:
        rf = " AND COALESCE(realm, 'cn') = ?" if realm in ('cn', 'global') else ''
        # 过滤用 ts 范围（走索引），分组用小时表达式（要聚合，躲不开）
        args = (db.day_start_ts(day), db.day_start_ts(day) + 86400) + ((realm,) if rf else ())
        rows = db.query(
            f"SELECT {db.hour_sql('ts')} AS bucket, COUNT(*) AS n FROM request_logs "
            f"WHERE ts >= ? AND ts < ? AND status >= 400 AND key_id IS NOT NULL{rf} "
            f'GROUP BY bucket',
            args,
        )
        out: dict[int, int] = {}
        for r in rows:
            bucket = str(r['bucket'] or '')
            if len(bucket) >= 13:
                out[int(bucket[11:13])] = int(r['n'])
        return out
    except Exception:  # noqa: BLE001
        return {}


@router.get('/by-model')
def by_model(days: int = 30, realm: str | None = None,
             user: dict = Depends(security.current_user)) -> list[dict]:
    """按模型聚合。realm 非空时只统计该版本。"""
    rf = ' AND realm = ?' if realm in ('cn', 'global') else ''
    args = (_since(max(1, days)),) + ((realm,) if rf else ())
    rows = db.query(
        'SELECT model AS name, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(credit),0) AS credit, '
        'COALESCE(SUM(cache_hit_tokens),0) AS cache_hit_tokens, '
        'COALESCE(SUM(cache_miss_tokens),0) AS cache_miss_tokens '
        f'FROM usage_daily WHERE day >= ?{rf} GROUP BY model ORDER BY SUM(prompt_tokens + completion_tokens) DESC',
        args,
    )
    return [
        {
            'name': r['name'] or '未知',
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'credit': float(r['credit'] or 0),
            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),
            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),
        }
        for r in rows
    ]


@router.get('/by-key')
def by_key(days: int = 30, realm: str | None = None,
           user: dict = Depends(security.current_user)) -> list[dict]:
    """按密钥聚合。realm 非空时只统计该版本。

    密钥本身不分版本（同一把密钥可调两个版本），这里筛的是「该密钥在该版本上
    的调用量」——所以同一把密钥在两个版本下都会出现，数字各是各的。这是如实
    反映架构，不是重复计数。
    """
    rf = ' AND u.realm = ?' if realm in ('cn', 'global') else ''
    args = (_since(max(1, days)),) + ((realm,) if rf else ())
    rows = db.query(
        'SELECT COALESCE(k.name, u.key_id || "") AS name, SUM(u.requests) AS requests, '
        'SUM(u.prompt_tokens) AS prompt_tokens, SUM(u.completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(u.credit),0) AS credit, '
        'COALESCE(SUM(u.cache_hit_tokens),0) AS cache_hit_tokens, '
        'COALESCE(SUM(u.cache_miss_tokens),0) AS cache_miss_tokens '
        'FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id '
        f'WHERE u.day >= ?{rf} GROUP BY u.key_id ORDER BY SUM(u.prompt_tokens + u.completion_tokens) DESC',
        args,
    )
    return [
        {
            'name': r['name'] or '未知',
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'credit': float(r['credit'] or 0),
            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),
            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),
        }
        for r in rows
    ]


@router.get('/upstream')
async def upstream_stats(user: dict = Depends(security.current_user)) -> dict:
    """上游自己那份统计（`/v1/stats`，issue #59）。

    **口径与这一页其它数字不同，界面上必须分开摆**：本页的汇总 / 趋势 / 按模型 /
    按密钥都来自本网关的库、只含经过本网关的调用、且跟时段选择器走；这里返回的是
    上游进程自己的累计（含**直连 7863** 的调用），并且是从上游启动算起，没有时段
    可言。用户要看「原有那把密钥用了多少」只能从这里看——那把密钥直连上游，
    本网关看不见它。

    取不到时返回 `available=False` 与原因（上游没起来 / 版本太旧没这个端点 /
    api_key 不一致），界面据此说明，而不是显示一片空白让人以为「用量是 0」。
    """
    return await wb2api.get_upstream_stats()
