import json
from unittest.mock import MagicMock, patch

import pytest

from quota_guard.gui import App
from test_account_scope import setup
from test_core import A
from test_runtime_evidence import fixture, log, row


def test_provider_only_runtime_does_not_become_an_account(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 110); log(src, 150)
    api = row('api', 140)
    api['payload'] = json.dumps(dict(turn_id='t', turn_at=110, provider='relay'))
    index.scan(0)
    for _ in range(2):
        index.prepare([api, row('new', 150)], {})
        assert index.resolve('new') == dict(reason='identity_conflict')


@pytest.mark.parametrize('phase', ['scan', 'reconcile', 'inactive'])
def test_history_failure_keeps_heartbeat_and_summary_fresh(tmp_path, phase):
    e, _, step, *_ = setup(tmp_path)
    step(100)
    mesh = MagicMock()
    mesh.peer_states.return_value = {'remote': {'route': 'public'}}
    mesh.connection_state.return_value = {'phase': 'connected'}
    mesh.status = 'connected'
    e.mesh = mesh
    target, method = (e, 'recover_inactive') if phase == 'inactive' else (e.recovery, phase)
    with patch.object(target, method, side_effect=IndexError('fixture failure')), patch.object(e, 'enforce') as enforce:
        step(200)
        step(210)
        view = e.snapshot()
        assert '历史补记异常' in view['error']
        assert view['summary']['epoch']['observed_at'] == 210
        assert view['peers'] == mesh.peer_states.return_value
        assert mesh.send.call_args.args[1]['presence']['scan_at'] == 210
        assert view['summary']['devices'][0]['online']
        enforce.assert_not_called()
    step(220)
    assert e.snapshot()['error'] == ''


@pytest.mark.parametrize('connected,expected', [(True, '连接在线·监测状态过期'), (False, '离线/已切换')])
def test_overview_distinguishes_connection_from_stale_monitoring(connected, expected):
    app = MagicMock()
    app.config = dict(name='local', device_id='local', quota=33, tracked_accounts={A: {}})
    app.pair_flow = {}
    app.history_values = {}
    app.cards = {key: MagicMock() for key in ('global', 'local', 'reset')}
    app.table.get_children.return_value = ()
    device = dict(id='remote', name='remote', active=0, uncertain=0, online=False, tokens=0, estimated=0, cap=33)
    summary = dict(epoch=dict(used=20, baseline=20, reset_at=10000), devices=[device],
                   unassigned=0, provisional=0, reset_pending=False)
    App.render(app, dict(identity=dict(mode='account', account=A), summary=summary,
                         peers={'remote': {'route': 'public'}} if connected else {}))
    assert app.table.insert.call_args.kwargs['values'][1] == expected
