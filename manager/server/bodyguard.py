"""Count actual ASGI bytes before parsing; bound aggregate memory and read time."""
import asyncio
import os
from starlette.responses import JSONResponse


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


class APIBodyGuard:
    """管理面（/api/*）请求体守卫：限制单请求体积、读取超时与带体请求的并发。

    并发名额只约束**带 body 的**请求：内存风险来自把请求体缓冲进内存，GET/HEAD
    没有 body 也不缓冲，不该抢名额。早先所有 /api/* 一起计数、名额只有 4，
    而面板一次刷新会并发十几个请求 —— 实测 10 路并发里 6 路被 503 拒掉，
    页面表现为「部分数据加载失败」。名额可通过 WB_API_CONCURRENCY 调整。
    """

    def __init__(self, app, maximum=16 * 1024 * 1024, timeout=20, concurrency=None):
        self.app, self.maximum, self.timeout = app, maximum, timeout
        self.concurrency = max(1, concurrency if concurrency is not None
                               else _env_int('WB_API_CONCURRENCY', 32))
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or not scope.get('path', '').startswith('/api/'):
            return await self.app(scope, receive, send)
        async def reject(status, detail):
            await JSONResponse({'detail': detail}, status_code=status)(scope, receive, send)
        lengths = [v for k, v in scope.get('headers', []) if k.lower() == b'content-length']
        try:
            declared = int(lengths[0]) if lengths else None
            if len(lengths) > 1 or declared is not None and declared < 0:
                raise ValueError
        except ValueError:
            return await reject(400, 'Content-Length 无效')
        if declared is not None and declared > self.maximum:
            return await reject(413, '请求体超过 16 MiB 上限')
        method = str(scope.get('method') or 'GET').upper()
        # 只有「可能带 body」的请求才占并发名额（见类文档）
        counts = declared is not None or method not in ('GET', 'HEAD', 'OPTIONS')
        if counts and self.active >= self.concurrency:
            return await reject(503, '管理请求容量已满，请稍后重试')
        if counts:
            self.active += 1
        try:
            chunks, size = [], 0
            try:
                async with asyncio.timeout(self.timeout):
                    while True:
                        msg = await receive()
                        if msg['type'] == 'http.disconnect':
                            return
                        chunk = msg.get('body', b'')
                        size += len(chunk)
                        if size > self.maximum:
                            return await reject(413, '请求体超过 16 MiB 上限')
                        chunks.append(chunk)
                        if not msg.get('more_body', False):
                            break
            except TimeoutError:
                return await reject(408, '请求体读取超时')
            if declared is not None and declared != size:
                return await reject(400, 'Content-Length 与实际请求体不一致')
            body = b''.join(chunks)
            chunks.clear()
            delivered = False
            async def replay():
                nonlocal delivered, body
                if not delivered:
                    delivered = True
                    payload, body = body, b''
                    return {'type': 'http.request', 'body': payload, 'more_body': False}
                return await receive()
            await self.app(scope, replay, send)
        finally:
            if counts:
                self.active -= 1
