from quota_guard.pair_status import presentation


def test_pending_and_timeout_are_not_overwritten_by_transport_status():
    view = dict(mesh='自动连接已启动 · 等待对方上线／输入匹配码', peers={})
    assert '生成' in presentation(dict(stage='preparing', elapsed=12), view)['title']
    failed = presentation(dict(stage='failed', error='公共连接准备超时'), view)
    assert failed['title'] == '匹配码尚未生成'
    assert '超时' in failed['detail']


def test_saved_is_not_connected_and_connected_is_not_synced():
    flow = dict(stage='saved')
    value = presentation(flow, dict(peers={}, connection=dict(phase='waiting', relay_ready=True)))
    assert value['title'] == '配对信息已保存 · 正在连接'
    assert value['steps'] == [True, True, False, False]
    value = presentation(flow, dict(peers={'b': {}}, sync_receipts={}))
    assert '等待首次用量同步' in value['title']
    assert not value['steps'][3]


def test_only_current_peer_receipts_count_and_disconnect_is_visible():
    view = dict(peers={'b': {'route': '公共加密中转'}}, sync_receipts={'b': 1000})
    result = presentation(dict(stage='saved'), view, now=1010)
    assert '已收到同步数据' in result['title'] and result['steps'][3]
    assert '10 秒前' in result['detail']
    stale = presentation(dict(stage='saved'), view, now=1100)
    assert '同步消息暂未更新' in stale['title'] and not stale['steps'][3]
    view['peers'] = {}
    result = presentation(dict(stage='saved'), view, now=1020)
    assert not result['steps'][3] and '上次' in result['detail']


def test_code_ready_is_not_peer_confirmed_and_unknown_reason_not_invented():
    value = presentation(dict(stage='ready'), dict(peers={}))
    assert value['title'] == '匹配码已生成 · 请复制给对方'
    assert '是否' in value['detail']


def test_api_scope_does_not_claim_sync_with_stale_receipts():
    view = dict(identity={'mode': 'api'}, peers={}, sync_receipts={'b': 1000})
    result = presentation(dict(stage='saved'), view)
    assert result['title'] == '当前未同步 · 请登录已添加的订阅账号'
    assert not result['steps'][3]


def test_engine_records_receipts_only_after_accepted_account_data(tmp_path):
    from test_account_scope import setup, ident
    from test_core import A, B
    e, current, step, *_ = setup(tmp_path)
    step(100)
    e.receive('wrong-account', dict(type='sync', account=B))
    e.receive('bad-data', dict(type='facts', account=A, records=[{'invalid': True}]))
    step(101)
    assert not e.snapshot()['sync_receipts']
    e.receive('peer', dict(type='sync', account=A, records=[], vector={}))
    step(102)
    assert e.snapshot()['sync_receipts'] == {'peer': 102}
    current[0] = ident(B)
    step(103)
    assert not e.snapshot()['sync_receipts']


def test_real_entry_quit_with_persistent_code_textbox(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    process = subprocess.Popen([sys.executable, 'main.py', '--demo', '--smoke-seconds', '1',
        '--data-dir', str(tmp_path/'entry')], cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONIOENCODING='utf-8'),
        creationflags=0x08000000)
    try:
        output, _ = process.communicate(timeout=20)
        assert process.returncode == 0 and b'Exception in Tkinter callback' not in output, output.decode()
    finally:
        if process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, creationflags=0x08000000)
            process.wait(timeout=5)
