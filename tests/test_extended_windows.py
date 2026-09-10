import pytest

from quota_guard.analytics import chart_data, usage
from quota_guard.storage import Database


def insert(database, event_id, ts, tokens, account='account', device='a'):
    with database.connect() as db:
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                   (event_id, device, account, ts, 'gpt-6-astra', tokens, tokens, True))
        db.execute('INSERT INTO event_details(id,input_tokens,cached_input_tokens,output_tokens) VALUES (?,?,?,?)',
                   (event_id, tokens, tokens//2, 0))


@pytest.mark.parametrize('window,duration,count', [('six_hours', 21600, 72), ('twelve_hours', 43200, 144)])
def test_extended_charts_keep_five_minute_buckets_and_account_boundaries(tmp_path, window, duration, count):
    database = Database(tmp_path/'windows.sqlite')
    now = 4_000_123
    start = (now//300-count+1)*300
    insert(database, 'before', start-.01, 1000)
    insert(database, 'first', start, 10)
    insert(database, 'second', start+300, 20)
    insert(database, 'last', now-.01, 30, device='b')
    insert(database, 'end', now, 2000)
    insert(database, 'future', now+1, 3000)
    insert(database, 'other', now-1, 4000, account='other')
    snapshot = usage(database, 'account', now)
    result = chart_data(snapshot, window, metric='tokens')
    assert result['step'] == 300 and len(result['points']) == count
    assert snapshot['windows'][window]['start'] == start
    assert result['total'] == 60
    assert result['points'][0] == 10 and result['points'][1] == 20 and result['points'][-1] == 30
    assert chart_data(snapshot, window, metric='tokens', device='b')['total'] == 30
    assert sum(row['cache_tokens'] for row in snapshot['windows'][window]['rows']) == 30
    assert chart_data(usage(database, 'other', now), window, metric='tokens')['total'] == 4000
    assert count*result['step'] == duration


@pytest.mark.parametrize('window,duration', [('pie_six_hours', 21600), ('pie_twelve_hours', 43200)])
def test_extended_pies_use_exact_rolling_start_and_exclude_current_instant(tmp_path, window, duration):
    database = Database(tmp_path/'pie.sqlite')
    now = 4_000_123
    start = now-duration
    insert(database, 'before', start-.001, 1000)
    insert(database, 'boundary', start, 10)
    insert(database, 'inside', start+.1, 20)
    insert(database, 'last', now-.001, 30)
    insert(database, 'end', now, 2000)
    insert(database, 'other', now-1, 4000, account='other')
    snapshot = usage(database, 'account', now)
    result = chart_data(snapshot, window, metric='tokens')
    assert snapshot['windows'][window]['start'] == start
    assert result['step'] == duration and result['points'] == [60]
    assert chart_data(usage(database, 'account', now+.05), window, metric='tokens')['total'] == 2050


@pytest.mark.parametrize('window', ['six_hours', 'twelve_hours', 'pie_twelve_hours'])
def test_extended_windows_retain_official_quota_attribution_without_cross_account_leak(tmp_path, window):
    database = Database(tmp_path/'quota.sqlite')
    now = 4_000_123
    for account, delta in [('account', 6), ('other', 90)]:
        insert(database, account+'-1', now-100, 10, account)
        insert(database, account+'-2', now-50, 20, account, 'b')
        with database.connect() as db:
            row = db.execute('INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES (?,?,?,?,?,?,?)',
                             (account, now-200, 0, delta, now+1000, now-1, 'initial'))
            db.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)',
                       (row.lastrowid, now-200, now-1, delta))
    selected = usage(database, 'account', now)['windows'][window]
    assert selected['quota_ready']
    assert sum(row['quota'] for row in selected['quota_rows']) == pytest.approx(6)
    assert {row['device']: row['quota'] for row in selected['quota_rows']} == pytest.approx({'a': 2, 'b': 4})
    assert not selected['quota_pending_rows'] and not selected['quota_estimate_rows']
