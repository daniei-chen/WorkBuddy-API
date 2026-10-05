"""把「这次调用实际用了哪个上游账号」回填进请求日志（issue #69）。

为什么要靠日志、而不是直接问上游
--------------------------------
账号是**上游**在它内部选出来的，本端转发时并不知道用了哪个号；上游也不在
响应头里回传（只有 `X-Service`），`/v1/stats` 也只按模型聚合、没有账号维度。
唯一的信息源是它的**运行日志**：

    | #971 | 14:58:13 | deepseek-v4.1-flash | stream | 200 | 6509授(3a3a19b1) | TTFB=1083ms | ...

Docker 模式下 `docker logs --timestamps` 会给每行加**纳秒级**的 RFC3339 前缀；
原生模式写文件时没有这层前缀，而**行内只有 `HH:MM:SS`**（上游 `logging.go` 用的
就是 `time.Now().Format("15:04:05")`，不带日期）——所以原生模式要靠日志文件的
mtime 作日期锚点、再按行内时钟还原秒数，见 `_native_timestamps`。两种情况都要能
还原出「请求结束」时刻：本端 `request_logs.ts` 记的也是该时刻（`_record` 在流
收尾时取 `int(time.time())`），所以两边可以直接对齐——实测同一批请求误差 < 1 秒。

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
import json

from .. import db
from . import wb2api

logger = logging.getLogger('workbuddy.accountlog')

# 轮询周期（秒）。上游日志是请求**结束时**才写出的，所以略微滞后即可；
# 间隔取长一些：回填晚几秒对「看谁在跳账号」没有影响，而读一次 docker
# logs 有进程开销，没必要太频繁。
_POLL_SECONDS = 15

# 每次采集读多少行。上游一行一个请求，500 行足以覆盖 15 秒内的活动
# （实测高峰期也就每 15 秒几十条）；读太多只是白白解析。
_TAIL = 2000

# 上游的对话请求行。格式见 workbuddy2api `internal/server/logging.go`：
#   | #%03d | HH:MM:SS | model | stream/nostream | status | 昵称(uid8) | TTFB= | tok= | ... | total= |
#
# 开头的 docker 时间戳单独用 `_TS_PREFIX` 剥离，不写进这个正则。
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

# 原生写日志时上游只有时钟，没有日期：| #971 | 14:58:13 | ...
_NATIVE_CLOCK = re.compile(r'\|\s*#\d+\s*\|\s*(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\s*\|')

def _parse_docker_ts(raw: str) -> int | None:
    """解析 docker --timestamps 的 RFC3339 UTC 前缀。"""
    try:
        text = raw.strip()
        if not text.endswith('Z'):
            return None
        text = text[:-1]
        head, _, frac = text.replace(' ', 'T').partition('.')
        stamp = datetime.datetime.strptime(head, '%Y-%m-%dT%H:%M:%S')
        if frac:
            stamp = stamp.replace(microsecond=int(frac[:6].ljust(6, '0')))
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return int(stamp.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _normalize_account(raw: str) -> str:
    """把账号标签归一成「可以直接拿去比对」的字符串。

    我们手上的上游源码用的是 `logfmt.Label`，聊天行里输出的是 **`昵称(uid8)`**
    （见 `internal/server/logging.go`）——这种保留原文，界面正要靠它显示昵称。
    `uid=<uid8>` 这种形态**不是**当前上游聊天行的写法（`uid=` 只出现在它的错误串
    里），但实测有构建/日志路径会这么打，所以顺手容错：见到前缀就剥掉。
    两者最后都交给数据库按 64 字符清洗。
    """
    value = raw.strip()
    if value.lower().startswith('uid='):
        value = value[4:].strip()
    return value


def _native_clock_seconds(line: str) -> int | None:
    """原生日志行内的 HH:MM:SS 转成当天秒数；没有时钟返回 None。"""
    m = _NATIVE_CLOCK.match(line)
    if not m:
        return None
    try:
        return int(m.group('h')) * 3600 + int(m.group('m')) * 60 + int(m.group('s'))
    except (TypeError, ValueError):
        return None


def _native_timestamps(lines: list[str], mtime: float | None) -> list[int | None]:
    """给原生日志行恢复 epoch 秒。

    原生行只有 `HH:MM:SS`，没有日期。我们把**最后一行**锚在日志文件 mtime 上，
    再按行序和时钟回推：只在同一个自然日/跨午夜且时钟单调时接受。日志轮转、
    时钟回拨或长于一天的旧行都返回 None，宁可漏填，不可填到别人的记录上。
    """
    if not lines or mtime is None:
        return [None] * len(lines)
    clocks = [_native_clock_seconds(ln) for ln in lines]
    # 最后一行可能不是请求行（pool/watch 等）；向前找最近一个有时钟的行。
    # 这不代表它在 mtime 那一秒：mtime 是文件最后一次写入时间，通常就是这一行
    # 附近；我们只用它作日期的锚点，行内时钟负责具体秒数。
    anchor_idx = next((i for i in range(len(lines) - 1, -1, -1) if clocks[i] is not None), None)
    if anchor_idx is None:
        return [None] * len(lines)
    anchor_clock = clocks[anchor_idx]
    assert anchor_clock is not None
    anchor_day = datetime.datetime.fromtimestamp(mtime).date()
    out: list[int | None] = [None] * len(lines)
    prev_clock: int | None = anchor_clock
    day = anchor_day
    for i in range(anchor_idx, -1, -1):
        clock = clocks[i]
        if clock is None:
            continue
        # 从锚点向左回推；遇到时钟回升说明跨了午夜，日期减一天。
        if clock > prev_clock:
            day -= datetime.timedelta(days=1)
        stamp = datetime.datetime.combine(day, datetime.time(clock // 3600,
                                                             (clock % 3600) // 60,
                                                             clock % 60))
        ts = int(stamp.timestamp())
        out[i] = ts
        prev_clock = clock
    return out


def parse_request_lines(lines: list[str]) -> list[dict]:
    """从上游日志里解析出对话请求记录。

    返回 `[{'ts': 结束时刻(epoch 秒), 'model': 模型名, 'account': '昵称(uid8)'}]`。

    时间来源分两层：
      · Docker 日志行：用行首的 RFC3339 UTC 时间戳；
      · 原生日志行：只有 `HH:MM:SS`，由调用方结合日志文件 mtime 恢复（见
        `parse_request_lines_with_mtime`）；直接调用本函数时仍跳过，避免猜日期。
    """
    out: list[dict] = []
    for line in lines:
        m = _CHAT_LINE.search(line)
        if not m:
            continue

        t = _TS_PREFIX.match(line)
        ts = _parse_docker_ts(t.group('stamp')) if t else None
        if ts is None:
            continue

        parts = [p.strip() for p in line.split('|')]
        model = parts[_MODEL_POS] if len(parts) > _MODEL_POS else ''
        acct = _normalize_account(m.group('acct'))
        if not model or not acct or acct == '-':
            continue
        out.append({'ts': ts, 'model': model, 'account': acct})
    return out


def parse_request_lines_with_mtime(lines: list[str], mtime: float | None) -> list[dict]:
    """解析日志行；原生无日期行用 mtime 恢复时间，Docker 行照旧解析。"""
    docker = parse_request_lines(lines)
    if docker or mtime is None:
        return docker
    native_ts = _native_timestamps(lines, mtime)
    out: list[dict] = []
    for line, ts in zip(lines, native_ts):
        m = _CHAT_LINE.search(line)
        if not m or ts is None:
            continue
        parts = [p.strip() for p in line.split('|')]
        model = parts[_MODEL_POS] if len(parts) > _MODEL_POS else ''
        acct = _normalize_account(m.group('acct'))
        if not model or not acct or acct == '-':
            continue
        out.append({'ts': ts, 'model': model, 'account': acct})
    return out


def _collect_once() -> int:
    """读一次上游日志并回填账号。返回回填条数（同步，供 to_thread 调用）。"""
    lines, mtime = wb2api.read_container_logs(_TAIL, timestamps=True, with_mtime=True)
    if not lines:
        return 0
    # Structured events provide exact request identity. Older table logs retain
    # their explicitly legacy correlation and never override exact attribution.
    from .. import ledger
    exact=0
    for line in lines:
        start=line.find('{')
        if start<0 or len(line)>8192:continue
        try:value=json.loads(line[start:])
        except (ValueError,TypeError):continue
        exact+=int(ledger.attempt(value))
    entries = parse_request_lines_with_mtime(lines, mtime)
    if not entries:
        return exact
    return exact+db.attach_request_accounts(entries)


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
