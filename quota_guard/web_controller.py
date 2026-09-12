"""Allowlisted local WebView bridge; the existing Engine owns all accounting."""
import copy
import hashlib
import json
import math
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from . import __version__, startup
from .accounts import enroll
from .engine import Engine
from .firewall import discover_programs, is_admin
from .pairing import create_code, read_code, save_config, validate_url
from .quota import identity
from .sync_diagnostics import report


_COLORS = ('#669cff', '#f6b763', '#55d6be', '#cd8af0', '#ed8299', '#c6db76', '#f39777', '#62cde2')
_SETTINGS = ('device_id', 'name', 'theme', 'quota_display', 'device_notes', 'device_colors', 'device_order',
             'autostart', 'auto_update', 'auto_block', 'codex_home', 'interval',
             'program_paths', 'force_relay', 'quota', 'multiplier')
_EDITABLE = {'name', 'theme', 'quota_display', 'autostart', 'auto_update', 'auto_block',
             'codex_home', 'interval', 'program_paths', 'quota', 'multiplier'}


def _pick(value, keys):
    return {key: copy.deepcopy(value[key]) for key in keys if key in value} if isinstance(value, dict) else {}


def _identity(value):
    return _pick(value, ('mode', 'account', 'label', 'plan', 'multiplier'))


def _summary(value):
    if not value:
        return None
    result = _pick(value, ('account', 'unassigned', 'provisional', 'allocation', 'reset_pending', 'server_time', 'compensation_enabled', 'billing_status', 'billing_reason'))
    result['token_budget'] = _pick(value.get('token_budget'), ('sampled_tokens', 'used_tokens', 'total_tokens', 'source', 'sample_devices', 'sample_ready', 'sample_segments', 'sample_until', 'sample_tokens', 'sample_percent'))
    result['epoch'] = _pick(value.get('epoch'), ('id', 'account', 'started', 'ended', 'baseline', 'used',
        'reset_at', 'observed_at', 'reason', 'cycle')) or None
    result['devices'] = [_pick(d, ('id', 'name', 'cap', 'seen', 'scan_at', 'active', 'uncertain', 'logged_in',
        'unbound_active', 'unbound_uncertain', 'estimated', 'settled', 'carry', 'fair_cap', 'fair_base_cap', 'tokens', 'weight', 'unknown_tokens', 'online', 'removed', 'quota_pending', 'avatar', 'device_ids', 'local', 'joined', 'available', 'rollover', 'available_cap', 'debt', 'pending_debt', 'confirmed_debt', 'by_account', 'fair_usage', 'active_model', 'active_models', 'confirmed_available', 'confirmed_available_cap', 'available_estimate', 'balance_estimated', 'estimate_missing', 'estimate_pending', 'estimate_at'))
        for d in value.get('devices', [])]
    result['attribution_gaps'] = [_pick(d, ('start', 'end', 'delta', 'reason', 'devices', 'unknown_models'))
                                  for d in value.get('attribution_gaps', [])]
    return result


