import copy
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quota_guard import web_controller as bridge
from quota_guard.storage import Database, defaults


@pytest.fixture
def controller(tmp_path, monkeypatch):
    config = defaults()
    config.update(device_id='local', codex_home=str(tmp_path), tracked_accounts={'a'*64: {
        'label': 'Test account', 'added_at': 10, 'cap': 50}})
    item = bridge.WebController(tmp_path, config, Database(tmp_path/'local.sqlite'), demo=True)
    view = dict(identity=dict(mode='account', account='a'*64), summary=dict(account='a'*64,
        devices=[dict(id='local', name='Local', tokens=80, cap=50), dict(id='peer', name='Peer', tokens=148, cap=50)]))
    item._demo_view = view
    monkeypatch.setattr(bridge, 'save_config', Mock())
    return item


def test_bridge_exposes_only_three_public_methods(controller):
    assert [name for name in dir(controller) if not name.startswith('_')] == ['command', 'snapshot', 'window_action']


def test_compensation_switch_targets_tracked_account_and_requires_bool(controller):
    controller._engine=Mock(commands=queue.Queue(),wakeup=threading.Event())
    assert controller.command('compensation_toggle',dict(account='a'*64,enabled=True))['ok']
    assert controller._engine.commands.get_nowait()==('compensation',dict(account='a'*64,enabled=True))
    assert not controller.command('compensation_toggle',dict(account='a'*64,enabled='false'))['ok']
    assert not controller.command('compensation_toggle',dict(account='b'*64,enabled=True))['ok']


def test_snapshot_never_exposes_private_settings_or_transport(controller):
    secret = controller._config['group_secret']
    controller._config.update(relay_token='relay-credential', auth={'access_token': 'oauth'})
    raw = controller._demo_view
    raw.update(connection=dict(ready=True, auth_url='https://login.tailscale.com/a/secret', raw='transport'),
        secret=secret, error='Authorization: Bearer oauth', analytics={'windows': {'day': {
            'start': 0, 'step': 1, 'count': 1, 'rows': [dict(device='local', model='gpt', bucket=0,
            tokens=80, weight=9, unknown=0, raw={'secret': secret})], 'transport': secret}}})
    raw['summary']['devices'][0]['auth'] = secret
    raw['summary']['devices'][0].update(unbound_active=2, unbound_uncertain=1)
    controller._publish()
    value = controller.snapshot()
    serialized = json.dumps(value)
    for private in (secret, 'relay-credential', 'oauth', 'login.tailscale.com', 'transport', 'access_token'):
        assert private not in serialized
    assert value['view']['analytics']['windows']['day']['rows'][0]['tokens'] == 80
    assert value['view']['summary']['devices'][1]['tokens'] == 148
    assert value['view']['summary']['devices'][0]['unbound_active'] == 2
    assert value['view']['summary']['devices'][0]['unbound_uncertain'] == 1


def test_snapshot_does_not_wait_for_mutating_network_action(controller):
    entered, release = threading.Event(), threading.Event()
    def hold():
        with controller._mutation:
            entered.set()
            release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(1)
    try:
        assert controller.snapshot()['version']
        assert controller.command('refresh')['ok'] is False
    finally:
        release.set()
        worker.join()


def test_sync_timestamp_requires_receipts_and_all_peers_caught_up(controller):
    view = controller._demo_view
    view.update(sync_progress={'p': dict(state='caught_up'), 'q': dict(state='caught_up')},
                sync_receipts={'p': 120, 'q': 100})
    assert controller.snapshot()['view']['sync_confirmed_at'] == 100
    view['sync_progress']['q']['state'] = 'stale'
    assert controller.snapshot()['view']['sync_confirmed_at'] is None
    view['sync_progress']['q']['state'] = 'caught_up'
    view['sync_receipts'].pop('q')
    assert controller.snapshot()['view']['sync_confirmed_at'] is None


def test_cap_delegates_expected_account_to_existing_engine(controller):
    controller._engine = Mock()
    response = controller.command('cap_save', {'account': 'a'*64, 'cap': 60})
    assert response['ok']
    controller._engine.set_cap.assert_called_once_with(60, expected_account='a'*64)
    assert not controller.command('cap_save', {'account': 'b'*64, 'cap': 70})['ok']
    assert controller._engine.set_cap.call_count == 1


def test_chart_history_is_local_read_only_and_account_scoped(controller):
    controller._engine=SimpleNamespace(group_db=controller._database)
    result=controller.command('chart_history',{'account':'a'*64,'end':1000})
    assert result['ok']
    assert set(result['data']['windows'])=={'hour','hour_curve'}
    assert result['data']['account']=='a'*64
    day=controller.command('chart_history',{'account':'a'*64,'end':1000,'period':'day'})
    assert day['ok'] and set(day['data']['windows'])=={'day'}
    assert day['data']['windows']['day']['count']==32*24
    assert not controller.command('chart_history',{'account':'a'*64,'end':1000,'period':'month'})['ok']
    assert not controller.command('chart_history',{'account':'b'*64,'end':1000})['ok']
    for value in (-1,float('nan'),float('inf'),'1000',True):
        assert not controller.command('chart_history',{'account':'a'*64,'end':value})['ok']


