"""Authenticated data channels, with encrypted WebSocket fallback and reconnect."""
import asyncio
import hashlib
import json
import ssl
import threading
import time

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from websockets.asyncio.client import connect

from .pairing import Cipher, validate_url


def make_mesh(config, account, on_message):
    if config.get('link_enabled'):
        from .file_mesh import FileMesh
        return FileMesh(config, account, on_message)
    return Mesh(config, account, on_message)


class Mesh:
    def __init__(self, config, account, on_message, local_test=False):
        self.config, self.device = dict(config), config['device_id']
        self.account = account
        # Isolate pre-enrollment-policy clients even when an old invitation is reused.
        self.cipher = Cipher(config['group_secret'], 'account-policy-v2:'+account)
        self.on_message = on_message
        self.local_test = local_test
        self.loop = None
        self.ws = None
        self.stopping = False
        self.peers = {}
        self.channels = {}
        self.pcs = {}
        self.tasks = set()
        self.status = '发现服务未连接'
        self.thread = None
        self.supervisor = None

    def start(self):
        validate_url(self.config['rendezvous_url'], self.local_test)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        async def run():
            self.loop = asyncio.get_running_loop()
            self.supervisor = asyncio.create_task(self._connect_loop())
            try:
                await self.supervisor
            except asyncio.CancelledError:
                pass
            finally:
                await self._cleanup()
        asyncio.run(run())

    async def _connect_loop(self):
        while not self.stopping:
            try:
                kwargs = dict(additional_headers={'Authorization': 'Bearer '+self.config['relay_token']},
                              open_timeout=15, close_timeout=2, max_size=256*1024, compression=None,
                              proxy=None if self.local_test else True)
                pin = self.config.get('fingerprint', '').lower().replace(':', '').replace(' ', '')
                if pin:
                    if len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin):
                        raise ValueError('发现服务证书指纹无效')
                    # Verify the cert during TLS before sending the WebSocket HTTP authorization.
                    # websockets doesn't expose a pin callback: self-signed users must install
                    # their CA locally instead. Public deployments use normal CA validation.
                    raise ValueError('P2P 模式使用系统信任的 TLS 证书；请清空旧指纹并配置可信 WSS 服务')
                async with connect(self.config['rendezvous_url'], **kwargs) as ws:
                    self.ws = ws
                    self.status = '发现服务已连接'
                    await ws.send(json.dumps(dict(room=self.cipher.room, device=self.device)))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get('type') == 'peers':
                            await self._peer_list(msg['peers'])
                        else:
                            await self._receive(msg, '中转')
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.status = '连接失败，自动重试 · '+type(e).__name__
                if isinstance(e, ValueError):
                    self.status = str(e)
            finally:
                self.ws = None
                # Existing direct channels may continue if only rendezvous goes away.
            if not self.stopping:
                await asyncio.sleep(5)

    def _task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if not t.cancelled():
                try:
                    t.result()
                except Exception:
                    pass
        task.add_done_callback(done)

    async def _peer_list(self, devices):
        for peer in devices:
            if peer == self.device:
                continue
            self.peers.setdefault(peer, dict(route='中转', last_seen=0))
            if not self.config.get('force_relay') and self.device < peer:
                pc = self.pcs.get(peer)
                if pc is None or pc.connectionState in ('failed', 'closed'):
                    self._task(self._offer(peer))
        for peer in list(self.peers):
            if peer not in devices and not self._direct(peer):
                self.peers.pop(peer, None)

    def _direct(self, peer):
        ch = self.channels.get(peer)
        return ch and ch.readyState == 'open' and ch.bufferedAmount < 512*1024

    async def _pc(self, peer):
        old = self.pcs.pop(peer, None)
        if old:
            await old.close()
        stun = self.config.get('stun_url', '')
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=stun)] if stun else []))
        self.pcs[peer] = pc
        @pc.on('datachannel')
        def on_channel(channel):
            self._channel(peer, channel)
        return pc

    def _channel(self, peer, channel):
        self.channels[peer] = channel
        @channel.on('open')
        def opened():
            self.peers.setdefault(peer, {})['route'] = 'P2P 直连'
        @channel.on('message')
        def message(raw):
            if isinstance(raw, str) and len(raw) <= 256*1024:
                self._task(self._receive(json.loads(raw), 'P2P 直连'))
        @channel.on('close')
        def closed():
            if peer in self.peers:
                self.peers[peer]['route'] = '中转'

    async def _offer(self, peer):
        pc = await self._pc(peer)
        self._channel(peer, pc.createDataChannel('quota', ordered=True))
        await pc.setLocalDescription(await pc.createOffer())
        await self._send(peer, 'offer', dict(sdp=pc.localDescription.sdp, type='offer'), signal=True)

    async def _receive(self, envelope, route):
        try:
            value = self.cipher.open(envelope, self.device)
            peer, kind = envelope['sender'], envelope['kind']
            self.peers.setdefault(peer, {}).update(route=route, last_seen=time.time())
            if kind == 'offer' and not self.config.get('force_relay'):
                pc = await self._pc(peer)
                await pc.setRemoteDescription(RTCSessionDescription(**value))
                await pc.setLocalDescription(await pc.createAnswer())
                await self._send(peer, 'answer', dict(sdp=pc.localDescription.sdp, type='answer'), signal=True)
            elif kind == 'answer' and peer in self.pcs:
                await self.pcs[peer].setRemoteDescription(RTCSessionDescription(**value))
            elif kind == 'app':
                self.on_message(peer, value)
        except Exception:
            # Unauthenticated, stale or malformed peer messages cannot reach the ledger.
            return

    async def _send(self, peer, kind, value, signal=False):
        raw = json.dumps(self.cipher.seal(self.device, peer, kind, value), separators=(',', ':'))
        if len(raw) > 240*1024:
            raise ValueError('同步消息过大')
        if not signal and not self.config.get('force_relay') and self._direct(peer):
            self.channels[peer].send(raw)
        elif self.ws:
            await self.ws.send(raw)

    def send(self, peer, value):
        if self.loop and not self.stopping:
            future = asyncio.run_coroutine_threadsafe(self._send(peer, 'app', value), self.loop)
            # Delivery is verified by anti-entropy vectors, not by this send completing.
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    def peer_states(self):
        return {k: dict(v) for k, v in list(self.peers.items())}

    async def _cleanup(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.ws:
            await self.ws.close()
        await asyncio.gather(*(pc.close() for pc in self.pcs.values()), return_exceptions=True)

    def close(self):
        self.stopping = True
        if self.loop and self.supervisor:
            async def goodbye():
                for peer in list(self.peers):
                    try:
                        await self._send(peer, 'app', dict(type='bye', account=self.account))
                    except Exception:
                        pass
            try:
                asyncio.run_coroutine_threadsafe(goodbye(), self.loop).result(timeout=3)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.supervisor.cancel)
        if self.thread:
            self.thread.join(timeout=20)