def _view(value):
    result = _pick(value, ('status', 'blocked', 'active', 'uncertain', 'unbound_active',
                          'unbound_uncertain', 'auto_block', 'tracked_since', 'pair_scope', 'display_account', 'shared_group_enabled'))
    # Transport exceptions can contain server payloads or URLs; do not export them.
    result['error'] = '监测异常，请查看同步诊断并检查登录状态' if value.get('error') else ''
    result['identity'] = _identity(value.get('identity'))
    result['summary'] = _summary(value.get('summary'))
    shared = value.get('shared_group') or {}
    if shared:
        result['shared_group'] = _pick(shared, ('enabled', 'id', 'stage', 'billing_start_note', 'state', 'reason', 'revision', 'admin', 'can_manage', 'pending_rule', 'rules_locked', 'maintenance_enabled'))
        result['shared_group']['bindings'] = [_pick(row, ('device','person','since')) for row in shared.get('bindings', [])]
        result['shared_group']['devices'] = [_pick(row, ('id','name','version')) for row in shared.get('devices', [])]
        result['shared_group']['available_accounts'] = [_pick(row, ('account','label')) for row in shared.get('available_accounts', [])]
        result['shared_group']['members'] = [_pick(member, ('id', 'name', 'local', 'online', 'accounts', 'current_account'))
                                             for member in shared.get('members', [])]
    result['account_summaries'] = []
    for card in value.get('account_summaries', []):
        clean = _pick(card, ('account', 'label', 'paused', 'reset_pending', 'used_estimate', 'remaining_estimate',
                             'balance_estimated', 'estimate_pending', 'estimate_missing', 'estimate_samples', 'estimate_at'))
        clean['epoch'] = _pick(card.get('epoch'), ('id', 'account', 'started', 'ended', 'baseline', 'used', 'reset_at', 'observed_at', 'reason')) or None
        result['account_summaries'].append(clean)
    result['connection'] = _pick(value.get('connection'), ('state', 'ready', 'tailnet', 'ips', 'connected',
                                                         'phase', 'transport', 'checked_at', 'tailnet_peer_count'))
    result['peers'] = {p: _pick(v, ('route', 'route_at', 'at', 'scan_at', 'active', 'uncertain', 'name'))
                       for p, v in value.get('peers', {}).items()}
    result['sync_progress'] = {p: _pick(v, ('state', 'send', 'receive'))
                               for p, v in value.get('sync_progress', {}).items()}
    result['sync_receipts'] = {p: v for p, v in value.get('sync_receipts', {}).items()
                               if isinstance(v, (int, float)) and math.isfinite(v)}
    states = [v.get('state') for v in result['sync_progress'].values()]
    result['sync_caption'] = ('同步异常' if 'error' in states else '同步中' if 'syncing' in states else
        '等待确认' if any(s in ('waiting', 'stale') for s in states) else
        '已同步' if states and all(s == 'caught_up' for s in states) else
        '等待确认' if result['peers'] else '未连接')
    receipts = result['sync_receipts']
    result['sync_confirmed_at'] = (min(receipts[p] for p in result['sync_progress'])
        if states and all(s == 'caught_up' for s in states) and all(receipts.get(p) for p in result['sync_progress']) else None)
    analytics = value.get('analytics') or {}
    result['analytics'] = _pick(analytics, ('account', 'at', 'models', 'cycle_start', 'statistics_start',
        'quota_unavailable', 'donut_archive_at', 'quota_estimate', 'account_estimates'))
    if analytics.get('live_reporting'):
        result['analytics']['personal_daily'] = analytics['live_reporting']['daily']
    if analytics.get('donut_windows'):
        result['analytics']['donut_windows'] = _view({'analytics': {'windows': analytics['donut_windows']}})['analytics']['windows']
    if analytics.get('cycle_pair'):
        pair = analytics['cycle_pair']
        result['analytics']['cycle_pair'] = _pick(pair, ('status', 'complete', 'reason', 'overlap_seconds'))
        result['analytics']['cycle_pair']['cycles'] = [_pick(cycle,
            ('account', 'label', 'started', 'ended', 'reset_at', 'usage_until')) for cycle in pair.get('cycles', [])]
    result['analytics']['cycles'] = []
    for row in analytics.get('cycles', []):
        safe = _pick(row, ('id', 'account', 'account_label', 'started', 'ended', 'reset_at', 'reset_type', 'used_percent', 'baseline_percent',
            'sampled_tokens', 'total_tokens', 'source', 'sample_tokens', 'sample_percent', 'sample_devices', 'sample_ready', 'sample_segments', 'sample_until', 'is_current', 'change_percent', 'change_tokens', 'reference_count', 'reference_total_tokens',
            'reference_starts', 'reduction_tokens', 'reduction_percent'))
        safe['models'] = [_pick(m, ('model', 'tokens')) for m in row.get('models', [])]
        result['analytics']['cycles'].append(safe)
    result['analytics']['windows'] = {}
    if value.get('daily_usage'):
        result['daily_usage'] = _pick(value['daily_usage'], ('total','people','unassigned','basis','account_count'))
    if value.get('billing'):
        result['billing'] = _pick(value['billing'], ('status','reason','people','anchors','active_since','compensation_enabled','entries','clean_start','last_confirmed'))
    for key in ('cycle', 'total', 'today', 'pie_hour', 'pie_six_hours', 'pie_twelve_hours', 'hour', 'hour_curve', 'six_hours', 'twelve_hours', 'day', 'week', 'month'):
        source = analytics.get('windows', {}).get(key)
        if source:
            window = _pick(source, ('start', 'step', 'count', 'quota_ready', 'quota_available'))
            if 'quota_gaps' in source:
                window['quota_gaps'] = [_pick(r, ('start','end')) for r in source['quota_gaps']]
            window['quota_pending_rows'] = [_pick(r, ('account', 'device', 'model', 'bucket', 'maintenance')) for r in source.get('quota_pending_rows', [])]
            window['quota_estimate_rows'] = [_pick(r, ('account', 'device', 'model', 'bucket', 'quota', 'cache_quota', 'shared_quota', 'maintenance')) for r in source.get('quota_estimate_rows', [])]
            window['quota_rows'] = [_pick(r, ('account', 'device', 'model', 'bucket', 'quota', 'cache_quota', 'shared_quota', 'maintenance')) for r in source.get('quota_rows', [])]
            window['rows'] = [_pick(r, ('account', 'device', 'model', 'bucket', 'tokens', 'weight', 'unknown', 'cache_tokens', 'detail_missing', 'input_tokens', 'output_tokens', 'reasoning_tokens', 'reasoning_count', 'reasoning_missing', 'event_count', 'detail_count', 'first_at', 'last_at', 'shared_tokens', 'maintenance')) for r in source.get('rows', [])]
            result['analytics']['windows'][key] = window
    result['recovery'] = _pick(value.get('recovery'), ('scanning', 'recovered_events', 'recovered_tokens',
        'inferred_tokens', 'runtime_tokens', 'unresolved_events', 'unresolved_tokens'))
    return result


