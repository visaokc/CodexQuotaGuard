from test_account_scope import setup
from test_core import A
from test_pair_bootstrap import Mesh
from quota_guard.sync_diagnostics import progress_text, report, rejection_reason


def test_sync_rejection_survives_render_and_heartbeat_until_valid_facts(tmp_path):
    e, _, step, *_ = setup(tmp_path)
    step(100)
    e.mesh = Mesh()
    record = dict(account=A, origin='peer', seq=1, ts=101, kind='profile',
                  payload=dict(device='wrong', name='Peer', cap=33))
    e.receive('peer', dict(type='facts', account=A, records=[record]))
    step(102)
    assert '同步记录被拒绝' in e.snapshot()['error']
    e.receive('peer', dict(type='sync', account=A, vector={}))
    step(103)
    assert '同步记录被拒绝' in e.snapshot()['error']
    record['payload']['device'] = 'peer'
    e.receive('peer', dict(type='facts', account=A, records=[record]))
    step(104)
    assert not e.snapshot()['error']


def test_sync_progress_distinguishes_connection_from_journal_convergence(tmp_path):
    e, _, step, *_ = setup(tmp_path)
    step(100)
    e.mesh = Mesh()
    step(101)
    assert e.snapshot()['sync_progress']['peer']['state'] == 'waiting'
    local = e.journal.vector(A)
    e.receive('peer', dict(type='sync', account=A, vector={'peer': 5}))
    step(102)
    progress = e.snapshot()['sync_progress']['peer']
    assert progress['receive'] == 5
    assert progress['send'] >= sum(local.values())
    e.receive('peer', dict(type='sync', account=A, vector=e.journal.vector(A)))
    step(103)
    assert e.snapshot()['sync_progress']['peer']['state'] == 'caught_up'


def test_invalid_vector_not_saved_or_acknowledged(tmp_path):
    e, _, step, *_ = setup(tmp_path)
    step(100)
    e.mesh = Mesh()
    e.receive('peer', dict(type='sync', account=A, vector={'peer': -1}))
    step(101)
    assert 'peer' not in e.sync_vectors
    assert 'peer' not in e.sync_receipts
    e.receive('peer', dict(type='sync', account=A, vector={}))
    step(102)
    assert not e.snapshot()['error']


def test_report_excludes_identity_secrets_and_runtime_paths():
    import json
    view = dict(identity={'label': 'private-email'}, connection={'auth_url': 'private-url'},
        config={'group_secret': 'private-key'}, recovery={'runtime': {'path': 'private-path'},
        'unresolved_tokens': 123}, summary={'epoch': {'used': 29, 'secret': 'private-secret'},
        'devices': [{'id': 'peer', 'tokens': 42, 'name': 'private-name'}]})
    value = report(view, 'local')
    assert 'private-' not in json.dumps(value)
    assert value['recovery']['unresolved_tokens'] == 123
    assert value['epoch']['used'] == 29
    assert rejection_reason(ValueError('private-key')) == 'ValueError'
    assert rejection_reason(ValueError('同步批次过大')) == '同步批次过大'


def test_progress_text_never_calls_waiting_or_missing_data_synced():
    assert '尚无在线对端' in progress_text({})
    assert '等待' in progress_text({'p': dict(state='waiting', send=0, receive=0)})
    assert '待发送 2 条' in progress_text({'p': dict(state='syncing', send=2, receive=3)})
    assert '旧日志' in progress_text({'p': dict(state='caught_up', send=0, receive=0)})


def test_export_button_writes_numeric_report(tmp_path):
    import json
    from types import SimpleNamespace
    from unittest.mock import patch
    from quota_guard.gui import App
    path = tmp_path/'diagnostics.json'
    app = SimpleNamespace(root=None, config={'device_id': 'one'},
                          last_view={'recovery': {'unresolved_tokens': 456}})
    with patch('quota_guard.gui.filedialog.asksaveasfilename', return_value=str(path)):
        App.export_sync_diagnostics(app)
    assert json.loads(path.read_text(encoding='utf-8'))['recovery']['unresolved_tokens'] == 456
