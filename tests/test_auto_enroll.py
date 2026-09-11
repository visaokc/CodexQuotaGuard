from types import SimpleNamespace

from test_shared_billing import setup_group, A, B
from test_web_controller import controller


def test_auto_enroll_shared_login_persists_once_and_excludes_api_and_unrelated_accounts(controller, tmp_path):
    db, _, _ = setup_group(tmp_path/'group')
    controller._config.update(shared_billing_v1=True, shared_group_enabled=True)
    controller._engine = SimpleNamespace(group_db=db, tracked=controller._config['tracked_accounts'], snapshot=lambda:controller._demo_view)
    identity = dict(mode='account', account=B, label='Shared second')
    assert controller._auto_enroll(identity, 400)
    assert controller._config['tracked_accounts'][B]['added_at'] == 400
    assert B in controller._engine.tracked
    assert B in {a['account'] for a in controller.snapshot()['accounts']}
    assert not controller._auto_enroll(identity, 450)
    assert controller._config['tracked_accounts'][B]['added_at'] == 400
    assert not controller._auto_enroll(dict(mode='api',account=''), 450)
    assert not controller._auto_enroll(dict(mode='account',account='c'*64,label='Other'), 450)


def test_manual_stop_is_not_undone_by_auto_enrollment(controller, tmp_path):
    db, _, _ = setup_group(tmp_path/'group')
    controller._config.update(shared_billing_v1=True, shared_group_enabled=True, auto_enroll_excluded=[B])
    controller._engine = SimpleNamespace(group_db=db, tracked={})
    assert not controller._auto_enroll(dict(mode='account',account=B,label='B'), 400)
    assert B not in controller._config['tracked_accounts']


def test_engine_auto_enrolls_before_scanning_new_login(tmp_path):
    from quota_guard.storage import Database, defaults
    from quota_guard.engine import Engine
    from quota_guard.accounts import enroll
    cfg = defaults()
    cfg.update(codex_home=str(tmp_path/'codex'),tracked_accounts={A:dict(label='A',added_at=10,cap=50)},started_at=10)
    def register(ident, now):
        enroll(cfg, ident, now)
        return True
    engine = Engine(Database(tmp_path/'local.sqlite'),cfg,identity_reader=lambda _:dict(mode='account',account=B,label='B',multiplier=1),
                    quota_reader=lambda _:dict(account=B,used=0,reset_at=900,at=400),account_enroller=register)
    engine.step(400)
    assert B in engine.tracked and B in engine.scanner.allowed_accounts
    assert engine.last_snapshot['account'] == B
    assert engine.tracked[B]['added_at'] == 400
    engine.step(401)
    assert engine.tracked[B]['added_at'] == 400
