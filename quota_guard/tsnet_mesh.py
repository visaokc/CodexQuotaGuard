"""Authenticated incremental ledger messages over a private embedded tailnet node."""
import hashlib
import json
from pathlib import Path
import threading
import time

from .pairing import Cipher
from .storage import atomic_json
from .tsnet_node import EmbeddedNode, route_label, tail_ip


class TailscaleMesh:
    history_limit = 400
    history_bytes = 160*1024

    def __init__(self, config, account, on_message):
        self.config, self.account, self.on_message = dict(config), account, on_message
        self.device = config['device_id']
        self.cipher = Cipher(config['group_secret'], 'account-policy-v2:'+account)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = self.node = None
        self.status = '内嵌 Tailscale · 正在启动'
        self.diagnostics = dict(phase='starting', transport='tailscale')
        self.peers, self.pending = {}, {}
        self.peer_error = ''
        group = hashlib.sha256(config['group_secret'].encode()).hexdigest()[:20]
        self.peer_file = Path(config['_data_dir'])/('tailscale-peers-'+group+'.json')
        self.removed_file = Path(config['_data_dir'])/('tailscale-removed-'+group+'.json')
        self.removed = set()
        self.membership = {}
        self.join_consumed = ''
        if self.removed_file.exists():
            saved = json.loads(self.removed_file.read_text(encoding='utf-8'))
            if isinstance(saved, list):
                self.membership = {p: [1, True, 'legacy'] for p in self._removed_ids(saved)}
            else:
                self.membership = self._membership_records(saved['members'])
                self.join_consumed = saved.get('join_consumed', '')
            self.removed = {p for p, record in self.membership.items() if record[1]}
        self.join_requested = config.get('tailscale_join_request', '')
        self.addresses = {}
        try:
            saved = json.loads(self.peer_file.read_text(encoding='utf-8'))
            if isinstance(saved, dict):
                self.addresses.update({p: ip for p, ip in saved.items() if isinstance(p, str) and 1 <= len(p) <= 100 and tail_ip(ip)})
        except (OSError, ValueError):
            pass
        self.addresses.update({p: ip for p, ip in config.get('tailscale_peers', {}).items()
                               if isinstance(p, str) and 1 <= len(p) <= 100 and tail_ip(ip)})
        self.addresses.pop(self.device, None)
        self.addresses = dict(list(self.addresses.items())[:16])

    @staticmethod
    def _removed_ids(value):
        if not isinstance(value, list) or len(value) > 1024 or any(
                not isinstance(p, str) or not 1 <= len(p) <= 100 for p in value):
            raise ValueError('设备组移除记录无效')
        return set(value)

    def removed_devices(self):
        with self.lock:
            return set(self.removed)

    @staticmethod
    def _membership_records(value):
        if not isinstance(value, dict) or len(value) > 1024:
            raise ValueError('设备组成员记录无效')
        for p, record in value.items():
            if (not isinstance(p, str) or not 1 <= len(p) <= 100
                    or not isinstance(record, list) or len(record) != 3
                    or type(record[0]) is not int or not 1 <= record[0] <= 10**12
                    or type(record[1]) is not bool or not isinstance(record[2], str)
                    or not 1 <= len(record[2]) <= 100):
                raise ValueError('设备组成员记录无效')
        return {p: list(record) for p, record in value.items()}

    def membership_records(self):
        with self.lock:
            return {p: list(record) for p, record in self.membership.items()}

    def _merge_membership(self, records, consume_join=False):
        records = self._membership_records(records)
        with self.lock:
            merged = dict(self.membership)
            for p, record in records.items():
                if p not in merged or tuple(record) > tuple(merged[p]):
                    merged[p] = record
            if len(merged) > 1024:
                raise ValueError('设备组成员记录已达上限')
            consumed = self.join_consumed
            if consume_join and self.join_requested and consumed != self.join_requested:
                # Only an explicit accept_pair creates a new request. Consume it
                # after hearing the peer's current removal revision, not at startup.
                revision = max((r[0] for r in merged.values()), default=0)+1
                merged[self.device] = [revision, False, self.device]
                consumed = self.join_requested
            if merged != self.membership or consumed != self.join_consumed:
                atomic_json(self.removed_file, dict(members=merged, join_consumed=consumed))
                self.membership, self.join_consumed = merged, consumed
                self.removed = {p for p, record in merged.items() if record[1]}
                self.peers = {p: s for p, s in self.peers.items() if p not in self.removed and self.device not in self.removed}
                self.pending = {k: v for k, v in self.pending.items() if k[0] not in self.removed and self.device not in self.removed}

    def _merge_removed(self, removed):
        self._merge_membership({p: [1, True, 'legacy'] for p in removed})

    def remove_device(self, peer):
        if self.device in self.removed_devices():
            raise ValueError('本机已被移除，不能再管理设备组')
        if peer == self.device:
            raise ValueError('不能在此移除本机')
        self._removed_ids([peer])
        with self.lock:
            revision = max((r[0] for r in self.membership.values()), default=0)+1
            self._merge_membership({peer: [revision, True, self.device]})

    def _diagnostic(self, event, error=None):
        # No auth URLs, IPC tokens, group secrets or account identifiers on disk.
        value = dict(event=event, at=time.time(), error=type(error).__name__ if error else None,
                     errno=getattr(error, 'errno', None), winerror=getattr(error, 'winerror', None))
        process = getattr(self.node, 'process', None)
        value['pid'] = process.pid if process else None
        try:
            atomic_json(Path(self.config['_data_dir'])/'tailscale'/'mesh-diagnostics.json', value)
        except OSError:
            pass

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def connection_state(self):
        with self.lock:
            return dict(self.diagnostics)

    def peer_states(self):
        with self.lock:
            now = time.time()
            return {p: dict(s, route=s['route'] if now-s.get('route_at', 0)<35 else 'Tailscale · 路径待确认')
                    for p, s in self.peers.items() if now-s['last_seen']<30 and p not in self.removed and self.device not in self.removed}

    def send(self, peer, value):
        # Anti-entropy regenerates unacknowledged facts. Bound pending work per peer/slot.
        slot = 'facts' if value.get('type') == 'facts' else 'sync'
        with self.lock:
            if peer in self.addresses and peer not in self.removed and self.device not in self.removed:
                self.pending[peer, slot] = value

    def _receive(self, packet):
        ip, envelope = packet['ip'], packet['envelope']
        if not tail_ip(ip) or envelope.get('kind') not in ('hello', 'app'):
            return
        value = self.cipher.open(envelope, '*' if envelope['kind'] == 'hello' else self.device)
        peer = envelope['sender']
        if not isinstance(peer, str) or not 1 <= len(peer) <= 100 or peer == self.device:
            return
        if not isinstance(value, dict) or value.get('account') != self.account:
            return
        if envelope['kind'] == 'hello':
            records = value.get('membership')
            if records is None:
                records = {p: [1, True, 'legacy'] for p in self._removed_ids(value.get('removed_devices', []))}
            self._merge_membership(records, consume_join=peer in self.config.get('tailscale_peers', {}))
        with self.lock:
            if envelope['kind'] != 'hello' and (peer in self.removed or self.device in self.removed):
                return
            if peer not in self.addresses and len(self.addresses)>=16:
                return
            fresh = peer not in self.peer_states()
            if self.addresses.get(peer) != ip:
                addresses = dict(self.addresses, **{peer: ip})
                atomic_json(self.peer_file, addresses)
                self.addresses = addresses
            # Keep a reply address for membership-only hellos, including a
            # removed device re-pairing with a member it never contacted before.
            if peer in self.removed or self.device in self.removed:
                return
            state = self.peers.setdefault(peer, dict(route='Tailscale · 路径待确认'))
            state['last_seen'] = time.time()
        if fresh:
            self.on_message(peer, dict(type='peer_ready', account=self.account))
        if envelope['kind'] == 'app':
            self.on_message(peer, value)

    def _poll_local(self, method):
        while not self.stop.is_set():
            try:
                return self.node.request(method)
            except OSError as e:
                process = getattr(self.node, 'process', None)
                if process is None or process.poll() is not None:
                    raise
                # A reset of one loopback HTTP connection is not a node exit.
                # Keep the enrolled node and its pending browser authorization alive.
                self._diagnostic('ipc_retry', e)
                with self.lock:
                    self.diagnostics.update(phase='retrying', ready=False, error=type(e).__name__)
                self.status = '内嵌 Tailscale · 本地通信暂时中断，正在恢复（无需重新登录）'
                self.stop.wait(2)
        return {}

    def _run(self):
        while not self.stop.is_set():
            try:
                self.node = EmbeddedNode(self.config['_data_dir'], self.device)
                self.node.start()
                self._diagnostic('node_started')
                last_hello = last_status = 0
                while not self.stop.is_set():
                    now = time.time()
                    if now-last_status>=2:
                        st = self._poll_local('status')
                        running = st.get('state') == 'Running' and st.get('ready')
                        with self.lock:
                            self.diagnostics = dict(phase='waiting' if running else 'starting', transport='tailscale',
                                ready=bool(running), state=st.get('state'), auth_url=st.get('auth_url', ''),
                                ips=st.get('ips') or [], checked_at=now, tailnet=st.get('tailnet', ''),
                                tailnet_peer_count=st.get('peer_count'))
                        last_status = now
                    if not self.diagnostics.get('ready'):
                        self.status = '内嵌 Tailscale · '+{'NeedsLogin':'请点击授权登录', 'NeedsMachineAuth':'等待管理员批准设备',
                            'Stopped':'节点已停止，请重新连接'}.get(self.diagnostics.get('state'), '正在连接控制服务')
                        self.stop.wait(1)
                        continue
                    for packet in self._poll_local('receive'):
                        try:
                            self._receive(packet)
                        except (ValueError, KeyError, TypeError):
                            continue
                        except Exception:
                            # Invalid authentication must not reach the journal.
                            continue
                    with self.lock:
                        addresses = dict(self.addresses)
                        pending, self.pending = self.pending, {}
                    hello = now-last_hello>=5
                    for peer, ip in addresses.items():
                        if self.stop.is_set():
                            break
                        messages = [(kind, v) for (p, kind), v in pending.items() if p == peer]
                        if hello:
                            messages.insert(0, ('hello', dict(account=self.account, membership=self.membership_records())))
                        for kind, value in messages:
                            if kind != 'hello' and (peer in self.removed_devices() or self.device in self.removed_devices()):
                                continue
                            envelope = self.cipher.seal(self.device, '*' if kind=='hello' else peer,
                                                       'hello' if kind=='hello' else 'app', value)
                            if len(json.dumps(envelope))>240*1024:
                                continue
                            try:
                                self.node.request('send', dict(ip=ip, envelope=envelope))
                            except OSError as e:
                                with self.lock:
                                    self.peers.pop(peer, None)
                                    self.peer_error = ('匹配地址不在当前节点可见的 tailnet 中，请核对两端内嵌节点所属网络'
                                        if getattr(e, 'code', None) == 400 else '对端同步端口不可达，请检查对端节点、网络或访问规则')
                                break
                            else:
                                self.peer_error = ''
                        if peer in self.peer_states() and now-self.peers[peer].get('route_at', 0)>=15:
                            try:
                                probe = self.node.request('route', dict(ip=ip))
                            except OSError:
                                probe = {}
                            with self.lock:
                                self.peers[peer].update(route=route_label(probe), route_at=time.time())
                    if hello:
                        last_hello = now
                    self.status = ('本机已从设备组移除 · 已停止账本同步' if self.device in self.removed_devices()
                                   else f'内嵌 Tailscale · {len(self.peer_states())} 台同账号设备在线')
                    if not self.peer_states() and self.peer_error:
                        self.status += ' · '+self.peer_error
                    if self.diagnostics.get('tailnet'):
                        self.status += ' · 网络：'+self.diagnostics['tailnet']
                    self.stop.wait(.5)
            except Exception as e:
                self._diagnostic('node_failed', e)
                with self.lock:
                    self.peers.clear()
                    self.diagnostics = dict(phase='retrying', transport='tailscale', error=type(e).__name__)
                self.status = '内嵌 Tailscale 重试中 · '+str(e)
            finally:
                if self.node:
                    self.node.close()
            self.stop.wait(5)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                raise RuntimeError('内嵌 Tailscale 尚未退出')
