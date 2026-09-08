import copy
import hashlib
import json
import queue
import threading
import time

from .firewall import Firewall
from .journal import Journal
from .ledger import Ledger
from .mesh import make_mesh
from .meter import Scanner
from .quota import identity, read_quota
from .recovery import HistoryRecovery
from .storage import Database


class Engine:
    def __init__(self, database, config, quota_reader=None, firewall=None, identity_reader=identity, mesh_factory=make_mesh):
        self.db, self.config = database, copy.deepcopy(config)
        self.config['_data_dir'] = str(database.path.parent)
        self.quota_reader = quota_reader or (lambda home: read_quota(home, expected_account=self.last_identity['account']))
        self.identity_reader = identity_reader
        self.mesh_factory = mesh_factory
        self.firewall = firewall or Firewall()
        group = hashlib.sha256(config['group_secret'].encode()).hexdigest()[:20]
        # Legacy journals predate explicit account enrollment; preserve, don't replay them.
        self.group_db = Database(database.path.parent / ('group-v2-'+group+'.sqlite'))
        self.ledger = Ledger(self.group_db)
        self.journal = Journal(self.group_db, self.ledger, config['device_id'])
        self.tracked = config.get('tracked_accounts', {})
        self.scanner = Scanner(database, config['codex_home'], config['device_id'], config['started_at'], self.tracked)
        self.recovery = HistoryRecovery(database, self.group_db, config['codex_home'], config['device_id'])
        self.stop_event, self.wakeup = threading.Event(), threading.Event()
        self.view_lock = threading.Lock()
        self.inbox = queue.Queue(maxsize=500)
        self.commands = queue.Queue()
        self.view = dict(status='准备监测…', identity={}, summary=None, blocked=False,
                         active=0, uncertain=0, notifications=[], error='', mesh='', peers={})
        self.mesh = None
        self.sync_receipts = {}
        self.sync_vectors = {}
        self.blocked = bool(self.db.get('block_state'))
        self.last_identity, self.last_snapshot = None, None
        self.last_read = 0
        self.last_broadcast = 0
        self.notice_key = None
        self.thread = None
        self.background_mode = False
        self.quota_error = ''
        self.quota_error_since = None
        self.quota_error_notified = False
        self.last_recovery = self.db.get('manual_restore_at', 0)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def receive(self, peer, message):
        try:
            self.inbox.put_nowait((peer, message))
            # Bootstrap and history progress should not wait for the 30 s hidden
            # timer. Unchanged presence heartbeats keep the low-idle-cost path.
            bootstrap = (self.last_identity and message.get('account') == self.last_identity['account']
                         and (message.get('type') in ('peer_ready', 'facts') or
                              (message.get('type') == 'sync' and
                               (peer not in self.sync_receipts or message.get('records') or
                                message.get('vector', {}) != self.sync_vectors.get(peer)))))
            if not self.background_mode or bootstrap:
                self.wakeup.set()
        except queue.Full:
            pass

    def run(self):
        try:
            self.scanner.seed()
            while not self.stop_event.is_set():
                try:
                    self.step()
                except Exception as e:
                    with self.view_lock:
                        self.view['error'] = str(e)
                        self.view['status'] = '监测异常；未将错误视为额度重置'
                # Keep account/API switches responsive even when hidden and restricted.
                self.wakeup.wait(2 if self.blocked else 30 if self.background_mode else 5)
                self.wakeup.clear()
        except Exception as e:
            with self.view_lock:
                self.view.update(error=str(e), status='初始化失败')
        finally:
            if self.mesh:
                self.mesh.close()

    def _account(self, ident, now):
        self.sync_receipts = {}
        self.sync_vectors = {}
        if self.mesh:
            self.mesh.close()
            self.mesh = None
        self.last_snapshot, self.last_read = None, 0
        self.quota_error = ''
        self.quota_error_since = None
        self.quota_error_notified = False
        if self.is_tracked(ident):
            account = ident['account']
            self.journal.project(account)
            profile = dict(device=self.config['device_id'], name=self.config['name'],
                           cap=self.tracked[account].get('cap', self.config['quota']))
            if self.group_db.get('own_profile:'+account) != profile:
                self.journal.append(account, 'profile', profile, now)
                self.group_db.put('own_profile:'+account, profile)
            if self.config.get('link_enabled') or self.config['rendezvous_url']:
                self.mesh = self.mesh_factory(self.config, account, self.receive)
                self.mesh.start()
            mark = hashlib.sha256(self.config['group_secret'].encode()).hexdigest()
            if self.db.get('published_group:'+account) != mark:
                with self.db.connect() as db:
                    db.execute("UPDATE outbox SET sent=0 WHERE sent<>2 AND json_extract(payload,'$.account')=? AND json_extract(payload,'$.ts')>?",
                               (account, self.tracked[account]['added_at']))
                self.db.put('published_group:'+account, mark)

    def is_tracked(self, ident):
        return ident['mode'] == 'account' and ident['account'] in self.tracked

    @staticmethod
    def scope_key(ident):
        return ident['mode'], ident['account'], ident.get('revision')

    def scanner_scope(self, ident):
        return dict(identity=json.dumps(self.scope_key(ident), sort_keys=True),
                    home=str(self.scanner.home.resolve()), device=self.config['device_id'],
                    added_at=self.tracked.get(ident['account'], {}).get('added_at'))

    def scope_changed(self, ident, now):
        if self.scope_key(self.identity_reader(self.config['codex_home'])) == self.scope_key(ident):
            return False
        if self.blocked:
            self.restore()
        self.db.put('scanner_scope', None)
        self.recovery.boundary(self.db.get('scope_observed_at'))
        self.scanner.boundary(now)
        if self.mesh:
            self.mesh.close()
            self.mesh = None
        self.ledger.logout(self.config['device_id'])
        with self.view_lock:
            self.view.update(summary=None, blocked=False, status='登录配置已变化，等待重新确认账号', peers={}, mesh='未连接')
        return True

    def publish_events(self, account, now):
        eligible = self.scanner.pending(account=account, limit=400)
        self.scanner.ack([e['id'] for e in eligible if e['ts'] <= self.tracked[account]['added_at']])
        eligible = [e for e in eligible if e['ts'] > self.tracked[account]['added_at']]
        # Leave room for runtime provenance under the journal's 24 KB limit.
        for i in range(0, len(eligible), 20):
            batch = eligible[i:i+20]
            self.journal.append(account, 'events', batch, now)
            self.scanner.ack([e['id'] for e in batch])

    def recover_inactive(self, current_account, now):
        # These are old tracked-account requests, not current API usage. Publish
        # locally only; inactive account groups never use the current connection.
        for account, profile in self.tracked.items():
            if account != current_account:
                self.recovery.reconcile(account, profile['added_at'], self.config['multiplier'], runtime_only=True)
                self.publish_events(account, now)

    def step(self, now=None):
        now = time.time() if now is None else now
        recovery = self.db.get('manual_restore_at', 0)
        if recovery > self.last_recovery:
            self.last_recovery = recovery
            self.config['auto_block'] = False
            self.blocked = False
        ident = self.identity_reader(self.config['codex_home'])
        first = self.last_identity is None
        changed = not first and self.scope_key(self.last_identity) != self.scope_key(ident)
        if first or changed:
            block = self.db.get('block_state')
            if first and self.blocked and (not self.config['auto_block'] or not self.is_tracked(ident)
                                          or (block and block.get('account') != ident['account'])):
                self.restore()
            if changed:
                if self.blocked:
                    self.restore()
                if self.last_identity.get('account'):
                    self.ledger.logout(self.config['device_id'])
            saved = self.db.get('scanner_scope') if first else None
            # Resume only a previously verified scope, never infer ownership from
            # the current login alone. A changed config or legacy install drains
            # ambiguous history as before.
            resume = (saved and self.is_tracked(ident) and ident.get('revision') is not None
                      and saved.get('scope') == self.scanner_scope(ident)
                      and isinstance(saved.get('since'), (int, float))
                      and self.scanner.start <= saved['since'] <= now)
            self.db.put('scanner_scope', None)
            if resume:
                self.scanner.scope_since = saved['since']
            else:
                self.recovery.boundary(self.db.get('scope_observed_at'))
                self.scanner.boundary(now)
            self._account(ident, now)
        self.last_identity = ident
        self.db.put('scope_observed_at', now)
        with self.view_lock:
            self.view['identity'] = ident
            self.view['blocked'] = self.blocked
            self.view['error'] = ''
        while not self.commands.empty():
            kind, payload = self.commands.get_nowait()
            if kind == 'restore':
                self.config['auto_block'] = False
                self.restore()
            elif kind == 'cap':
                target = payload.pop('account')
                if target in self.tracked:
                    self.journal.append(target, 'cap', payload, now)
        if not self.is_tracked(ident):
            if self.blocked:
                self.restore()
            self.scanner.scan('')
            if self.tracked:
                self.recovery.scan(min(p['added_at'] for p in self.tracked.values()))
                self.recover_inactive(None, now)
            with self.view_lock:
                self.view.update(summary=None, status=('当前账号未添加：不统计、不查询额度、不连接设备组'
                                 if ident['mode'] == 'account' else 'API / 未登录模式：不统计当前用量，仅核对已绑定旧请求'),
                                 active=0, uncertain=0, peers={}, mesh='未连接', history=None,
                                 pair_scope=False, connection={}, sync_receipts={})
            return
        account = ident['account']
        sync_due = False
        while not self.inbox.empty():
            peer, message = self.inbox.get_nowait()
            try:
                if message.get('account') != account:
                    continue
                if message['type'] == 'sync':
                    vector = message.get('vector', {})
                    if not isinstance(vector, dict):
                        raise ValueError('同步版本向量无效')
                    sync_due |= peer not in self.sync_receipts
                    sync_due |= bool(self.journal.merge(account, message.get('records', [])))
                    if message.get('presence'):
                        self.journal.presence(account, peer, message['presence'], now)
                    if self.mesh:
                        batch = self.journal.since(account, vector)
                        if batch:
                            self.mesh.send(peer, dict(type='facts', account=account, records=batch))
                    self.sync_vectors[peer] = dict(vector)
                elif message['type'] == 'facts':
                    sync_due |= bool(self.journal.merge(account, message['records']))
                elif message['type'] == 'peer_ready':
                    sync_due = True
                elif message['type'] == 'bye':
                    self.ledger.logout(peer)
                if message['type'] in ('sync', 'facts'):
                    self.sync_receipts[peer] = now
            except (ValueError, KeyError, TypeError) as e:
                with self.view_lock:
                    self.view['error'] = '忽略无效同步数据：'+type(e).__name__
        if now-self.last_read >= self.config['interval']:
            self.last_read = now
            try:
                snap = self.quota_reader(self.config['codex_home'])
                if snap['account'] != account or self.scope_key(self.identity_reader(self.config['codex_home'])) != self.scope_key(ident):
                    raise RuntimeError('查询期间账号发生切换，等待下一次确认')
                self.last_snapshot = snap
                self.quota_error = ''
                self.quota_error_since = None
                self.quota_error_notified = False
                self.journal.append(account, 'quota', snap, snap['at'])
            except Exception as e:
                self.quota_error = str(e)
                if self.quota_error_since is None:
                    self.quota_error_since = now
                if now-self.quota_error_since >= 180 and not self.quota_error_notified:
                    self.quota_error_notified = True
                    with self.view_lock:
                        self.view['notifications'].append('连续 3 分钟无法确认官方额度；不会自动清零。请检查网络或 Codex 登录状态。')
        # Recheck after slow I/O, before collecting tokens or restricting a process.
        if self.scope_changed(ident, now):
            return
        checkpoint = self.scanner.checkpoint()
        # An interrupted or identity-racing scan must not authorize restart replay.
        self.db.put('scanner_scope', None)
        self.scanner.scan(account, self.config['multiplier'] * ident['multiplier'])
        recovery_error = ''
        recovered = self.db.get('history_recovery:'+account, {})
        try:
            self.recovery.scan(min(p['added_at'] for p in self.tracked.values()))
            recovered = self.recovery.reconcile(account, self.tracked[account]['added_at'], self.config['multiplier'])
        except Exception as e:
            recovery_error = '历史补记异常：'+type(e).__name__+'；稍后重试'
        if self.scope_changed(ident, now):
            self.scanner.reject_since(checkpoint)
            return
        self.db.put('scanner_scope', dict(scope=self.scanner_scope(ident), since=self.scanner.scope_since))
        active, uncertain = self.scanner.activity(now, account)
        unbound_active, unbound_uncertain = self.scanner.activity(now, '')
        self.publish_events(account, now)
        if not recovery_error:
            try:
                self.recover_inactive(account, now)
            except Exception as e:
                recovery_error = '历史补记异常：'+type(e).__name__+'；稍后重试'
        presence = dict(device=self.config['device_id'], account=account, at=now, scan_at=now,
                        active=active, uncertain=uncertain,
                        unbound_active=unbound_active, unbound_uncertain=unbound_uncertain)
        self.journal.presence(account, self.config['device_id'], presence, now)
        if self.mesh and (sync_due or now-self.last_broadcast >= 5):
            self.last_broadcast = now
            message = dict(type='sync', account=account, records=[], vector=self.journal.vector(account), presence=presence)
            for peer in self.mesh.peer_states():
                self.mesh.send(peer, message)
        summary = self.ledger.summary(account, now)
        self.db.put('last_summary', summary)
        if not recovery_error:
            self.enforce(summary, now)
        if self.scope_changed(ident, now):
            return
        with self.view_lock:
            self.view.update(summary=summary, history=self.ledger.history(account, self.config['device_id'], now),
                recovery=recovered,
                status='监测中 · 所有设备用量均为估算' if self.mesh else '本机监测中 · 尚未配置异地匹配服务',
                active=active, uncertain=uncertain, unbound_active=unbound_active,
                unbound_uncertain=unbound_uncertain, blocked=self.blocked,
                error=' | '.join(value for value in (self.quota_error, recovery_error) if value),
                mesh=self.mesh.status if self.mesh else '点击“生成匹配码”即可自动连接，无需填写参数',
                pair_scope=True, sync_receipts=dict(self.sync_receipts),
                connection=self.mesh.connection_state() if self.mesh and hasattr(self.mesh, 'connection_state') else {},
                peers=self.mesh.peer_states() if self.mesh else {})

    def enforce(self, summary, now):
        if summary.get('account') not in self.tracked:
            if self.blocked:
                self.restore()
            return
        epoch = summary.get('epoch')
        if not epoch:
            return
        fresh = now-epoch['observed_at'] < 120 and not summary['reset_pending']
        block = self.db.get('block_state')
        if block and fresh and (block['account'] != summary['account'] or block['cycle'] != epoch['cycle']):
            self.restore()
        device = next((d for d in summary['devices'] if d['id'] == self.config['device_id']), None)
        if not device:
            return
        reached = device['estimated'] >= device['cap']
        key = (summary['account'], epoch['cycle'], device['cap'])
        if reached and self.notice_key != key:
            self.notice_key = key
            with self.view_lock:
                self.view['notifications'].append(f"本机估算用量 {device['estimated']:.2f}% 已达到配额 {device['cap']:.2f}%")
        if self.blocked:
            if not self.config['auto_block'] or (fresh and block and device['cap'] > block.get('cap', 0) and not reached):
                self.restore()
            else:
                self.firewall.pause(self.config['program_paths'], self.db)
            return
        if self.config['auto_block'] and fresh and device.get('settled', 0) >= device['cap']:
            self.firewall.apply(self.config['program_paths'])
            self.blocked = True
            self.db.put('block_state', dict(account=summary['account'], cycle=epoch['cycle'], cap=device['cap']))
            if self.last_identity and self.scope_changed(self.last_identity, now):
                return
            self.firewall.pause(self.config['program_paths'], self.db)

    def restore(self):
        self.firewall.resume(self.db)
        self.firewall.restore()
        self.blocked = False
        self.db.put('block_state', None)

    def set_cap(self, cap, expected_account=None):
        cap = float(cap)
        if not 0 < cap <= 100:
            raise ValueError('配额必须大于 0 且不超过 100')
        ident = self.last_identity
        if not ident or not self.is_tracked(ident):
            raise ValueError('请先添加并登录需要设置配额的账号')
        if expected_account is not None:
            live = self.identity_reader(self.config['codex_home'])
            if ident['account'] != expected_account or not self.is_tracked(live) or live['account'] != expected_account:
                raise ValueError('登录账号已变化，请关闭弹窗后重新设置，避免修改错误账号。')
        self.commands.put(('cap', dict(account=ident['account'], device=self.config['device_id'], cap=cap)))
        self.wakeup.set()

    def snapshot(self):
        with self.view_lock:
            result = copy.deepcopy(self.view)
            result['auto_block'] = bool(self.config['auto_block'])
            self.view['notifications'] = []
        return result

    def close(self):
        self.stop_event.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=40)
            if self.thread.is_alive():
                raise RuntimeError('后台查询尚未结束，请稍后再试')
