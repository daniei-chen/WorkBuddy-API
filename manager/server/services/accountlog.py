"""把「这次调用实际用了哪个上游账号」回填进请求日志（issue #69）。

为什么要靠日志、而不是直接问上游
--------------------------------
账号是**上游**在它内部选出来的，本端转发时并不知道用了哪个号；上游也不在
响应头里回传（只有 `X-Service`），`/v1/stats` 也只按模型聚合、没有账号维度。
唯一的信息源是它的**容器日志**：

    | #971 | 14:58:13 | deepseek-v4.1-flash | stream | 200 | 6509授(3a3a19b1) | TTFB=1083ms | ...

好在 `docker logs --timestamps` 会给每行加**纳秒级**的 RFC3339 前缀，而本端
`request_logs.ts` 记的也是「请求结束」时刻（`_record` 在流收尾时取
`int(time.time())`），两边能直接对上——实测同一批请求误差 < 1 秒。

为什么值得记（issue 里提的两件事）
----------------------------------
  · **是否在跳账号**：同一把密钥的连续请求若被分散到不同账号，说明上游的轮换
    在工作；若一直落在同一个号上，可能就是「为什么这个号先耗尽」的答案。
  · **缓存为什么没命中**：前缀缓存是**按账号**存的，换了号就必然 miss。
    把账号与 `cache_hit_tokens` 放在一起看才能解释这件事。

失败时的行为
------------
填不上就留空（界面显示「—」）。回填是**旁路**：docker 不可用、日志已滚掉、
模型对不上，都只是少几行数据，绝不能影响转发——与 tasklog / 用量统计同口径。
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re

from .. import db
from . import wb2api

logger = logging.getLogger('workbuddy.accountlog')

# 轮询周期（秒）。上游日志是请求**结束时**才写出的，所以略微滞后即可；
# 间隔取长一些：回填晚几秒对「看谁在跳账号」没有影响，而读一次 docker
# logs 有进程开销，没必要太频繁。
_POLL_SECONDS = 15

# 每次采集读多少行。上游一行一个请求，500 行足以覆盖 15 秒内的活动
# （实测高峰期也就每 15 秒几十条）；读太多只是白白解析。
_TAIL = 500

# 上游的对话请求行。格式见 workbuddy2api `internal/server/logging.go`：
#   | #%03d | HH:MM:SS | model | stream/nostream | status | 昵称(uid8) | TTFB= | tok= | ... | total= |
#
# 开头的 docker 时间戳（可选）单独用 `_TS_PREFIX` 剥离，不写进这个正则——
# 原生部署（WB2API_MODE=native）读的是日志文件，那一行**没有** docker 前缀。
_CHAT_LINE = re.compile(
    r'\|\s*#\d+\s*\|'          # | #971 |
    r'\s*[\d:]+\s*\|'          # | 14:58:13 |
    r'\s*[^|]+\s*\|'           # | deepseek-v4.1-flash |
    r'\s*\S+\s*\|'             # | stream |
    r'\s*(?P<status>\d+)\s*\|'   # | 200 |   ← 状态码
    r'\s*(?P<acct>[^|]+?)\s*\|'  # | 6509授(3a3a19b1) |   ← 账号
    r'\s*TTFB=',               # 用它锚定「后面还有请求字段」，避免误吃任务日志
)

# 模型名在上面那条正则里没单独捕获（它前面有两个「任意非竖线」段，写成命名组
# 会和 status 的匹配打架）。这里按竖线切开后**按位置取**，位置由上游格式固定。
_MODEL_POS = 3

# docker --timestamps 的前缀：2026-09-23T06:58:13.123895379Z
_TS_PREFIX = re.compile(r'^(?P<stamp>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+')


def parse_request_lines(lines: list[str]) -> list[dict]:
    """从上游日志里解析出对话请求记录。

    返回 `[{'ts': 结束时刻(epoch 秒), 'model': 模型名, 'account': '昵称(uid8)'}]`。

    **只收带 docker 时间戳的行**：没有时间戳就无法确定它是哪一次请求
    （日志内容里的 `HH:MM:SS` 精度只有秒，且是上游所在时区的墙钟），
    与其猜错把账号填到别人的记录上，不如不填。
    """
    out: list[dict] = []
    for line in lines:
        m = _CHAT_LINE.search(line)
        if not m:
            continue

        t = _TS_PREFIX.match(line)
        if not t:
            continue
        try:
            # 前缀形如 2026-09-23T06:58:13.123895379Z —— 纳秒部分 Python 的
            # fromisoformat 在 3.11+ 才支持，这里自己截到微秒再解析。
            raw = t.group('stamp')
            head, _, frac = raw[:-1].partition('.')
            stamp = datetime.datetime.strptime(head, '%Y-%m-%dT%H:%M:%S')
            if frac:
                stamp = stamp.replace(microsecond=int(frac[:6].ljust(6, '0')))
            ts = int(stamp.replace(tzinfo=datetime.timezone.utc).timestamp())
        except (ValueError, TypeError):
            continue

        parts = [p.strip() for p in line.split('|')]
        model = parts[_MODEL_POS] if len(parts) > _MODEL_POS else ''
        acct = m.group('acct')
        if not model or not acct or acct == '-':
            continue
        out.append({'ts': ts, 'model': model, 'account': acct})
    return out


def _collect_once() -> int:
    """读一次上游日志并回填账号。返回回填条数（同步，供 to_thread 调用）。"""
    lines = wb2api.read_container_logs(_TAIL, timestamps=True)
    if not lines:
        return 0
    entries = parse_request_lines(lines)
    if not entries:
        return 0
    return db.attach_request_accounts(entries)


async def _loop() -> None:
    while True:
        try:
            # 读 docker logs 是阻塞调用（subprocess），放线程池里跑——
            # 直接 await 会把事件循环卡住（网关的转发也在同一个循环上）。
            filled = await asyncio.to_thread(_collect_once)
            if filled:
                logger.debug('回填账号 %d 条', filled)
        except Exception as exc:  # noqa: BLE001
            # 采集失败只是这一轮没有数据，下一轮继续；不影响任何请求
            logger.warning('账号回填失败（不影响请求）: %s', exc)
        await asyncio.sleep(_POLL_SECONDS)


def start_collector() -> bool:
    """启动后台回填任务（幂等）。需要运行中的事件循环。"""
    global _collector
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _collector is None or _collector.done():
        _collector = loop.create_task(_loop())
    return True


def stop_collector() -> None:
    global _collector
    if _collector and not _collector.done():
        _collector.cancel()
    _collector = None


_collector: asyncio.Task | None = None
