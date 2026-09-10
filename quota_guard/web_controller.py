"""Allowlisted local WebView bridge; the existing Engine owns all accounting."""
import copy
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
    result = _pick(value, ('account', 'unassigned', 'provisional', 'allocation', 'reset_pending', 'server_time'))
    result['token_budget'] = _pick(value.get('token_budget'), ('sampled_tokens', 'used_tokens', 'total_tokens', 'source'))
    result['epoch'] = _pick(value.get('epoch'), ('id', 'account', 'started', 'ended', 'baseline', 'used',
        'reset_at', 'observed_at', 'reason', 'cycle')) or None
    result['devices'] = [_pick(d, ('id', 'name', 'cap', 'seen', 'scan_at', 'active', 'uncertain', 'logged_in',
        'unbound_active', 'unbound_uncertain', 'estimated', 'settled', 'carry', 'fair_cap', 'fair_base_cap', 'tokens', 'weight', 'unknown_tokens', 'online', 'removed'))
        for d in value.get('devices', [])]
    result['attribution_gaps'] = [_pick(d, ('start', 'end', 'delta', 'reason', 'devices', 'unknown_models'))
                                  for d in value.get('attribution_gaps', [])]
    return result


def _view(value):
    result = _pick(value, ('status', 'blocked', 'active', 'uncertain', 'unbound_active',
                          'unbound_uncertain', 'auto_block', 'tracked_since', 'pair_scope'))
    # Transport exceptions can contain server payloads or URLs; do not export them.
    result['error'] = '监测异常，请查看同步诊断并检查登录状态' if value.get('error') else ''
    result['identity'] = _identity(value.get('identity'))
    result['summary'] = _summary(value.get('summary'))
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
    result['analytics'] = _pick(analytics, ('account', 'at', 'models', 'cycle_start', 'statistics_start'))
    result['analytics']['cycles'] = []
    for row in analytics.get('cycles', []):
        safe = _pick(row, ('id', 'started', 'ended', 'reset_at', 'reset_type', 'used_percent', 'baseline_percent',
            'sampled_tokens', 'total_tokens', 'source', 'sample_tokens', 'sample_percent', 'is_current', 'change_percent', 'change_tokens', 'reference_count', 'reference_total_tokens',
            'reference_starts', 'reduction_tokens', 'reduction_percent'))
        safe['models'] = [_pick(m, ('model', 'tokens')) for m in row.get('models', [])]
        result['analytics']['cycles'].append(safe)
    result['analytics']['windows'] = {}
    for key in ('cycle', 'total', 'today', 'pie_hour', 'pie_six_hours', 'hour', 'hour_curve', 'day', 'week', 'month'):
        source = analytics.get('windows', {}).get(key)
        if source:
            window = _pick(source, ('start', 'step', 'count', 'quota_ready'))
            window['quota_rows'] = [_pick(r, ('device', 'model', 'bucket', 'quota')) for r in source.get('quota_rows', [])]
            window['rows'] = [_pick(r, ('device', 'model', 'bucket', 'tokens', 'weight', 'unknown')) for r in source.get('rows', [])]
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
        self._engine = Engine(self._database, self._config)
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
        actions = {'refresh', 'account_scan', 'account_add', 'account_remove', 'account_history', 'cap_save',
                   'limit_toggle', 'restore', 'note_save', 'color_save', 'device_order_save', 'device_remove', 'settings_save',
                   'programs_discover', 'pair_generate', 'pair_join', 'tailscale_login', 'tailscale_switch',
                   'connection_save', 'update_check', 'update_install', 'diagnostics'}
        if action not in actions:
            return {'ok': False, 'error': '未知操作'}
        if not self._mutation.acquire(blocking=False):
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

    def _refresh(self, _payload):
        if self._engine:
            self._engine.wakeup.set()

    def _account_scan(self, _payload):
        return _identity(identity(self._config['codex_home']))

    def _account_add(self, _payload):
        ident = identity(self._config['codex_home'])
        enroll(self._config, ident)
        self._save(restart=True)
        return _identity(ident)

    def _account_remove(self, payload):
        self._confirm(payload)
        account = self._tracked(payload)
        self._config['tracked_accounts'].pop(account)
        self._save(restart=True)

    def _account_history(self, payload):
        account = self._tracked(payload)
        if not self._engine:
            raise ValueError('账本尚未就绪')
        return dict(summary=_summary(self._engine.ledger.summary(account)),
                    history=self._engine.ledger.history(account, self._config['device_id']))

    def _cap_save(self, payload):
        account = self._tracked(payload)
        if not self._engine:
            raise ValueError('监测尚未就绪')
        self._engine.set_cap(payload['cap'], expected_account=account)

    def _limit_toggle(self, payload):
        if not isinstance(payload.get('enabled'), bool):
            raise ValueError('enabled 须为布尔值')
        if not payload['enabled']:
            return self._restore(payload)
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
        account = self._tracked(payload)
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
        account = self._tracked(payload)
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
        if ident.get('mode') != 'account' or ident.get('account') not in self._config.get('tracked_accounts', {}):
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
        self._config.update(pair, tailscale_join_request=uuid.uuid4().hex, pair_role='receiver')
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
