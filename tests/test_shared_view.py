from quota_guard.shared_view import shared_usage, shared_overview
from quota_guard.storage import Database
from quota_guard.web_controller import _view


A, B = 'a'*64, 'b'*64


def epoch(database, account, started, end, closed=False):
    with database.connect() as db:
        db.execute('INSERT INTO epochs(account,started,ended,baseline,used,reset_at,observed_at,reason) VALUES (?,?,?,?,?,?,?,?)',
                   (account, started, end if closed else None, 0, 20, end, started, 'reset'))


def event(database, identifier, account, ts, tokens, device='one', details=True):
    with database.connect() as db:
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                   (identifier, device, account, ts, 'gpt-6-astra', tokens, tokens, 1))
        if details:
            db.execute('INSERT INTO event_details VALUES (?,?,?,?,?)',
                       (identifier, tokens-10, tokens-20, 10, 5))


def test_cycle_pair_counts_whole_selected_cycles_without_adjacent_or_future_usage(tmp_path):
    database = Database(tmp_path/'group.sqlite')
    epoch(database, A, 100, 200)
    epoch(database, B, 60, 160, True)
    epoch(database, B, 160, 260)
    event(database, 'a_before', A, 100, 1000)
    event(database, 'a_current', A, 170, 100)
    event(database, 'a_future', A, 181, 9000)
    event(database, 'b_pre_overlap', B, 80, 200)
    event(database, 'b_end', B, 160, 300)
    event(database, 'b_next', B, 170, 8000)
    result = shared_usage(database, 'group:test', {A: 'A', B: 'B'}, 180)
    rows = result['windows']['cycle']['rows']
    assert sum(row['tokens'] for row in rows) == 600
    assert sum(row['input_tokens'] for row in rows) == 570
    assert sum(row['cache_tokens'] for row in rows) == 540
    assert sum(row['output_tokens'] for row in rows) == 30
    assert result['cycle_start'] == 60
    assert [cycle['usage_until'] for cycle in result['cycle_pair']['cycles']] == [180, 160]
    assert sum(row['tokens'] for row in result['windows']['pie_hour']['rows']) == 9600
    members = {'one': dict(name='One', online=True, accounts=[A, B], current_account=A)}
    overview = shared_overview(database, 'group:test', {A: 'A', B: 'B'}, members, 'one', 180, result)
    assert overview['summary']['devices'][0]['tokens'] == 600
    assert overview['summary']['devices'][0]['estimated'] is None
    assert overview['account_summaries'][1]['epoch']['started'] == 160
    public = _view(dict(analytics=result, shared_group=dict(enabled=True, billing_start_note='免结转')))
    assert public['analytics']['cycle_pair']['cycles'][1]['label'] == 'B'
    assert public['shared_group']['billing_start_note'] == '免结转'
    assert public['analytics']['windows']['cycle']['quota_rows'] == []


def test_missing_account_remains_incomplete_and_missing_cache_is_not_fabricated(tmp_path):
    database = Database(tmp_path/'group.sqlite')
    epoch(database, A, 100, 200)
    event(database, 'legacy', A, 120, 100, details=False)
    result = shared_usage(database, 'group:test', {A: 'A', B: 'B'}, 180)
    assert not result['cycle_pair']['complete']
    row = result['windows']['cycle']['rows'][0]
    assert row['tokens'] == row['detail_missing'] == 100
    assert row['cache_tokens'] is None


def test_no_login_and_empty_group_can_render_all_windows(tmp_path):
    result = shared_usage(Database(tmp_path/'empty.sqlite'), 'group:test', {}, 180)
    assert not result['cycle_pair']['complete']
    assert result['windows']['cycle']['rows'] == []
    assert {'six_hours', 'twelve_hours', 'pie_twelve_hours'} <= set(result['windows'])


def test_removed_members_not_restored_and_offline_member_has_no_active_status(tmp_path):
    database = Database(tmp_path/'group.sqlite')
    epoch(database, A, 100, 200)
    for identifier in ('one', 'gone'):
        event(database, identifier, A, 120, 100, identifier)
        with database.connect() as db:
            db.execute('INSERT INTO devices(account,id,name,cap,seen,active) VALUES (?,?,?,?,?,?)',
                       (A, identifier, identifier, 50, 120, 3))
    analytics = shared_usage(database, 'group:test', {A: 'A'}, 180)
    members = {'one': dict(name='One', online=False, accounts=[A], current_account=A)}
    overview = shared_overview(database, 'group:test', {A: 'A'}, members, 'local', 180, analytics, removed={'gone'})
    assert len(overview['summary']['devices']) == 1
    person = overview['summary']['devices'][0]
    assert not person['online'] and not person['active']
    assert person['tokens'] == 100
