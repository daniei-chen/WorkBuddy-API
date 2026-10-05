"""Count actual ASGI bytes before parsing; bound aggregate memory and read time."""
import asyncio
from starlette.responses import JSONResponse

class APIBodyGuard:
    def __init__(self, app, maximum=16 * 1024 * 1024, timeout=20, concurrency=4):
        self.app, self.maximum, self.timeout = app, maximum, timeout
        self.active, self.concurrency = 0, concurrency

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
        if self.active >= self.concurrency:
            return await reject(503, '管理请求容量已满，请稍后重试')
        self.active += 1
        slot_held = True
        def release_slot():
            nonlocal slot_held
            if slot_held:
                self.active -= 1
                slot_held = False
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
                    # Bound buffered request bodies, not the whole handler lifetime.
                    # Slow dashboard queries must not occupy the body-read budget.
                    release_slot()
                    return {'type': 'http.request', 'body': payload, 'more_body': False}
                return await receive()
            if not body:
                release_slot()
            await self.app(scope, replay, send)
        finally:
            release_slot()