def test_device_remove_uses_synced_command_keeps_history(controller):
    controller._engine = SimpleNamespace(snapshot=lambda: copy.deepcopy(controller._demo_view),
                                        commands=queue.Queue(), wakeup=threading.Event())
    payload = {'account': 'a'*64, 'device': 'peer'}
    assert not controller.command('device_remove', payload)['ok']
    assert controller._engine.commands.empty()
    assert controller.command('device_remove', dict(payload, confirmed=True))['ok']
    assert controller._engine.commands.get_nowait() == ('remove_device', dict(account='a'*64, device='peer'))
    assert not controller.command('device_remove', dict(payload, device='local', confirmed=True))['ok']
    assert len(controller._demo_view['summary']['devices']) == 2


def test_settings_reject_secret_injection_and_invalid_values_without_saving(controller):
    before = copy.deepcopy(controller._config)
    for changes in ({'group_secret': 'changed'}, {'device_id': 'other'}, {'quota': float('nan')},
                    {'interval': 1}, {'auto_block': 'yes'}, {'quota_display': 'wrong'},
                    {'program_paths': ['script.ps1']}):
        assert not controller.command('settings_save', {'settings': changes})['ok']
        assert controller._config == before
    bridge.save_config.assert_not_called()


def test_note_color_are_account_scoped_validated_and_saved_without_restart(controller):
    payload = dict(account='a'*64, device='peer')
    assert controller.command('note_save', dict(payload, text='Workstation'))['ok']
    assert controller.command('color_save', dict(payload, color='#f6b763'))['ok']
    assert controller.snapshot()['settings']['device_notes']['a'*64]['peer'] == 'Workstation'
    assert controller.snapshot()['settings']['device_colors']['a'*64]['peer'] == '#f6b763'
    assert not controller.command('color_save', dict(payload, color='#000000'))['ok']
    assert not controller.command('note_save', dict(payload, device='removed', text='Hidden'))['ok']
    assert controller._engine is None


def test_pair_join_needs_confirmation_and_preserves_local_identity(controller):
    code = bridge.create_code(dict(tailscale_enabled=True, device_id='peer', tailscale_ip='100.64.0.1',
                                  group_secret='z'*40))
    assert not controller.command('pair_join', {'code': code})['ok']
    assert controller.command('pair_join', {'code': code, 'confirmed': True})['ok']
    assert controller._config['device_id'] == 'local'
    assert controller._config['group_secret'] == 'z'*40
    assert 'a'*64 in controller._config['tracked_accounts']
    assert 'z'*40 not in json.dumps(controller.snapshot())


def test_device_order_is_complete_account_scoped_and_does_not_restart(controller, monkeypatch):
    restart = Mock()
    monkeypatch.setattr(controller, '_restart', restart)
    account = 'a'*64
    controller._demo_view['summary']['devices'].append(dict(id='removed', removed=True))
    for ids in (['peer'], ['peer', 'peer'], ['local', 'removed'], ['local', 'peer', 'other'], 'peer'):
        assert not controller.command('device_order_save', {'account': account, 'devices': ids})['ok']
    assert not controller.command('device_order_save', {'account': 'b'*64, 'devices': ['peer', 'local']})['ok']
    assert 'device_order' not in controller._config
    assert controller.command('device_order_save', {'account': account, 'devices': ['peer', 'local']})['ok']
    assert controller.snapshot()['settings']['device_order'] == {account: ['peer', 'local']}
    bridge.save_config.assert_called_once()
    restart.assert_not_called()


def test_restore_queues_engine_restore_and_close_restores_paused_processes(controller):
    controller._engine = Mock(commands=queue.Queue(), wakeup=threading.Event(), blocked=True)
    assert controller.command('restore')['ok']
    assert controller._engine.commands.get_nowait() == ('restore', None)
    assert controller._config['auto_block'] is False
    controller._close()
    controller._engine.close.assert_called_once()
    controller._engine.restore.assert_called_once()


def test_update_install_requires_verified_offer_and_preserves_protection(controller):
    assert not controller.command('update_install', {'confirmed': True})['ok']
    controller._update_offer = ({'payload': {'version': '1.0.0'}}, 'stage')
    controller._engine = Mock(blocked=True)
    assert not controller.command('update_install', {'confirmed': True})['ok']
    controller._engine.close.assert_not_called()


def test_window_actions_are_allowlisted(controller):
    controller._window_handler = Mock(return_value={'ok': True})
    assert not controller.window_action('execute')['ok']
    controller._window_handler.assert_not_called()
    assert controller.window_action('minimize')['ok']
    controller._window_handler.assert_called_once_with('minimize')


def test_start_failure_keeps_controller_available_with_actionable_notice(controller, monkeypatch):
    controller._demo = False
    monkeypatch.setattr(controller, '_restart', Mock(side_effect=OSError('private detail')))
    controller._start()
    value = controller.snapshot()
    assert '后台启动未完成' in value['notices'][0]
    assert 'private detail' not in str(value)
    assert not controller._closed.is_set()


