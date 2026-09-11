from types import SimpleNamespace
import hashlib

from test_clean_start import setup_clean
from test_shared_billing import A, B, add_event, close_samples
from quota_guard.shared_policy import load_rules
from quota_guard.shared_view import shared_usage
from quota_guard.usage_history import archive_boundary, range_window
from quota_guard.web_controller import WebController, _view
from quota_guard.storage import defaults


def test_exhaustion_archives_exact_event_boundary_without_changing_trend_or_facts(tmp_path):
    db, journals, _ = setup_clean(tmp_path)
    add_event(journals, account=A, at=190)
    add_event(journals, account=B, at=200)
    add_event(journals, account=B, at=201)
    rules = load_rules(db, [A, B], 400)
    assert archive_boundary(db, rules, 400) is None
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=800, at=200), 200)
    close_samples(journals)
    rules = load_rules(db, [A, B], 400)
    assert archive_boundary(db, rules, 199) is None
    with db.connect() as connection:
        before = [tuple(r) for r in connection.execute('SELECT * FROM events ORDER BY id')]
    data = shared_usage(db, 'group:test', {A:'账号1', B:'账号2'}, 400, rules=rules)
    assert data['donut_archive_at'] == 200
    assert sum(r['tokens'] for r in data['donut_windows']['pie_hour']['rows']) == 2200
    assert {r['account'] for r in data['donut_windows']['pie_hour']['rows']} == {B}
    assert sum(r['tokens'] for r in data['windows']['hour_curve']['rows']) == 3300
    assert sum(r['tokens'] for r in range_window(db, [dict(account=a, start=0, end=200) for a in (A,B)], rules)['rows']) == 2200
    safe = _view({'analytics': data})['analytics']
    assert safe['donut_archive_at'] == 200
    assert safe['donut_windows']['pie_hour']['rows'] == _view({'analytics': {'windows': {'pie_hour': data['donut_windows']['pie_hour']}}})['analytics']['windows']['pie_hour']['rows']
    with db.connect() as connection:
        assert [tuple(r) for r in connection.execute('SELECT * FROM events ORDER BY id')] == before
    # A later cycle reaching 100 does not move this one-time archive point.
    journals['one'].append(A, 'quota', dict(account=A, used=0, reset_at=1500, at=801), 801)
    journals['one'].append(A, 'quota', dict(account=A, used=0, reset_at=1500, at=820), 820)
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=1500, at=900), 900)
    assert archive_boundary(db, load_rules(db, [A,B], 950), 950) == 200


def test_personal_history_reads_selected_account_cycle_and_rejects_unknown_scope(tmp_path, monkeypatch):
    db, journals, _ = setup_clean(tmp_path)
    add_event(journals, account=A, at=150)
    add_event(journals, account=B, at=180)
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=800, at=200), 200)
    add_event(journals, account=B, at=250)
    rules = load_rules(db, [A,B], 400)
    data = shared_usage(db, 'group:test', {A:'账号1',B:'账号2'}, 400, rules=rules)
    config = defaults()
    config.update(shared_billing_v1=True, shared_group_enabled=True)
    scope = 'group:'+hashlib.sha256(config['group_secret'].encode()).hexdigest()[:20]
    controller = WebController(tmp_path, config, db, demo=True)
    view = dict(display_account=scope, identity={'account': A}, analytics=data,
                account_summaries=[dict(account=A),dict(account=B)])
    controller._engine = SimpleNamespace(group_db=db, snapshot=lambda: view)
    monkeypatch.setattr('quota_guard.web_controller.time.time', lambda: 400)
    archived = controller.command('member_history', dict(account=scope, cycle='archive'))
    assert archived['ok'], archived
    assert sum(r['tokens'] for r in archived['data']['windows']['cycle']['rows']) == 1100
    assert {r['account'] for r in archived['data']['windows']['cycle']['rows']} == {A}
    second = next(c for c in data['cycles'] if c['account'] == B)
    result = controller.command('member_history', dict(account=scope, cycle=second['id']))
    assert result['ok'], result
    rows = result['data']['windows']['cycle']['rows']
    assert {r['account'] for r in rows} == {B}
    assert sum(r['tokens'] for r in rows) == 2200
    assert not controller.command('member_history', dict(account='group:other', cycle=second['id']))['ok']
    assert not controller.command('member_history', dict(account=scope, cycle='missing'))['ok']
