import copy
import hashlib
import json
import queue
import threading
import time

from .firewall import Firewall
from .journal import Journal
from .ledger import Ledger
from .analytics import usage
from .cycle_statistics import cycle_statistics
from .sample_pool import checkpoint_input
from .transport import make_mesh
from .meter import Scanner
from .quota import identity, read_quota
from .recovery import HistoryRecovery
from .storage import Database
from .sync_diagnostics import progress, rejection_reason
from .shared_sync import SharedSync
from .shared_policy import person_for
from .shared_view import shared_usage, shared_overview


class Engine:
    def __init__(self, database, config, quota_reader=None, firewall=None, identity_reader=identity, mesh_factory=make_mesh, account_enroller=None):
        self.db, self.config = database, copy.deepcopy(config)
        self.config['_data_dir'] = str(database.path.parent)
        self.quota_reader = quota_reader or (lambda home: read_quota(home, expected_account=self.last_identity['account']))
        self.identity_reader = identity_reader
        self.account_enroller = account_enroller
        self.mesh_factory = mesh_factory
        self.firewall = firewall or Firewall()
        group = hashlib.sha256(config['group_secret'].encode()).hexdigest()[:20]
        # Legacy journals predate explicit account enrollment; preserve, don't replay them.
        self.group_db = Database(database.path.parent / ('group-v2-'+group+'.sqlite'))
        self.ledger = Ledger(self.group_db)
        self.journal = Journal(self.group_db, self.ledger, config['device_id'])
        self.tracked = config.get('tracked_accounts', {})
        self.shared_mode = config.get('shared_group_enabled', False) is True
        self.shared = SharedSync(self.group_db, self.journal, config['device_id'], config['name'], self.tracked) if self.shared_mode else None
        self.shared_analytics_cache = {}
        self.shared_rules_cache = None
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
        self.sync_errors = {}
        self.sync_error_types = {}
        self.blocked = bool(self.db.get('block_state'))
        self.last_identity, self.last_snapshot = None, None
        self.last_quota_publish = 0
        self.last_read = 0
        self.last_broadcast = 0
        self.thread = None
        self.analytics_cache = {}
        self._background_mode = False
        self._network_limited = False
        self._force_read = False
        self._force_sync = False
        self._background_next_sync = 0
        self._sync_burst_active = False
        self._sync_burst_goals = {}
        self._sync_burst_sent = None
        self._sync_burst_advertised = {}
        self._sync_early_facts = {}
        self._presence_lock = threading.Lock()
        self._pending_presence = {}
        self._last_presence = 0
        self.quota_error = ''
        self.quota_error_since = None
        self.quota_error_notified = False
        self.last_recovery = self.db.get('manual_restore_at', 0)
        if self.shared_mode:
            scope = 'group:'+group
            analytics = shared_usage(self.group_db, scope, {}, time.time())
            initial = shared_overview(self.group_db, scope, {}, {}, config['device_id'],
                                      analytics['at'], analytics)
            self.view.update(initial, display_account=scope, shared_group_enabled=True,
                             analytics=analytics, history=None, pair_scope=True,
                             shared_group=dict(enabled=True, id=group, members=[], state='waiting',
                                 stage='billing' if config.get('shared_billing_v1') else 'preparing',
                                 reason='正在加载共享账本'))

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @property
    def background_mode(self):
        return self._background_mode

    @background_mode.setter
    def background_mode(self, value):
        before = self._background_mode
        self._background_mode = bool(value)
        if before and not self._background_mode:
            self._force_read = self._force_sync = True
            self.wakeup.set()

    def _limited_network(self):
        # Quota protection needs fresh official evidence and peer allocation.
        if self.shared_mode:
            return False
        return self.background_mode and not (self.config['auto_block'] or self.blocked)

    def _network_tick(self, now):
        limited = self._limited_network()
        if limited != self._network_limited:
            self._network_limited = limited
            self._sync_burst_active = False
            self._sync_burst_goals = {}
            self._sync_early_facts = {}
            if limited:
                self._background_next_sync = now + 600
            else:
                self._force_read = self._force_sync = True

    def _begin_sync_round(self, account, now):
        if self._network_limited and now >= self._background_next_sync:
            self._background_next_sync = now + 600
            self._sync_burst_active = True
            self._sync_burst_sent = None
            self._sync_burst_advertised = {}
            self._sync_early_facts = {}
            self._sync_burst_goals = {peer: None for peer in self.mesh.peer_states()} if self.mesh else {}
            self._force_sync = True

    def _end_sync_round(self, account, now):
        if not self._network_limited or not self._sync_burst_active:
            return
        if not self._sync_burst_goals:
            self._sync_burst_active = False
            return
        if self._sync_burst_sent is None:
            return
        local = self.journal.vector(account)
        def covers(vector, target):
            return all(vector.get(origin, 0) >= seq for origin, seq in target.items())
        completed = [peer for peer, target in self._sync_burst_goals.items()
                     if target is not None and covers(local, target)
                     and covers(self.sync_vectors.get(peer, {}), self._sync_burst_sent)]
        for peer in completed:
            self._sync_burst_goals.pop(peer)
        # A hidden peer may only answer at its own (offset) ten-minute window.
        # Waiting keeps no fact backlog and does not periodically resend data.
        if not self._sync_burst_goals:
            self._sync_burst_active = False

    def _round_vector(self, account):
        current = self.journal.vector(account)
        if self._sync_burst_sent is None:
            self._sync_burst_sent = dict(current)
        ceiling = dict(self._sync_burst_sent)
        for target in self._sync_burst_goals.values():
            for origin, seq in (target or {}).items():
                ceiling[origin] = max(ceiling.get(origin, 0), seq)
        return {origin: min(seq, ceiling.get(origin, 0)) for origin, seq in current.items()}

    def receive(self, peer, message):
        if self.shared_mode:
            try:
                self.inbox.put_nowait((peer, message))
                self.wakeup.set()
            except queue.Full:
                pass
            return
        if self._limited_network() and not self._sync_burst_active:
            # The next anti-entropy exchange regenerates unacknowledged facts.
            # Keep only a bounded latest activity heartbeat, never 600 s of pages.
            if message.get('type') in ('sync', 'presence') and message.get('presence'):
                with self._presence_lock:
                    if peer in self._pending_presence or len(self._pending_presence) < 32:
                        self._pending_presence[peer] = dict(type='presence', account=message.get('account'),
                                                            presence=message['presence'])
            return
        try:
            self.inbox.put_nowait((peer, message))
            # A scheduled catch-up round drains all pages without 600 s per page.
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
                self.wakeup.wait(2 if self.blocked or not self.background_mode else 5)
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
        self.sync_errors = {}
        self.sync_error_types = {}
        if self.mesh and not self.shared_mode:
            self.mesh.close()
            self.mesh = None
        self.last_snapshot, self.last_read = None, 0
        self._force_read = self._force_sync = True
        self._background_next_sync = now
        self._sync_burst_active = False
        self._sync_burst_goals = {}
        self._sync_early_facts = {}
        with self._presence_lock:
            self._pending_presence.clear()
        self.analytics_cache = {}
        self.last_quota_publish = 0
        self.quota_error = ''
        self.quota_error_since = None
        self.quota_error_notified = False
        if self.is_tracked(ident):
            account = ident['account']
            self.journal.project(account)
            profile = dict(device=self.config['device_id'], name=self.config['name'],
                           cap=self.tracked[account].get('cap', self.config['quota']))
            previous_profile = self.group_db.get('own_profile:'+account, {})
            with self.group_db.connect() as db:
                epoch = db.execute('SELECT started FROM epochs WHERE account=? ORDER BY started DESC LIMIT 1', (account,)).fetchone()
            profile['fairness_start'] = previous_profile.get('fairness_start',
                self.group_db.get('statistics_start:'+account, epoch['started'] if epoch else now))
            exemption = self.config.get('no_debt_cycle_by_account', {}).get(account)
            if exemption:
                profile['no_debt_cycle'] = dict(exemption)
            if previous_profile != profile:
                self.journal.append(account, 'profile', profile, now)
                self.group_db.put('own_profile:'+account, profile)
            if not self.shared_mode and (self.config.get('link_enabled') or self.config['rendezvous_url']):
                self.mesh = self.mesh_factory(self.config, account, self.receive)
                self.mesh.start()
            mark = hashlib.sha256(self.config['group_secret'].encode()).hexdigest()
            if self.db.get('published_group:'+account) != mark:
                with self.db.connect() as db:
                    db.execute("UPDATE outbox SET sent=0 WHERE sent<>2 AND json_extract(payload,'$.account')=? AND json_extract(payload,'$.ts')>?",
                               (account, self.tracked[account]['added_at']))
                self.db.put('published_group:'+account, mark)

        self._ensure_shared_mesh()

    def _ensure_shared_mesh(self):
        if self.shared_mode and self.mesh is None and (self.config.get('link_enabled') or self.config.get('rendezvous_url')):
            group = hashlib.sha256(self.config['group_secret'].encode()).hexdigest()[:20]
            self.mesh = self.mesh_factory(self.config, 'group:'+group, self.receive)
            self.mesh.start()

    def _receive_shared(self, now):
        if not self.shared:
            return
        self._ensure_shared_mesh()
        while not self.inbox.empty():
            peer, message = self.inbox.get_nowait()
            self.shared.receive(peer, message, self.mesh, now)

    def _update_shared_view(self, now, presence=None, observed=None):
        if not self.shared:
            return
        self.shared.tick(self.mesh, now, presence=presence)
        state = self.shared.snapshot(self.mesh, now)
        group = hashlib.sha256(self.config['group_secret'].encode()).hexdigest()[:20]
        scope = 'group:'+group
        labels = state.get('account_labels', {})
        rules = None
        if self.config.get('shared_billing_v1'):
            rules = self._billing_rules(labels, now)
            if rules.get('policy'):
                labels = {a: '账号'+str(i+1) for i, a in enumerate(rules['accounts'])}
        analytics = self.shared_analytics_cache
        if (not analytics or now-analytics['at'] >= 10
                or analytics.get('account_ids') != sorted(labels) or self.shared_rules_cache != rules):
            analytics = shared_usage(self.group_db, scope, labels, now, rules=rules)
        overview = shared_overview(self.group_db, scope, labels, state.get('members', {}),
                                   self.config['device_id'], now, analytics,
                                   removed=self.mesh.removed_devices() if self.mesh and hasattr(self.mesh, 'removed_devices') else ())
        members = overview.pop('members', [])
        billing = overview.get('billing', {})
        if rules and rules.get('policy') and len(billing.get('anchors', {})) < 2:
            self.last_quota_publish = 0
        group_view = dict(enabled=True, id=group, stage='preparing', members=members,
            billing_start_note='原两人账号指定周期免结转；后续周期按新规则处理。统一计费待启用。')
        if self.config.get('shared_billing_v1'):
            policy = (rules or {}).get('policy') or {}
            group_view.update(stage='billing', state=billing.get('status', rules['status']),
                reason=billing.get('reason', rules['reason']), revision=policy.get('revision'),
                admin=policy.get('admin'), can_manage=policy.get('admin') == self.config['device_id'],
                rules_locked=True,
                maintenance_enabled=rules.get('maintenance', {}).get(person_for(rules, self.config['device_id'], now), {}).get('enabled', False),
                pending_rule=bool(self.group_db.get('shared:pending_rule')),
                available_accounts=[dict(account=a, label=label) for a,label in state.get('account_labels', {}).items()],
                bindings=(rules or {}).get('bindings', []),
                devices=[dict(id=d, name=p.get('name', d), version=state.get('peer_versions', {}).get(d,'')) for d,p in state.get('members', {}).items()],
                billing_start_note='个人百分比固定以100/3额度点为100%；两个账号独立刷新，新份额只在官方实际刷新后发放，未用份额随对应账号到期。')
        # The bridge may poll during disk/network work. Publish one complete
        # shared projection, never an intermediate legacy-account snapshot.
        activity = presence or {}
        published = dict(observed or {})
        published.update(overview)
        published.update(display_account=scope, shared_group_enabled=True,
                shared_group=group_view, identity=self.last_identity or {},
                error=(observed or {}).get('error', ''), history=None,
                active=activity.get('active', 0), uncertain=activity.get('uncertain', 0),
                unbound_active=activity.get('unbound_active', 0),
                unbound_uncertain=activity.get('unbound_uncertain', 0),
                analytics=analytics, blocked=False, auto_block=False,
                status=('共享额度 · '+(group_view.get('reason') or '官方增量分摊')) if self.config.get('shared_billing_v1') else '共享组入组过渡版 · Token 合并统计，统一额度计费待启用',
                pair_scope=True, peers=self.mesh.peer_states() if self.mesh else {},
                connection=self.mesh.connection_state() if self.mesh and hasattr(self.mesh, 'connection_state') else {},
                mesh=self.mesh.status if self.mesh else '请生成或输入匹配码以加入共享组',
                sync_receipts=state.get('sync_receipts', {}), sync_progress=state.get('sync_progress', {}),
                sync_errors=state.get('sync_errors', {}))
        with self.view_lock:
            self.shared_analytics_cache = analytics
            self.shared_rules_cache = copy.deepcopy(rules)
            self.view.update(published)

    def _billing_rules(self, labels, now):
        from .shared_policy import load_rules, genesis, profile, person_for, publish_change
        rules = load_rules(self.group_db, labels, now)
        if rules['reason'] == '等待共享组管理员规则' and self.config.get('shared_group_admin') == self.config['device_id'] and self.tracked:
            account = next(iter(self.tracked))
            devices = self.config.get('shared_initial_devices', [self.config['device_id']])
            policy = genesis(self.config['device_id'], account, devices, now)
            self.journal.append(account, 'profile', profile(self.config, account, group_policy=policy), now)
            rules = load_rules(self.group_db, labels, now)
        policy = rules.get('policy') or {}
        if policy.get('admin') == self.config['device_id'] and len(policy['accounts']) == 1 and len(rules['accounts']) == 2:
            publish_change(self.journal, rules, self.config['device_id'], self.config['name'], self.config['quota'],
                           dict(accounts=rules['accounts']), now)
            rules = load_rules(self.group_db, labels, now)
        policy = rules.get('policy') or {}
        if policy.get('admin') == self.config['device_id']:
            changes = {}
            if not policy.get('rules_locked'):
                changes.update(compensation=True, rules_locked=True)
            clean = self.config.get('shared_clean_start')
            if clean and not policy.get('clean_start'):
                changes['clean_start'] = clean
                baseline = clean['baseline']
                overrides = [r for r in policy.get('reset_types', [])
                             if (r['account'],r['started']) != (baseline['account'],baseline['started'])]
                changes['reset_types'] = overrides+[dict(account=baseline['account'], started=baseline['started'], type='natural')]
            if changes:
                publish_change(self.journal, rules, self.config['device_id'], self.config['name'], self.config['quota'], changes, now)
                self.group_db.put('shared:pending_rule', None)
                rules = load_rules(self.group_db, labels, now)
        current = self.last_identity or {}
        if rules.get('policy') and self.is_tracked(current) and not person_for(rules, self.config['device_id'], now):
            occupied = {b['person'] for b in rules.get('bindings', [])}
            empty = [p for p in ('person1', 'person2', 'person3') if p not in occupied]
            claim_key = 'shared:claimed:'+rules['genesis']
            if len(empty) == 1 and not self.group_db.get(claim_key):
                claim = dict(device=self.config['device_id'], person=empty[0], genesis=rules['genesis'])
                self.journal.append(current['account'], 'profile', profile(self.config, current['account'], member_claim=claim), now)
                self.group_db.put(claim_key, True)
                rules = load_rules(self.group_db, labels, now)
        pending = self.group_db.get('shared:pending_rule')
        if pending and self._group_rule_ready(rules, now):
            self.group_db.put('shared:pending_rule', None)
            try:
                self._apply_group_rule(pending, rules, now)
            except ValueError:
                with self.view_lock:
                    self.view['notifications'].append('共同规则已变化，待同步的补偿设置请重新提交。')
            rules = load_rules(self.group_db, labels, now)
        return rules

    def _group_rule_ready(self, rules, now=None):
        from .shared_policy import person_for
        now = time.time() if now is None else now
        if not self.mesh or rules.get('status') != 'ready':
            return False
        peers = self.shared.snapshot(self.mesh, now)
        represented = {person_for(rules, self.config['device_id'], now)}
        represented.update(person_for(rules, peer, now) for peer, value in peers['sync_progress'].items()
                           if value['state'] == 'caught_up' and tuple(int(v) for v in peers.get('peer_versions', {}).get(peer, '0.0.0').split('.') if v.isdigit()) >= (0,6,0))
        return represented >= {'person1', 'person2', 'person3'}

    def _apply_group_rule(self, payload, rules, now):
        from .shared_policy import publish_change
        if payload.get('revision') != rules.get('policy', {}).get('revision'):
            raise ValueError('共同规则已变化，请重新提交')
        changes = {}
        if payload.get('kind') == 'compensation':
            raise ValueError('跨周期补偿已固定开启，不能关闭')
        elif payload.get('kind') == 'bind':
            if payload.get('person') not in ('person1','person2','person3'):
                raise ValueError('成员身份无效')
            if payload.get('device') not in self.shared.directory:
                raise ValueError('设备尚未加入共享组')
            bindings = list(rules['policy']['bindings'])
            since = now if any(b['device'] == payload['device'] for b in bindings) else 0
            changes['bindings'] = bindings+[dict(device=payload['device'], person=payload['person'], since=since)]
        elif payload.get('kind') == 'accounts':
            accounts = payload.get('accounts', [])
            if len(accounts) != 2 or any(a not in self.shared.snapshot(self.mesh, now)['account_labels'] for a in accounts):
                raise ValueError('请选择两个已加入的账号')
            if accounts[:len(rules['policy']['accounts'])] != rules['policy']['accounts']:
                raise ValueError('不能替换已生效的共享账号')
            changes['accounts'] = accounts
        elif payload.get('kind') == 'reset':
            account, started, cause = payload.get('account'), payload.get('started'), payload.get('cause')
            if account not in rules['accounts'] or cause not in ('natural', 'card', 'official'):
                raise ValueError('请选择共享账号和重置原因')
            with self.group_db.connect() as db:
                exists = db.execute('SELECT 1 FROM epochs WHERE account=? AND started=? AND started<=?', (account, started, now)).fetchone()
            if not exists:
                raise ValueError('周期已变化，请重新选择')
            changes['reset_types'] = [r for r in rules['policy'].get('reset_types', []) if (r['account'],r['started']) != (account,started)]
            changes['reset_types'].append(dict(account=account, started=started, type=cause))
        elif payload.get('kind') == 'availability':
            account, paused = payload.get('account'), payload.get('paused')
            if account not in rules['accounts'] or type(paused) is not bool:
                raise ValueError('请选择共享账号和暂停状态')
            current = set(rules['policy'].get('paused_accounts', []))
            if (account in current) == paused:
                return
            current.add(account) if paused else current.discard(account)
            changes['paused_accounts'] = [a for a in rules['accounts'] if a in current]
        else:
            raise ValueError('未知共享规则操作')
        publish_change(self.journal, rules, self.config['device_id'], self.config['name'], self.config['quota'], changes, now)
        self.shared_analytics_cache = {}

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
        if self.mesh and not self.shared_mode:
            self.mesh.close()
            self.mesh = None
        self.ledger.logout(self.config['device_id'])
        with self.view_lock:
            if self.shared_mode:
                self.view.update(blocked=False, status='登录配置已变化，正在确认；共享账本保留')
            else:
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

    def publish_sample_checkpoint(self, account, now):
        if not self.recovery.complete or self.scanner.pending(account=account, limit=1):
            return
        with self.group_db.connect() as db:
            sample = checkpoint_input(db, account, self.config['device_id'], now-120)
        if sample is None or sample['through'] > now-120:
            return
        key = 'published_sample_checkpoint:'+account
        if self.group_db.get(key) == sample:
            return
        profile = dict(device=self.config['device_id'], name=self.config['name'],
                       cap=self.tracked[account].get('cap', self.config['quota']),
                       sample_checkpoint=dict(through=sample['through']))
        self.journal.append(account, 'profile', profile, now)
        self.group_db.put(key, sample)

    def recover_inactive(self, current_account, now):
        # These are old tracked-account requests, not current API usage. Publish
        # locally only; inactive account groups never use the current connection.
        for account, profile in self.tracked.items():
            if account != current_account:
                self.recovery.reconcile(account, profile['added_at'], self.config['multiplier'], runtime_only=True)
                self.publish_events(account, now)
                if self.shared_mode:
                    self.publish_sample_checkpoint(account, now)

    def step(self, now=None):
        now = time.time() if now is None else now
        self._network_tick(now)
        recovery = self.db.get('manual_restore_at', 0)
        if recovery > self.last_recovery:
            self.last_recovery = recovery
            self.config['auto_block'] = False
            self.blocked = False
        ident = self.identity_reader(self.config['codex_home'])
        enrolled = bool(self.account_enroller and not self.is_tracked(ident) and self.account_enroller(ident, now))
        first = self.last_identity is None
        changed = not first and self.scope_key(self.last_identity) != self.scope_key(ident)
        if first or changed or enrolled:
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
        if not self.shared_mode:
            with self.view_lock:
                self.view['identity'] = ident
                self.view['blocked'] = self.blocked
                self.view['error'] = ''
        while not self.commands.empty():
            kind, payload = self.commands.get_nowait()
            if kind == 'restore':
                self.config['auto_block'] = False
                self.restore()
            elif kind == 'cap' and not self.shared_mode:
                target = payload.pop('account')
                if target in self.tracked:
                    self.journal.append(target, 'cap', payload, now)
            elif kind == 'compensation' and not self.shared_mode:
                target = payload['account']
                if target in self.tracked:
                    profile = dict(device=self.config['device_id'], name=self.config['name'],
                                   cap=self.tracked[target].get('cap', self.config['quota']),
                                   compensation_enabled=payload['enabled'])
                    self.journal.append(target, 'profile', profile, now)
            elif kind == 'maintenance' and self.shared and self.config.get('shared_billing_v1'):
                from .shared_policy import load_rules
                from .maintenance import publish
                rules = load_rules(self.group_db, self.shared.snapshot(self.mesh, now)['account_labels'], now)
                publish(self.journal, rules, self.config, payload['enabled'], payload['at'])
                self.shared_analytics_cache = None
            elif kind == 'group_rule' and self.shared and self.config.get('shared_billing_v1'):
                from .shared_policy import load_rules
                rules = load_rules(self.group_db, self.shared.snapshot(self.mesh, now)['account_labels'], now)
                if payload.get('kind') == 'compensation' and not self._group_rule_ready(rules, now):
                    self.group_db.put('shared:pending_rule', payload)
                else:
                    self._apply_group_rule(payload, rules, now)
            elif kind == 'remove_device':
                if (self.mesh and (self.shared_mode or payload['account'] == ident.get('account'))
                        and payload['device'] != self.config['device_id']):
                    known = ({d['id'] for d in (self.view.get('summary') or {}).get('devices', [])} if self.shared_mode else
                             {d['id'] for d in self.ledger.summary(payload['account'], now)['devices']})
                    if self.config.get('shared_billing_v1') and self.shared:
                        known = set(self.shared.directory)
                    if payload['device'] in known:
                        self.mesh.remove_device(payload['device'])
        self._receive_shared(now)
        if not self.is_tracked(ident):
            if self.blocked:
                self.restore()
            self.scanner.scan('')
            if self.tracked:
                self.recovery.scan(min(p['added_at'] for p in self.tracked.values()))
                self.recover_inactive(None, now)
            if self.shared_mode:
                self._update_shared_view(now, observed=dict(tracked_since=None))
            else:
                with self.view_lock:
                    self.view.update(summary=None, status=('当前账号未添加：不统计、不查询额度、不连接设备组'
                                     if ident['mode'] == 'account' else 'API / 未登录模式：不统计当前用量，仅核对已绑定旧请求'),
                                     active=0, uncertain=0, peers={}, mesh='未连接', history=None, analytics=None,
                                     pair_scope=False, connection={}, sync_receipts={}, sync_progress={},
                                     sync_vectors={}, local_vector={}, sync_errors={}, tracked_since=None)
            return
        account = ident['account']
        self._begin_sync_round(account, now)
        network_allowed = not self._network_limited or self._sync_burst_active
        sync_due = self._force_sync
        self._force_sync = False
        incoming = []
        if not self.shared_mode:
            with self._presence_lock:
                incoming, self._pending_presence = list(self._pending_presence.items()), {}
            while not self.inbox.empty():
                incoming.append(self.inbox.get_nowait())
        for peer, message in incoming:
            try:
                if message.get('account') != account:
                    continue
                removed = self.mesh.removed_devices() if self.mesh and hasattr(self.mesh, 'removed_devices') else set()
                if peer in removed or self.config['device_id'] in removed:
                    continue
                if message.get('type') == 'presence' or not network_allowed:
                    if message.get('presence'):
                        self.journal.presence(account, peer, message['presence'], now)
                    continue
                if self._network_limited and peer not in self._sync_burst_goals:
                    # An already completed peer cannot extend this round forever.
                    continue
                if message['type'] == 'sync':
                    vector = message.get('vector', {})
                    if (not isinstance(vector, dict) or any(
                            not isinstance(k, str) or not 1 <= len(k) <= 100
                            or type(v) is not int or not 0 <= v <= 1e9 for k, v in vector.items())):
                        raise ValueError('同步版本向量无效')
                    if self._network_limited:
                        first = self._sync_burst_goals[peer] is None
                        if self._sync_burst_goals[peer] is None:
                            self._sync_burst_goals[peer] = dict(vector)
                        target = self._sync_burst_goals[peer]
                        acknowledged = any(min(seq, self._sync_burst_advertised.get(origin, 0)) >
                            self.sync_vectors.get(peer, {}).get(origin, 0) for origin, seq in vector.items())
                        if not first and not acknowledged:
                            if message.get('presence'):
                                self.journal.presence(account, peer, message['presence'], now)
                            continue
                        sync_due |= first
                    sync_due |= peer not in self.sync_receipts
                    records = message.get('records', [])
                    if self._network_limited:
                        early = self._sync_early_facts.pop(peer, [])
                        early = [r for r in early if r['seq'] <= target.get(r['origin'], 0)]
                        sync_due |= bool(self.journal.merge(account, early,
                            limit=getattr(self.mesh, 'history_limit', 60)))
                        records = [r for r in records if r['seq'] <= target.get(r['origin'], 0)]
                    merged = bool(self.journal.merge(account, records))
                    sync_due |= merged
                    if message.get('presence'):
                        self.journal.presence(account, peer, message['presence'], now)
                    if self.mesh:
                        request_vector = dict(vector)
                        if self._network_limited:
                            ceiling = self._round_vector(account)
                            for origin, seq in self.journal.vector(account).items():
                                if vector.get(origin, 0) >= ceiling.get(origin, 0):
                                    request_vector[origin] = seq
                        batch = self.journal.since(account, request_vector,
                            limit=getattr(self.mesh, 'history_limit', 60),
                            byte_limit=getattr(self.mesh, 'history_bytes', 24000))
                        if self._network_limited:
                            batch = [r for r in batch if r['seq'] <= ceiling.get(r['origin'], 0)]
                        if batch:
                            self.mesh.send(peer, dict(type='facts', account=account, records=batch))
                    self.sync_vectors[peer] = dict(vector)
                elif message['type'] == 'facts':
                    records = message['records']
                    if self._network_limited:
                        target = self._sync_burst_goals[peer]
                        if target is None:
                            if len(records) <= getattr(self.mesh, 'history_limit', 60):
                                self._sync_early_facts[peer] = records
                            continue
                        records = [r for r in records if r['seq'] <= target.get(r['origin'], 0)]
                    merged = bool(self.journal.merge(account, records,
                        limit=getattr(self.mesh, 'history_limit', 60)))
                    sync_due |= merged
                elif message['type'] == 'peer_ready':
                    sync_due = True
                elif message['type'] == 'bye':
                    self.ledger.logout(peer)
                if message['type'] in ('sync', 'facts'):
                    self.sync_receipts[peer] = now
                    if (self.sync_error_types.get(peer) == message['type']
                            and (message['type'] == 'sync' or message['records'])):
                        self.sync_errors.pop(peer, None)
                        self.sync_error_types.pop(peer, None)
            except (ValueError, KeyError, TypeError) as e:
                self.sync_errors[peer] = rejection_reason(e)
                self.sync_error_types[peer] = message.get('type')
        reset_pending = bool(self.group_db.get('reset_candidate:'+account))
        query_interval = (min(30, self.config['interval']) if reset_pending else
                          600 if self._network_limited or (self.shared_mode and self.background_mode) else self.config['interval'])
        if self._force_read or now-self.last_read >= query_interval:
            self._force_read = False
            self.last_read = now
            try:
                snap = self.quota_reader(self.config['codex_home'])
                if snap['account'] != account or self.scope_key(self.identity_reader(self.config['codex_home'])) != self.scope_key(ident):
                    raise RuntimeError('查询期间账号发生切换，等待下一次确认')
                previous = self.last_snapshot
                self.last_snapshot = snap
                self.quota_error = ''
                self.quota_error_since = None
                self.quota_error_notified = False
                changed = (not previous or previous['used'] != snap['used']
                           or previous['reset_at'] != snap['reset_at'])
                if changed or snap['at']-self.last_quota_publish >= 600:
                    self.journal.append(account, 'quota', snap, snap['at'])
                    self.last_quota_publish = snap['at']
                else:
                    # Keep local freshness without filling replicated history
                    # with an identical official snapshot every polling cycle.
                    self.ledger.observe(snap)
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
        active_models = self.scanner.active_models(now, account)
        for model in self.scanner.active_models(now, ''):
            if model not in active_models:
                active_models.append(model)
        self.publish_events(account, now)
        if not recovery_error:
            self.publish_sample_checkpoint(account, now)
            try:
                self.recover_inactive(account, now)
            except Exception as e:
                recovery_error = '历史补记异常：'+type(e).__name__+'；稍后重试'
        presence = dict(device=self.config['device_id'], account=account, at=now, scan_at=now,
                        active=active, uncertain=uncertain, active_models=active_models[:16],
                        unbound_active=unbound_active, unbound_uncertain=unbound_uncertain)
        self.journal.presence(account, self.config['device_id'], presence, now)
        if not self.shared_mode and self.mesh and network_allowed and (sync_due or (not self._network_limited and now-self.last_broadcast >= 2)):
            self.last_broadcast = now
            vector = self._round_vector(account) if self._network_limited else self.journal.vector(account)
            self._sync_burst_advertised = vector
            message = dict(type='sync', account=account, records=[], vector=vector, presence=presence)
            for peer in self.mesh.peer_states():
                if self._network_limited and peer not in self._sync_burst_goals:
                    continue
                self.mesh.send(peer, message)
        elif self.mesh and self._network_limited and now-self._last_presence >= 20:
            self._last_presence = now
            for peer in self.mesh.peer_states():
                self.mesh.send(peer, dict(type='presence', account=account, presence=presence))
        self._end_sync_round(account, now)
        removed = self.mesh.removed_devices() if self.mesh and hasattr(self.mesh, 'removed_devices') else set()
        summary = self.ledger.summary(account, now, removed=removed)
        self.db.put('last_summary', summary)
        if not recovery_error:
            self.enforce(summary, now)
        if self.scope_changed(ident, now):
            return
        peers = self.mesh.peer_states() if self.mesh else {}
        local_vector = self.journal.vector(account)
        if not self.shared_mode and (not self.analytics_cache or now-self.analytics_cache['at'] >= 10
                or self.analytics_cache['account'] != account
                or self.analytics_cache.get('cycle_start') != (summary.get('epoch') or {}).get('started')):
            self.analytics_cache = usage(self.group_db, account, now)
            self.analytics_cache['cycles'] = cycle_statistics(self.group_db, account, now)['rows']
        sync_errors = {p: error for p, error in self.sync_errors.items() if p not in removed}
        if self.shared_mode:
            self._update_shared_view(now, presence, observed=dict(recovery=recovered,
                tracked_since=self.tracked[account]['added_at'],
                error=' | '.join(value for value in (self.quota_error, recovery_error) if value)))
        else:
            with self.view_lock:
                self.view.update(summary=summary, analytics=self.analytics_cache, history=self.ledger.history(account, self.config['device_id'], now),
                    recovery=recovered,
                    tracked_since=self.tracked[account]['added_at'],
                    status='监测中 · Token 按日志统计，额度随官方更新校准' if self.mesh else '本机监测中 · 尚未配置异地匹配服务',
                    active=active, uncertain=uncertain, unbound_active=unbound_active,
                    unbound_uncertain=unbound_uncertain, blocked=self.blocked,
                    error=' | '.join(value for value in (self.quota_error, recovery_error,
                        '同步记录被拒绝：'+', '.join(sorted(set(sync_errors.values()))) if sync_errors else '') if value),
                    mesh=self.mesh.status if self.mesh else '点击“生成匹配码”即可自动连接，无需填写参数',
                    pair_scope=True, sync_receipts=dict(self.sync_receipts),
                    sync_progress=progress(local_vector, peers, self.sync_vectors, self.sync_receipts, sync_errors, now),
                    local_vector=local_vector, sync_vectors=copy.deepcopy(self.sync_vectors), sync_errors=sync_errors,
                    connection=self.mesh.connection_state() if self.mesh and hasattr(self.mesh, 'connection_state') else {},
                    peers=peers)

    def enforce(self, summary, now):
        if self.shared_mode:
            if self.blocked:
                self.restore()
            return
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
        cap = device.get('fair_cap', device['cap'])
        reached = device['estimated'] >= cap
        if self.blocked:
            reallocated = summary.get('allocation') == 'cycle_weighted_v1' and summary.get('unassigned') == 0
            if not self.config['auto_block'] or (fresh and block and not reached
                    and (cap > block.get('cap', 0) or reallocated)):
                self.restore()
            else:
                self.firewall.pause(self.config['program_paths'], self.db)
            return
        if self.config['auto_block'] and fresh and device.get('settled', 0) >= cap:
            self.firewall.apply(self.config['program_paths'])
            self.blocked = True
            self.db.put('block_state', dict(account=summary['account'], cycle=epoch['cycle'], cap=cap))
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
            result['auto_block'] = not self.shared_mode and bool(self.config['auto_block'])
            self.view['notifications'] = []
        return result

    def close(self):
        self.stop_event.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=40)
            if self.thread.is_alive():
                raise RuntimeError('后台查询尚未结束，请稍后再试')