def test_verified_update_uses_existing_download_and_helper(controller, monkeypatch):
    from quota_guard import updater
    controller._demo = False
    monkeypatch.setattr(bridge.sys, 'frozen', True, raising=False)
    manifest = {'payload': {'version': '9.9.9'}}
    check, download, launch = Mock(return_value=manifest), Mock(return_value='verified-stage'), Mock()
    monkeypatch.setattr(updater, 'check', check)
    monkeypatch.setattr(updater, 'download', download)
    monkeypatch.setattr(updater, 'launch_helper', launch)
    controller._window_handler = Mock()
    response = controller.command('update_check')
    assert response['ok'] and response['data']['ready']
    download.assert_called_once_with(manifest, controller._folder/'updates')
    assert not controller.command('update_install')['ok']
    launch.assert_not_called()
    assert controller.command('update_install', {'confirmed': True})['ok']
    launch.assert_called_once_with(manifest, 'verified-stage', controller._folder, False)
    controller._window_handler.assert_called_once_with('quit')


def test_failed_update_helper_restarts_existing_engine(controller, monkeypatch):
    from quota_guard import updater
    controller._update_offer = ({'payload': {'version': '9.9.9'}}, 'stage')
    event = threading.Event()
    event.set()
    controller._engine = Mock(blocked=False, stop_event=event)
    monkeypatch.setattr(updater, 'launch_helper', Mock(side_effect=OSError('private detail')))
    response = controller.command('update_install', {'confirmed': True})
    assert not response['ok'] and 'private detail' not in response['error']
    controller._engine.start.assert_called_once()
    assert not event.is_set()
    assert not controller._closed.is_set()


def test_connection_omissions_preserve_values_and_explicit_empty_clears(controller):
    controller._config.update(rendezvous_url='wss://relay.example.test', relay_token='r'*32,
                              stun_url='stun:stun.example.test:19302', force_relay=False, link_enabled=False)
    assert controller.command('connection_save', {'force_relay': True})['ok']
    assert controller._config['rendezvous_url'] == 'wss://relay.example.test'
    assert controller._config['relay_token'] == 'r'*32
    assert controller._config['stun_url'] == 'stun:stun.example.test:19302'
    assert controller._config['link_enabled'] is False
    assert controller.command('connection_save', {'rendezvous_url': '', 'relay_token': '', 'stun_url': ''})['ok']
    assert controller._config['rendezvous_url'] == controller._config['relay_token'] == controller._config['stun_url'] == ''
    assert controller._config['force_relay'] is True


def test_display_only_setting_needs_no_limit_confirmation_or_restart(controller, monkeypatch):
    controller._config['auto_block'] = True
    controller._demo = False
    restart = Mock()
    monkeypatch.setattr(controller, '_restart', restart)
    validate = Mock()
    monkeypatch.setattr(controller, '_validate_limit', validate)
    assert controller.command('settings_save', {'settings': {'quota_display': 'personal', 'auto_update': False}})['ok']
    assert controller._config['auto_block'] is True
    assert controller.command('settings_save', {'settings': {'quota_display': 'fair'}})['ok']
    assert controller._config['quota_display'] == 'fair'
    restart.assert_not_called()
    validate.assert_not_called()
    before = copy.deepcopy(controller._config)
    assert not controller.command('settings_save', {'settings': {'program_paths': ['other.exe']}})['ok']
    assert controller._config == before
    assert controller.command('settings_save', {'settings': {'program_paths': ['other.exe']}, 'confirmed': True})['ok']
    validate.assert_called_once()


def test_theme_persists_without_restarting_monitor(controller):
    controller._restart = Mock()
    for theme in ('light', 'dark', 'system'):
        assert controller.command('settings_save', {'settings': {'theme': theme}})['ok']
        assert controller.snapshot()['settings']['theme'] == theme
    assert not controller.command('settings_save', {'settings': {'theme': 'invalid'}})['ok']
    controller._restart.assert_not_called()


def test_cycle_statistics_and_budget_projection_are_allowlisted(controller):
    view = controller._demo_view
    view['summary']['token_budget'] = dict(total_tokens=450000000, source='粗估', private='hidden-budget')
    view['analytics'] = dict(statistics_start=100, cycles=[dict(id='1000:100', started=100,
        total_tokens=450000000, change_percent=-5, source='同步样本', private='hidden-cycle', reference_count=3,
        reference_total_tokens=500000000, reference_starts=[10,20,30], reduction_tokens=50000000, reduction_percent=10,
        models=[dict(model='gpt-6-astra', tokens=1000, private='hidden-model')])])
    projected = controller.snapshot()['view']
    assert projected['summary']['token_budget'] == dict(total_tokens=450000000, source='粗估')
    assert projected['analytics']['statistics_start'] == 100
    assert projected['analytics']['cycles'][0]['change_percent'] == -5
    assert projected['analytics']['cycles'][0]['reduction_percent'] == 10
    assert projected['analytics']['cycles'][0]['reference_total_tokens'] == 500000000
    assert projected['analytics']['cycles'][0]['models'] == [dict(model='gpt-6-astra', tokens=1000)]
    assert 'hidden-' not in json.dumps(projected)
