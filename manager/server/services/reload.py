"""上游重载编排：把「改配置 / 增删账号后需要重启容器」收敛为自动、可观测的动作。

背景
----
workbuddy2api 只在进程启动时读取 config.json 与扫描 auths/ 目录
（见 cmd/server/main.go：auth.LoadDir + p.SyncToDir + 各 SetXxx 注入），
之后不再重读，也不处理 SIGHUP。因此任何影响这两者的改动都必须重启容器。

**一处例外**（上游 2026-09-18 起）：auths 目录加了热加载——每 5 秒轮询目录指纹，
凭证文件增删改会自动重新加载，那时不必重启（见其 internal/pool/watch.go）。
但 **config.json 仍然只读一次**，所以改配置依旧必须重启；账号相关的改动我们
照旧触发一次重启，好处是新旧上游都能立即生效（新版不必等那 5 秒轮询）。

为什么可以直接自动重启
--------------------
- 实测 `docker restart` 到服务可用约 0.45 秒
- 上游实现了优雅停机：收到 SIGTERM 先 p.Flush() 落盘，再 srv.Shutdown(5s)
  等在途请求结束，因此不会掐断正在进行的对话
- 容器 restart 策略为 unless-stopped

多次连续改动会合并为一次重启，避免并发重启互相干扰。
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import wb2api

logger = logging.getLogger('workbuddy.reload')

# 合并窗口：这段时间内的多次请求只触发一次重启
COALESCE_SECONDS = 0.8

_state: dict = {
    'running': False,
    'pending': False,
    'last_at': 0.0,
    'last_ok': None,
    'last_message': '',
    'restart_count': 0,
}
_pending = False
_worker: asyncio.Task | None = None
_lock = asyncio.Lock()


def state() -> dict:
    """当前重载状态，供前端展示。"""
    return dict(_state)


async def _worker_loop() -> None:
    global _pending
    async with _lock:
        _state['running'] = True
        try:
            while _pending:
                _pending = False
                _state['pending'] = False
                # 等一小段，把紧挨着的多次改动合并成一次重启
                await asyncio.sleep(COALESCE_SECONDS)
                if _pending:
                    # 等待期间又有新请求，重新开始计时
                    continue
                ok, message = await wb2api.restart_container()
                _state['restart_count'] += 1
                _state['last_at'] = time.time()
                _state['last_ok'] = ok
                _state['last_message'] = message
        finally:
            _state['running'] = False
            _state['pending'] = False


def request_restart() -> bool:
    """请求一次上游重载（幂等：短时间内多次调用只重启一次）。

    立即返回，不阻塞请求；实际重启在后台完成。
    返回 False 表示没有运行中的事件循环，无法调度后台任务。
    """
    global _pending, _worker
    _pending = True
    _state['pending'] = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环：无法后台调度，交由调用方决定是否同步执行
        return False
    if _worker is None or _worker.done():
        _worker = loop.create_task(_worker_loop())
    return True


async def restart_now() -> tuple[bool, str]:
    """立即重启并等待结果，供「重启上游」这类显式操作使用。"""
    return await wb2api.restart_container()


# ── 加账号后的收尾：优先热加载，超时才重启 ────────────────────────────
#
# 背景（用户反馈）：国际版用链接 / GitHub 快速登录，界面提示登录成功之后，
# 后台还要 20-30 秒才看到账号。链路是这样：
#
#   落盘成功 → 我们**直接重启上游** → 前端收到成功立刻刷新账号列表 → 那一刷
#   里 `get_status()` 去请求上游 /status，而上游正在重启、连不上 → 面板侧的上游
#   调用超时是 WB_UPSTREAM_TIMEOUT（默认 120 秒）→ 请求一直挂着，直到容器起来
#   才返回。用户看到的「20-30 秒」就是**等容器重启**，账号文件其实早就落盘了。
#
# 而上游自 2026-09-19 起会热加载 auths 目录（约 5 秒一轮），新版根本不需要重启。
# 所以这里改成：先给它一点时间，账号出现在 /status 里就结束；只有「上游是旧版本
# 不热加载」或「上游整个不可达」时才回退到重启（那两个情形下重启是唯一的出路）。
HOT_RELOAD_WAIT_SECONDS = 8.0    # 上游热加载周期约 5 秒，留一次往返的余量
HOT_RELOAD_POLL_SECONDS = 1.0
_pending_uids: set[str] = set()
_reload_worker: asyncio.Task | None = None


async def _reload_worker_loop(wait_seconds: float) -> None:
    deadline = time.monotonic() + wait_seconds
    saw_upstream = False
    while _pending_uids and time.monotonic() < deadline:
        st = await wb2api.get_status()
        if st.get('connected'):
            saw_upstream = True
            seen = {str(a.get('uid') or '') for a in (st.get('accounts') or [])}
            _pending_uids.difference_update(seen)
            if not _pending_uids:
                logger.info('新账号已由上游热加载收录，无需重启')
                return
        await asyncio.sleep(HOT_RELOAD_POLL_SECONDS)

    if not _pending_uids:
        return
    # 超时仍未收录：老上游不热加载；或者上游不可达（重启是唯一出路）。
    # 两种情况都回退到原来的重启，且**只重启一次**（哪怕同时加了多个账号）。
    logger.info('热加载未在 %.0fs 内生效（上游%s），回退重启',
                wait_seconds, '可达但未收录' if saw_upstream else '不可达')
    _pending_uids.clear()
    ok, message = await wb2api.restart_container()
    _state['restart_count'] += 1
    _state['last_at'] = time.time()
    _state['last_ok'] = ok
    _state['last_message'] = message


def request_reload_or_restart(uid: str) -> bool:
    """新增账号后的收尾：先等热加载，超时才重启（见上面的说明）。

    与 `request_restart` 一样立即返回、后台执行；同时加多个账号只会重启一次
    （等待期间把后加入的 uid 一并纳入判断）。
    返回 False 表示没有运行中的事件循环，调用方自行决定。
    """
    global _reload_worker
    uid = str(uid or '').strip()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if uid:
        _pending_uids.add(uid)
    if _reload_worker is None or _reload_worker.done():
        _reload_worker = loop.create_task(_reload_worker_loop(HOT_RELOAD_WAIT_SECONDS))
    return True

