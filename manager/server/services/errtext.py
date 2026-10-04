"""把异常渲染成**用户看得懂的一句原因**。

为什么单独一个模块：面板会把服务端的失败原因直接显示给用户（toast / 列表里的
「刷新异常: …」之类），而很多异常的 `str()` 是**空的** —— `TimeoutError()`、
`asyncio.CancelledError`、部分 httpx 超时都是这样。直接 `f'{exc}'` 的结果就是
「刷新异常: 」这种断在冒号上的提示（issue #132 的截图），用户看不出发生了什么。

统一走这里：空文本时退化成异常类型名（`TimeoutError` 比空白有用得多），
并给超时/取消这两类最常见的网络原因一句人话。
"""
from __future__ import annotations

import asyncio


def err_text(exc: BaseException) -> str:
    """异常 → 一句原因。保证非空。"""
    detail = str(exc).strip()
    if detail:
        return f'{type(exc).__name__}: {detail}'
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return '超时（等不到响应）'
    if isinstance(exc, asyncio.CancelledError):
        return '请求已取消'
    return type(exc).__name__