class WebController:
    def __init__(self, folder, config, database, demo=False, startup_enabled=True):
        self._folder, self._config, self._database = Path(folder), config, database
        self._demo, self._startup_enabled = demo, startup_enabled and not demo
        self._engine = None
        self._mutation = threading.RLock()
        self._closed = threading.Event()
        self._window_handler = None
        self._hidden = False
        self._pairing = {'stage': 'saved' if config.get('link_enabled') else 'idle'}
        self._update = {'status': '尚未检测更新', 'ready': False}
        self._update_offer = None
        self._notices = []
        self._settings_cache = self._public_settings()
        self._accounts_cache = self._public_accounts()
        self._demo_view = {}

    def _public_settings(self):
        return _pick(self._config, _SETTINGS)

    def _public_accounts(self):
        return [dict(account=account, **_pick(value, ('label', 'added_at', 'cap')))
                for account, value in self._config.get('tracked_accounts', {}).items()]

    def _publish(self):
        self._settings_cache = self._public_settings()
        self._accounts_cache = self._public_accounts()

    def _start(self):
        if not self._demo:
            try:
                with self._mutation:
                    self._restart()
            except Exception as exc:
                self._notices.append('后台启动未完成（'+type(exc).__name__+'），请检查设置后保存重试')
            if self._startup_enabled and getattr(sys, 'frozen', False):
                threading.Thread(target=self._auto_updates, daemon=True).start()

    def _auto_updates(self):
        if self._closed.wait(15):
            return
        while not self._closed.is_set():
            if self._config.get('auto_update', True):
                self.command('update_check', {'manual': False})
                while self._update_offer and not self._closed.is_set() and self._config.get('auto_update', True):
                    result = self.command('update_install', {'confirmed': True})
                    if result['ok'] or not ((self._engine and self._engine.blocked) or self._database.get('paused_processes')):
                        break
                    if self._closed.wait(30):
                        return
            if self._closed.wait(3600):
                return

    def _restart(self):
        if self._engine:
            self._engine.close()
        if not self._config.get('program_paths'):
            try:
                self._config['program_paths'] = discover_programs()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass
        self._engine = Engine(self._database, self._config, account_enroller=self._auto_enroll)
        self._engine.background_mode = self._hidden
        self._engine.start()
        if self._startup_enabled:
            startup.apply(self._config.get('autostart', True), self._folder,
                          elevated=self._config['auto_block'])
        self._publish()

    def _save(self, restart=False):
        save_config(self._folder/'settings.json', self._config)
        self._publish()
        if restart and not self._demo:
            self._restart()

    def _close(self):
        with self._mutation:
            if self._engine:
                self._engine.close()
                if self._engine.blocked or self._database.get('paused_processes'):
                    self._engine.restore()
            self._closed.set()

    def _set_hidden(self, hidden):
        self._hidden = bool(hidden)
        if self._engine:
            self._engine.background_mode = self._hidden

    def snapshot(self):
        # Never wait for a network command, disk write, or Engine restart here.
        raw = self._engine.snapshot() if self._engine else copy.deepcopy(self._demo_view)
        safe = _view(raw)
        if raw.get('notifications'):
            # Engine emits these internally; arbitrary mesh payloads never enter this list.
            self._notices = list(dict.fromkeys(self._notices + raw['notifications']))[-5:]
        settings = copy.deepcopy(self._settings_cache)
        if 'auto_block' in raw:
            settings['auto_block'] = raw['auto_block']
        return dict(version=__version__, view=safe, settings=settings,
                    accounts=copy.deepcopy(self._accounts_cache),
                    pairing=dict(self._pairing, **safe['connection']),
                    update=copy.deepcopy(self._update), notices=list(self._notices))

    def window_action(self, action):
        if action not in ('minimize', 'close', 'quit', 'drag', 'resize', 'maximize', 'shown', 'browse_program', 'export_diagnostics'):
            return {'ok': False, 'error': '未知窗口操作'}
        if not self._window_handler:
            return {'ok': False, 'error': '窗口尚未就绪'}
        return self._window_handler(action)

    def command(self, action, payload=None):
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            return {'ok': False, 'error': '操作参数须为对象'}
        actions = {'refresh', 'account_scan', 'account_add', 'account_remove', 'account_history', 'member_history', 'chart_history', 'cap_save',
                   'limit_toggle', 'compensation_toggle', 'maintenance_toggle', 'restore', 'note_save', 'color_save', 'device_order_save', 'device_remove', 'settings_save',
                   'programs_discover', 'pair_generate', 'pair_join', 'tailscale_login', 'tailscale_switch',
                   'connection_save', 'update_check', 'update_install', 'diagnostics', 'group_rule'}
        if action not in actions:
            return {'ok': False, 'error': '未知操作'}
        if not self._mutation.acquire(blocking=action in ('chart_history', 'member_history')):
            return {'ok': False, 'error': '上一项操作尚未结束，请稍候'}
        try:
            if self._closed.is_set():
                raise ValueError('程序正在退出')
            data = getattr(self, '_'+action)(payload)
            return {'ok': True, 'data': data}
        except (ValueError, KeyError, TypeError) as exc:
            # Validation messages are ours; raw external exception data is excluded.
            message = str(exc)[:240] if isinstance(exc, ValueError) else '操作参数不完整或格式不正确'
            for key in ('group_secret', 'relay_token'):
                secret = self._config.get(key)
                if secret:
                    message = message.replace(secret, '[已隐藏]')
            return {'ok': False, 'error': message}
        except Exception as exc:
            return {'ok': False, 'error': '操作未完成（'+type(exc).__name__+'），请检查网络或稍后重试'}
        finally:
            self._mutation.release()

    def _confirm(self, payload):
        if payload.get('confirmed') is not True:
            raise ValueError('请先在确认窗口中确认此操作')

    def _tracked(self, payload, current=False):
        account = payload.get('account')
        if account not in self._config.get('tracked_accounts', {}):
            raise ValueError('请先添加需要统计的账号')
        if current:
            ident = identity(self._config['codex_home'])
            if ident.get('mode') != 'account' or ident.get('account') != account:
                raise ValueError('当前登录账号已变化，请重新选择')
        return account

    def _display_scope(self, payload):
        account = payload.get('account')
        if self._config.get('shared_group_enabled'):
            scope = 'group:'+hashlib.sha256(self._config['group_secret'].encode()).hexdigest()[:20]
            if account == scope:
                return scope
        return self._tracked(payload)

    def _read_account(self, payload):
        account = payload.get('account')
        raw = self._engine.snapshot() if self._engine else self._demo_view
        if self._config.get('shared_group_enabled') and any(card['account'] == account for card in raw.get('account_summaries', [])):
            return account
        return self._tracked(payload)

    def _refresh(self, _payload):
        if self._engine:
            self._engine.wakeup.set()

    def _account_scan(self, _payload):
        ident = identity(self._config['codex_home'])
        if self._engine:
            self._engine.wakeup.set()
        return _identity(ident)

    def _auto_enroll(self, ident, now):
        account = ident.get('account')
        if (not self._config.get('shared_billing_v1') or ident.get('mode') != 'account' or not self._engine
                or account in self._config.get('tracked_accounts', {})
                or account in self._config.get('auto_enroll_excluded', [])):
            return False
        # Never block a worker behind a UI restart waiting for that worker to exit.
        if not self._mutation.acquire(blocking=False):
            return False
        try:
            from .shared_policy import load_rules
            with self._engine.group_db.connect() as db:
                accounts = [row[0] for row in db.execute('SELECT DISTINCT account FROM facts')]
            rules = load_rules(self._engine.group_db, accounts, now)
            if not rules.get('policy') or account not in rules['accounts']:
                return False
            enroll(self._config, ident, now)
            self._save()
            self._engine.tracked.update(self._config['tracked_accounts'])
            return True
        finally:
            self._mutation.release()

    def _account_add(self, _payload):
        ident = identity(self._config['codex_home'])
        enroll(self._config, ident)
        self._config['auto_enroll_excluded'] = [a for a in self._config.get('auto_enroll_excluded', []) if a != ident['account']]
        self._save(restart=True)
        return _identity(ident)

    def _account_remove(self, payload):
        self._confirm(payload)
        account = self._tracked(payload)
        self._config['tracked_accounts'].pop(account)
        self._config['auto_enroll_excluded'] = sorted(set(self._config.get('auto_enroll_excluded', [])) | {account})
        self._save(restart=True)

    def _account_history(self, payload):
        account = self._read_account(payload)
        if not self._engine:
            raise ValueError('账本尚未就绪')
        if self._config.get('shared_billing_v1'):
            view = self._engine.snapshot()
            card = next((row for row in view.get('account_summaries', []) if row['account'] == account), {})
            analytics = view.get('analytics', {})
            window = analytics.get('windows', {}).get('cycle', {})
            devices = []
            for person in view.get('summary', {}).get('devices', []):
                tokens = sum(row['tokens'] for row in window.get('rows', []) if row['account'] == account and row['device'] == person['id'])
                quota = sum(row['quota'] for row in window.get('quota_rows', []) if row['account'] == account and row['device'] == person['id'])
                devices.append(dict(id=person['id'], name=person['name'], tokens=tokens, estimated=quota))
            history = dict(day={}, week={}, month={})
            sources = {source for person in view.get('summary', {}).get('devices', []) for source in person.get('device_ids', [])}
            for source in sources:
                saved = self._engine.ledger.history(account, source)
                for unit in history:
                    for date, tokens in saved[unit].items():
                        history[unit][date] = history[unit].get(date, 0)+tokens
            return dict(summary=dict(epoch=card.get('epoch'), devices=devices), history=history, shared=True)
        return dict(summary=_summary(self._engine.ledger.summary(account)),
                    history=self._engine.ledger.history(account, self._config['device_id']))

    def _cap_save(self, payload):
        if self._config.get('shared_group_enabled'):
            raise ValueError('共享组固定三人均分，两账号共200点，每人基础份额66.67点')
        account = self._tracked(payload)
        if not self._engine:
            raise ValueError('监测尚未就绪')
        self._engine.set_cap(payload['cap'], expected_account=account)

    def _member_history(self, payload):
        scope = self._display_scope(payload)
        if not self._engine:
            raise ValueError('账本尚未就绪')
        view = self._engine.snapshot()
        cycles = view.get('analytics', {}).get('cycles', [])
        selected = next((c for c in cycles if str(c['id']) == payload.get('cycle')), None)
        from .usage_history import archive_boundary, archive_end, range_window
        from .shared_policy import load_rules
        from .shared_quota import attribution
        now = time.time()
        accounts = [c['account'] for c in view.get('account_summaries', [])] if scope.startswith('group:') else [scope]
        rules = load_rules(self._engine.group_db, accounts, now) if self._config.get('shared_billing_v1') else None
        cutoff = archive_boundary(self._engine.group_db, rules, now)
        if payload.get('cycle') == 'archive' and cutoff is not None:
            ranges = [dict(account=a, start=0, end=archive_end(self._engine.group_db, rules, now)) for a in accounts if a == rules['policy']['clean_start']['account']]
        elif selected and selected.get('account', scope) in accounts:
            ranges = [dict(account=selected.get('account', scope), start=selected['started'],
                           end=min(selected.get('ended') or now, now), after=selected['started'])]
        else:
            raise ValueError('该周期尚未同步或不存在')
        attributed = attribution(self._engine.group_db, rules, now) if rules else None
        window = range_window(self._engine.group_db, ranges, rules, attributed)
        return _view({'analytics': dict(account=scope, windows={'selected': window, 'cycle': window})})['analytics']

    def _chart_history(self, payload):
        from .analytics import usage
        account = self._display_scope(payload)
        end = payload.get('end')
        period = payload.get('period', 'hour')
        if period not in ('hour', 'six_hours', 'twelve_hours', 'day'):
            raise ValueError('图表时间范围无效')
        if type(end) not in (int,float) or not math.isfinite(end) or end < 0 or end > time.time()+60:
            raise ValueError('图表时间无效')
        if not self._engine:
            raise ValueError('账本尚未就绪')
        options = dict(day_end=end,day_buffer=True) if period == 'day' else dict(hour_end=end,hour_buffer=True)
        if period in ('six_hours', 'twelve_hours'):
            options = dict(rolling_period=period, rolling_end=end, rolling_buffer=True)
        windows = ('hour','hour_curve') if period == 'hour' else (period,)
        options['selected_windows'] = windows
        if account.startswith('group:'):
            from .shared_view import shared_usage
            from .shared_policy import load_rules
            labels = {card['account']: card['label'] for card in self._engine.snapshot().get('account_summaries', [])}
            rules = load_rules(self._engine.group_db, labels, time.time()) if self._config.get('shared_billing_v1') else None
            analytics = shared_usage(self._engine.group_db, account, labels, rules=rules, **options)
        else:
            analytics = usage(self._engine.group_db,account,**options)
        analytics['windows'] = {k:v for k,v in analytics['windows'].items() if k in windows}
        return _view({'analytics':analytics})['analytics']

    def _maintenance_toggle(self, payload):
        if not self._engine or not self._config.get('shared_billing_v1'):
            raise ValueError('共享计费尚未就绪')
        if type(payload.get('enabled')) is not bool:
            raise ValueError('软件维护开关须为布尔值')
        self._engine.commands.put(('maintenance', dict(enabled=payload['enabled'], at=time.time())))
        self._engine.wakeup.set()

    def _compensation_toggle(self, payload):
        if self._config.get('shared_billing_v1'):
            return self._group_rule(dict(payload, kind='compensation'))
        if self._config.get('shared_group_enabled'):
            raise ValueError('过渡版尚未启用两账号统一补偿，不会执行旧单账号补偿')
        account = self._tracked(payload)
        if type(payload.get('enabled')) is not bool:
            raise ValueError('补偿开关须为布尔值')
        if not self._engine:
            raise ValueError('账本尚未就绪')
        self._engine.commands.put(('compensation', dict(account=account, enabled=payload['enabled'])))
        self._engine.wakeup.set()

    def _group_rule(self, payload):
        if not self._engine or not self._config.get('shared_billing_v1'):
            raise ValueError('共享计费尚未就绪')
        group = self._engine.snapshot().get('shared_group', {})
        if not group.get('can_manage'):
            raise ValueError('共同规则由共享组管理员修改')
        if payload.get('revision') != group.get('revision'):
            raise ValueError('规则已更新，请刷新后重试')
        kind = payload.get('kind')
        if kind not in ('compensation','bind','accounts','reset','availability'):
            raise ValueError('未知共享规则操作')
        if kind == 'compensation':
            raise ValueError('跨周期补偿已固定开启，不能关闭')
        if kind == 'bind' and (payload.get('person') not in ('person1','person2','person3') or not any(d['id'] == payload.get('device') for d in group.get('devices', []))):
            raise ValueError('请选择已入组设备和成员')
        if kind == 'accounts' and (not isinstance(payload.get('accounts'), list) or len(payload['accounts']) != 2 or len(set(payload['accounts'])) != 2):
            raise ValueError('请选择两个不同账号')
        if kind == 'reset' and (type(payload.get('started')) not in (int,float) or not math.isfinite(payload['started']) or payload.get('cause') not in ('natural','card','official')):
            raise ValueError('请选择周期和重置原因')
        if kind == 'availability' and (type(payload.get('paused')) is not bool or not any(a['account'] == payload.get('account') for a in group.get('available_accounts', []))):
            raise ValueError('请选择共享账号和暂停状态')
        self._engine.commands.put(('group_rule', {k: payload[k] for k in ('kind','revision','enabled','person','device','accounts','account','started','cause','paused') if k in payload}))
        self._engine.wakeup.set()
        return dict(queued=True)

    def _limit_toggle(self, payload):
        if not isinstance(payload.get('enabled'), bool):
            raise ValueError('enabled 须为布尔值')
        if not payload['enabled']:
            return self._restore(payload)
        if self._config.get('shared_group_enabled'):
            raise ValueError('共享组模式保留用量监测；同一进程无法分别限制两个账号，自动限制未启用')
        self._confirm(payload)
        self._tracked(payload, current=True)
        self._validate_limit(self._config)
        self._config['auto_block'] = True
        self._save(restart=True)

    def _validate_limit(self, value):
        paths = value.get('program_paths', [])
        if not is_admin() or not paths or any(not Path(p).is_file() for p in paths):
            raise ValueError('自动限额需要管理员权限，并选择实际存在的 Codex EXE')

    def _restore(self, _payload):
        self._config['auto_block'] = False
        self._save()
        if self._engine:
            self._engine.commands.put(('restore', None))
            self._engine.wakeup.set()

    def _device(self, payload):
        account = self._display_scope(payload)
        view = self._engine.snapshot() if self._engine else self._demo_view
        summary = view.get('summary') or {}
        if summary.get('account') != account:
            raise ValueError('设备列表已变化，请刷新后重试')
        device = next((d for d in summary.get('devices', []) if d.get('id') == payload.get('device') and not d.get('removed')), None)
        if not device:
            raise ValueError('设备不存在或已移除')
        return account, device['id']

    def _note_save(self, payload):
        account, device = self._device(payload)
        text = payload.get('text', '')
        if not isinstance(text, str) or len(text) > 40:
            raise ValueError('备注最多 40 个字符')
        notes = self._config.setdefault('device_notes', {}).setdefault(account, {})
        if text.strip():
            notes[device] = text.strip()
        else:
            notes.pop(device, None)
        self._save()

    def _color_save(self, payload):
        account, device = self._device(payload)
        color = str(payload.get('color', '')).lower()
        if color not in _COLORS:
            raise ValueError('请选择预设颜色')
        self._config.setdefault('device_colors', {}).setdefault(account, {})[device] = color
        self._save()

    def _device_order_save(self, payload):
        account = self._display_scope(payload)
        view = self._engine.snapshot() if self._engine else self._demo_view
        summary = view.get('summary') or {}
        devices = payload.get('devices')
        if not isinstance(devices, list) or any(not isinstance(d, str) for d in devices):
            raise ValueError('设备顺序格式不正确')
        current = {d['id'] for d in summary.get('devices', []) if not d.get('removed')}
        if summary.get('account') != account or len(devices) != len(current) or set(devices) != current:
            raise ValueError('设备列表已变化，请刷新后重新排序')
        self._config.setdefault('device_order', {})[account] = devices[:]
        self._save()

    def _device_remove(self, payload):
        self._confirm(payload)
        if self._config.get('shared_billing_v1') and self._engine:
            account, device = self._display_scope(payload), payload.get('device')
            if not any(row['id'] == device for row in self._engine.snapshot().get('shared_group', {}).get('devices', [])):
                raise ValueError('设备列表已变化，请重新选择')
        else:
            account, device = self._device(payload)
        if device == self._config['device_id']:
            raise ValueError('不能在此移除本机')
        if not self._engine:
            raise ValueError('监测尚未就绪')
        self._engine.commands.put(('remove_device', dict(account=account, device=device)))
        self._engine.wakeup.set()

    def _settings_save(self, payload):
        changes = payload.get('settings')
        if not isinstance(changes, dict) or set(changes)-_EDITABLE:
            raise ValueError('包含不支持的设置')
        candidate = dict(self._config, **changes)
        if self._config.get('shared_billing_v1'):
            if 'quota_display' in changes and changes['quota_display'] != 'fair':
                raise ValueError('共享计费固定使用补偿模式')
            candidate['quota_display'] = 'fair'
        if self._config.get('shared_group_enabled') and candidate.get('auto_block'):
            raise ValueError('共享组模式不能分别限制同一进程内的两个账号，自动限制未启用')
        cap, multiplier, interval = float(candidate['quota']), float(candidate['multiplier']), int(candidate['interval'])
        if not 0 < cap <= 100 or not .05 <= multiplier <= 20 or not 15 <= interval <= 300:
            raise ValueError('配额：(0,100]；权重系数：0.05–20；查询间隔：15–300 秒')
        if candidate.get('theme', 'system') not in ('system', 'light', 'dark'):
            raise ValueError('未知界面主题')
        if candidate.get('quota_display', 'personal') not in ('account', 'personal', 'fair'):
            raise ValueError('未知配额显示方式')
        for key in ('autostart', 'auto_update', 'auto_block'):
            if not isinstance(candidate[key], bool):
                raise ValueError('开关设置须为布尔值')
        if not isinstance(candidate['program_paths'], list) or any(not isinstance(p, str) or not p.lower().endswith('.exe') for p in candidate['program_paths']):
            raise ValueError('程序路径须为 EXE 文件列表')
        if not Path(candidate['codex_home']).is_dir():
            raise ValueError('Codex 数据目录不存在')
        changing_protected_targets = any(candidate[key] != self._config[key] for key in ('codex_home', 'program_paths'))
        if candidate['auto_block'] and (not self._config['auto_block'] or changing_protected_targets):
            self._confirm(payload)
            self._validate_limit(candidate)
        candidate.update(name=str(candidate['name']).strip()[:80] or 'Windows', quota=cap, multiplier=multiplier, interval=interval)
        self._config.update(candidate)
        self._save(restart=bool(set(changes)-{'quota_display', 'auto_update', 'theme'}))

    def _programs_discover(self, _payload):
        return discover_programs()

    def _connection_save(self, payload):
        url = str(payload.get('rendezvous_url', self._config.get('rendezvous_url', ''))).strip()
        token = str(payload.get('relay_token', self._config.get('relay_token', ''))).strip()
        if url:
            validate_url(url)
            if len(token) < 24:
                raise ValueError('服务访问密钥至少 24 个字符')
        force_relay = payload.get('force_relay', self._config.get('force_relay', False))
        if not isinstance(force_relay, bool):
            raise ValueError('force_relay 须为布尔值')
        stun = str(payload.get('stun_url', self._config.get('stun_url', ''))).strip()
        self._config.update(rendezvous_url=url, relay_token=token, stun_url=stun,
                            force_relay=force_relay, fingerprint='', link_enabled=not bool(url))
        self._save(restart=True)

    def _pair_generate(self, _payload):
        ident = identity(self._config['codex_home'])
        if not self._config.get('shared_group_enabled') and (ident.get('mode') != 'account' or ident.get('account') not in self._config.get('tracked_accounts', {})):
            raise ValueError('请先添加当前 Codex 订阅账号')
        mesh = self._engine.mesh if self._engine else None
        from .tsnet_mesh import TailscaleMesh
        if not isinstance(mesh, TailscaleMesh):
            self._config.update(tailscale_enabled=True, link_enabled=True, rendezvous_url='', pair_role='sender')
            self._pairing = {'stage': 'authorizing'}
            self._save(restart=True)
            return {'pending': True}
        from .tsnet_node import tail_ip
        state = mesh.connection_state()
        ips = [ip for ip in state.get('ips', []) if tail_ip(ip)]
        if not state.get('ready') or not ips:
            self._pairing = {'stage': 'authorizing'}
            return {'pending': True}
        self._config['tailscale_ip'] = ips[0]
        self._config['pair_role'] = 'sender'
        self._save()
        code = create_code(self._config)
        self._pairing = {'stage': 'ready'}
        return {'code': code}

    def _pair_join(self, payload):
        self._confirm(payload)
        pair = read_code(payload['code'])
        if not pair.get('tailscale_enabled'):
            raise ValueError('请使用原设备组授权 Tailscale 后生成的 CQG4 匹配码；不要新建组或清空账本')
        self._config.update(pair, tailscale_join_request=uuid.uuid4().hex, pair_role='receiver',
                            shared_group_enabled=True, shared_group_prepare_v1=True, auto_block=False)
        self._pairing = {'stage': 'saved'}
        self._save(restart=True)

    def _tailscale_login(self, _payload):
        from .tsnet_node import auth_url
        mesh = self._engine.mesh if self._engine else None
        if not mesh:
            raise ValueError('请先生成或接收匹配码以启动节点')
        state = mesh.connection_state()
        url = state.get('auth_url', '')
        if state.get('ready'):
            return {'opened': False, 'message': '节点已经授权并就绪'}
        if not auth_url(url) and state.get('state') == 'NeedsLogin' and getattr(mesh, 'node', None):
            mesh.node.request('login')
            for _ in range(15):
                url = mesh.node.request('status').get('auth_url', '')
                if auth_url(url) or self._closed.wait(1):
                    break
        if url and auth_url(url):
            webbrowser.open(url)
            return {'opened': True}
        return {'opened': False, 'message': '节点正在连接，请稍后重试授权'}

    def _tailscale_switch(self, payload):
        self._confirm(payload)
        mesh = self._engine.mesh if self._engine else None
        node = getattr(mesh, 'node', None)
        if not node:
            raise ValueError('请先启动匹配节点')
        from .tsnet_node import auth_url
        node.request('logout')
        node.request('login')
        self._config.pop('tailscale_ip', None)
        self._pairing = {'stage': 'authorizing'}
        self._save()
        for _ in range(30):
            url = node.request('status').get('auth_url', '')
            if url and auth_url(url):
                webbrowser.open(url)
                return {'opened': True}
            if self._closed.wait(1):
                break
        raise ValueError('尚未取得新授权链接，请稍后点击授权登录')

    def _update_check(self, payload):
        if self._demo or not getattr(sys, 'frozen', False):
            self._update = {'status': '源码／演示模式不安装更新，请使用发布版 EXE', 'ready': False}
            return self._update
        from . import updater
        self._update = {'status': '正在检测更新…', 'ready': False}
        try:
            manifest = updater.check(__version__)
            if not manifest:
                self._update = {'status': '当前已是最新正式版 v'+__version__, 'ready': False}
                return self._update
            previous = self._folder/'updates'/'result.json'
            failed = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else {}
            if payload.get('manual', True) is False and failed.get('ok') is False and failed.get('version') == manifest['payload']['version']:
                self._update = {'status': '上次更新未完成，请手动重试', 'ready': False}
                return self._update
            staged = updater.download(manifest, self._folder/'updates')
            self._update_offer = (manifest, staged)
            self._update = {'status': '新版已下载并通过签名校验', 'ready': True, 'version': manifest['payload']['version']}
            return self._update
        except Exception:
            self._update = {'status': '更新检测／下载失败，保留当前版本', 'ready': False}
            raise

    def _update_install(self, payload):
        self._confirm(payload)
        if not self._update_offer:
            raise ValueError('尚无通过签名校验的更新')
        engine = self._engine
        if (engine and engine.blocked) or self._database.get('paused_processes'):
            raise ValueError('正在限额保护，请解除后再安装更新')
        from . import updater
        try:
            if engine:
                engine.close()
                if engine.blocked or self._database.get('paused_processes'):
                    raise ValueError('已延后更新，保持当前限额保护')
            updater.launch_helper(*self._update_offer, self._folder, self._hidden)
        except Exception:
            if engine and engine.stop_event.is_set():
                engine.stop_event.clear()
                engine.start()
            raise
        self._closed.set()
        if self._window_handler:
            self._window_handler('quit')
        return {'installing': True}

    def _diagnostics(self, _payload):
        raw = self._engine.snapshot() if self._engine else self._demo_view
        return report(raw, self._config['device_id'])
