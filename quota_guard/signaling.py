"""Rendezvous + opaque encrypted fallback. No account credentials or usage storage."""
import asyncio
import hmac
import json
import time

from websockets.asyncio.server import serve


class SignalServer:
    def __init__(self, token):
        if len(token) < 24:
            raise ValueError('访问密钥至少 24 个字符')
        self.token = token
        self.rooms = {}
        self.forwarded = 0

    async def announce(self, room):
        members = self.rooms.get(room, {})
        data = json.dumps(dict(type='peers', peers=sorted(members)))
        await asyncio.gather(*(ws.send(data) for ws in list(members.values())), return_exceptions=True)

    async def handler(self, ws):
        room = device = None
        try:
            if not hmac.compare_digest(ws.request.headers.get('Authorization', ''), 'Bearer '+self.token):
                await ws.close(code=1008, reason='unauthorized')
                return
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            room, device = hello.get('room', ''), hello.get('device', '')
            if (len(room) != 64 or any(c not in '0123456789abcdef' for c in room)
                    or not isinstance(device, str) or not 1 <= len(device) <= 100):
                await ws.close(code=1008, reason='invalid hello')
                return
            members = self.rooms.setdefault(room, {})
            if len(members) >= 16 and device not in members:
                await ws.close(code=1008, reason='room full')
                return
            old = members.get(device)
            if old:
                await old.close(code=1000, reason='device reconnected')
            members[device] = ws
            await self.announce(room)
            window, messages = time.monotonic(), 0
            async for raw in ws:
                if time.monotonic()-window > 1:
                    window, messages = time.monotonic(), 0
                messages += 1
                if messages > 80:
                    await ws.close(code=1008, reason='rate exceeded')
                    break
                message = json.loads(raw)
                if message.get('sender') != device:
                    await ws.close(code=1008, reason='sender mismatch')
                    break
                target = members.get(message.get('recipient'))
                if target:
                    await target.send(raw)
                    self.forwarded += 1
        except Exception:
            pass
        finally:
            if room in self.rooms and self.rooms[room].get(device) is ws:
                self.rooms[room].pop(device, None)
                await self.announce(room)
                if not self.rooms[room]:
                    self.rooms.pop(room, None)

    async def start(self, host='127.0.0.1', port=48733, ssl_context=None):
        return await serve(self.handler, host, port, ssl=ssl_context, max_size=256*1024,
                           max_queue=16, ping_interval=20, ping_timeout=30,
                           compression=None, open_timeout=10, close_timeout=3)
