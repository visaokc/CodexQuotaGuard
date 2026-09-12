import pytest

from quota_guard.analytics import usage, chart_data, quota_display
from quota_guard.charts import axis_scale
from quota_guard.storage import Database, defaults
from quota_guard.gui import App, sync_caption
from quota_guard.pairing import load_config
from quota_guard.charts import smooth_points
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger


@pytest.mark.parametrize('state,caption', [('caught_up', '已同步'), ('syncing', '同步中'),
    ('error', '同步异常'), ('stale', '等待确认'), ('waiting', '等待确认')])
def test_sync_card_requires_confirmed_ledger_progress(state, caption):
    assert sync_caption(dict(sync_progress={'peer': {'state': state}})) == caption
    assert sync_caption(dict(peers={'peer': {}})) == '等待确认'
    assert sync_caption({}) == '未连接'


def test_synced_charts_converge_after_duplicates_reordering_and_late_history(tmp_path):
    import hashlib
    account, now = 'a'*64, 4_000_000
    databases = [Database(tmp_path/f'{device}.sqlite') for device in ('a', 'b')]
    journals = [Journal(db, Ledger(db), device) for db, device in zip(databases, ('a', 'b'))]
    records = [('a', now-10, 'm1', 100, 1, True), ('b', now-1800, 'm2', 300, 0, False),
               ('a', now-2*86400, 'm1', 700, 7, True), ('b', now-3400, 'm1', 50, .5, True),
               ('a', now-31*86400, 'm1', 1000, 10, True)]
    for device, ts, model, tokens, weight, known in records:
        event = dict(id=hashlib.sha256(f'{device}-{ts}'.encode()).hexdigest(), device=device,
                     account=account, ts=ts, model=model, tokens=tokens, weight=weight, known=known)
        journals[device == 'b'].append(account, 'events', [event], now-1)
    for sender, receiver in ((journals[0], journals[1]), (journals[1], journals[0])):
        batch = sender.since(account, receiver.vector(account))
        receiver.merge(account, list(reversed(batch)))
        receiver.merge(account, batch)
    snapshots = [usage(db, account, now) for db in databases]
    for window, expected in dict(hour=450, day=450, week=1150, month=1150, total=2150).items():
        assert chart_data(snapshots[0], window, metric='tokens')['total'] == expected
        for metric in ('tokens', 'weight'):
            for model in (None, 'm1', 'm2'):
                for device in (None, 'a', 'b'):
                    a, b = [chart_data(snapshot, window, model, metric, device) for snapshot in snapshots]
                    assert a == b


def test_chart_axis_covers_data_with_readable_ticks():
    assert axis_scale(700.3) == 800
    assert axis_scale(17_321_998) == 20_000_000
    assert axis_scale(0) > 0
    assert axis_scale(.00013) >= .00013


@pytest.mark.parametrize('values', [[0, 100, 0, 0, 40], [20, 0, 1, 300, 5], [0, 0], [10]])
def test_smoothing_preserves_samples_and_does_not_invent_extrema(values):
    points = smooth_points(values)
    assert all((i, v) in points for i, v in enumerate(values))
    for x, y in points:
        i = min(int(x), len(values)-1)
        a, b = values[i], values[min(i+1, len(values)-1)]
        assert min(a, b) <= y <= max(a, b)


def insert(db, id, ts, device='a', model='m1', tokens=100, weight=1, known=True, account='account'):
    with db.connect() as conn:
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                     (id, device, account, ts, model, tokens, weight, known))


def test_personal_percentage_uses_allocation_as_denominator():
    assert quota_display(16.76, 50, True) == pytest.approx((33.52, 100))
    assert quota_display(33.24, 50, True) == pytest.approx((66.48, 100))
    assert quota_display(16.76, 50) == (16.76, 50)
    assert quota_display(55, 50, True) == pytest.approx((110, 100))
    assert quota_display(0, 0, True) == (None, 100)


def test_rolling_boundaries_account_and_model_filters(tmp_path):
    db = Database(tmp_path/'data.sqlite')
    now = 4_000_000
    start = (now//120-29)*120
    insert(db, 'hour-start', start, tokens=100, weight=1)
    insert(db, 'other-device', now-1, device='b', model='m2', tokens=300, weight=3)
    insert(db, 'before-hour', start-.01, tokens=700, weight=7)
    insert(db, 'other-account', now-1, tokens=900, account='other')
    insert(db, 'future', now+1, tokens=900)
    insert(db, 'before-month', now-30*86400-1, tokens=900)
    data = usage(db, 'account', now)
    hourly = chart_data(data, 'hour')
    assert hourly['total'] == 4
    assert len(hourly['points']) == 30
    assert hourly['step'] == 120
    assert hourly['points'][0] == 1
    assert hourly['points'][-1] == 3
    assert hourly['shares'] == pytest.approx({'a': 25, 'b': 75})
    assert chart_data(data, 'hour', 'm1', 'tokens')['total'] == 100
    assert chart_data(data, 'day', metric='tokens', device='b')['total'] == 300
    assert chart_data(data, 'day', 'm1', 'tokens', device='b')['total'] == 0
    assert chart_data(data, 'day', metric='tokens')['total'] == 1100
    assert len(chart_data(data, 'week')['points']) == 7
    assert len(chart_data(data, 'month')['points']) == 30
    assert sum(chart_data(data, 'month', metric='tokens')['points']) == 1100
    assert chart_data(data, 'total', metric='tokens')['total'] == 2000


def test_unknown_model_falls_back_for_whole_selection_and_empty_has_no_share(tmp_path):
    db = Database(tmp_path/'data.sqlite')
    insert(db, 'known', 99, tokens=100, weight=10)
    insert(db, 'unknown', 99, device='b', model='new', tokens=300, weight=0, known=False)
    data = usage(db, 'account', 100)
    hourly = chart_data(data, 'hour')
    assert hourly['metric'] == 'tokens'
    assert hourly['shares'] == pytest.approx({'a': 25, 'b': 75})
    assert chart_data(data, 'hour', 'm1')['metric'] == 'weight'
    empty = chart_data(data, 'hour', 'missing')
    assert empty['total'] == 0 and empty['shares'] == {}


def test_offline_history_and_late_events_are_included_once(tmp_path):
    db = Database(tmp_path/'data.sqlite')
    insert(db, 'old-peer', 100, device='retired')
    assert chart_data(usage(db, 'account', 200), 'hour')['total'] == 1
    insert(db, 'late', 110, device='offline', tokens=200, weight=2)
    for _ in range(2):
        data = chart_data(usage(db, 'account', 200), 'hour')
        assert data['total'] == 3
        assert set(data['devices']) == {'retired', 'offline'}


def test_device_note_persists_by_account_without_renaming_device(tmp_path):
    app = App.__new__(App)
    app.config = defaults()
    app.folder, app.demo = tmp_path, False
    name = app.config['name']
    App.save_device_note(app, 'a', 'peer', '  工作站  ')
    App.save_device_note(app, 'b', 'peer', '另一备注')
    config = load_config(tmp_path/'settings.json')
    assert config['device_notes'] == {'a': {'peer': '工作站'}, 'b': {'peer': '另一备注'}}
    assert config['name'] == name
    App.save_device_note(app, 'a', 'peer', '')
    assert load_config(tmp_path/'settings.json')['device_notes']['a'] == {}
    App.save_device_color(app, 'a', 'peer', '#e383cf')
    App.save_device_color(app, 'b', 'peer', '#98cb65')
    assert load_config(tmp_path/'settings.json')['device_colors'] == {'a': {'peer': '#e383cf'}, 'b': {'peer': '#98cb65'}}


def test_cycle_excludes_previous_history_and_reset_boundary(tmp_path):
    db = Database(tmp_path/'cycle.sqlite')
    insert(db, 'previous-a', 10, tokens=1850)
    insert(db, 'previous-b', 20, device='b', tokens=688)
    insert(db, 'boundary', 100, tokens=999)
    insert(db, 'current-a', 101, tokens=800)
    insert(db, 'current-b', 102, device='b', model='m2', tokens=1480)
    with db.connect() as conn:
        conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('account',100,0,53,999,103,'reset')")
    result = chart_data(usage(db, 'account', 200), 'cycle', metric='tokens')
    assert result['devices'] == {'a': 800, 'b': 1480}
    assert chart_data(usage(db, 'account', 200), 'cycle', model='m2', metric='tokens')['total'] == 1480
    assert chart_data(usage(db, 'empty', 200), 'cycle', metric='tokens')['total'] == 0


def test_sync_time_requires_all_peer_receipts_and_uses_oldest_confirmation():
    from quota_guard.gui import sync_confirmed_at
    view = dict(sync_progress={'a': {'state': 'caught_up'}, 'b': {'state': 'caught_up'}},
                sync_receipts={'a': 100, 'b': 120})
    assert sync_confirmed_at(view) == 100
    view['sync_progress']['b']['state'] = 'syncing'
    assert sync_confirmed_at(view) is None
    view['sync_progress']['b']['state'] = 'caught_up'
    del view['sync_receipts']['b']
    assert sync_confirmed_at(view) is None
    assert sync_confirmed_at({}) is None


def test_formal_statistics_baseline_survives_replay_and_next_cycle(tmp_path):
    import hashlib
    db = Database(tmp_path/'formal.sqlite')
    account = 'a'*64
    ledger = Ledger(db)
    journal = Journal(db, ledger, 'a')
    for at, used, reset in [(100, 0, 1000), (200, 10, 1000), (1000, 0, 2000)]:
        journal.append(account, 'quota', dict(account=account, at=at, used=used, reset_at=reset), at)
    db.put('statistics_start:'+account, 1000)
    for ts, tokens in [(150, 9000), (1000, 7000), (1050, 1000)]:
        event = dict(id=hashlib.sha256(str(ts).encode()).hexdigest(), device='a', account=account,
                     ts=ts, model='m', tokens=tokens, weight=1, known=True)
        journal.append(account, 'events', [event], 1200)
    for window in ('total', 'hour', 'day', 'week', 'month', 'cycle'):
        assert chart_data(usage(db, account, 1500), window, metric='tokens')['total'] == 1000
    assert sum(ledger.history(account, 'a', 1500)['day'].values()) == 1000
    journal.project(account)
    journal.append(account, 'quota', dict(account=account, at=2000, used=0, reset_at=3000), 2000)
    insert(db, 'second', 2050, tokens=2000, account=account)
    result = usage(Database(tmp_path/'formal.sqlite'), account, 2500)
    assert chart_data(result, 'total', metric='tokens')['total'] == 3000
    assert chart_data(result, 'cycle', metric='tokens')['total'] == 2000
    assert result['statistics_start'] == 1000
    assert sum(ledger.history(account, 'a', 2500)['day'].values()) == 3000
    insert(db, 'other', 150, tokens=123, account='other')
    assert chart_data(usage(db, 'other', 2500), 'total', metric='tokens')['total'] == 123
    with db.connect() as connection:
        assert connection.execute('SELECT SUM(tokens) FROM events WHERE account=?', (account,)).fetchone()[0] == 19000


def test_two_minute_buckets_do_not_drift_between_refreshes(tmp_path):
    db = Database(tmp_path/'minutes.sqlite')
    minute = 4_000_080  # Exact two-minute boundary.
    insert(db, 'previous', minute-125, tokens=80)
    insert(db, 'current', minute+2, tokens=20)
    first = usage(db, 'account', minute+5)['windows']['hour']
    second = usage(db, 'account', minute+95)['windows']['hour']
    assert first == second
    assert first['start'] % 120 == 0 and first['step'] == 120 and first['count'] == 30
    insert(db, 'more', minute+100, tokens=30)
    current = chart_data(usage(db, 'account', minute+110), 'hour', metric='tokens')
    previous = chart_data(usage(db, 'account', minute+5), 'hour', metric='tokens')
    assert current['points'][:-1] == previous['points'][:-1]
    assert current['points'][-1] == 50
    next_minute = chart_data(usage(db, 'account', minute+121), 'hour', metric='tokens')
    assert next_minute['start'] == current['start']+120
    assert next_minute['points'][:-1] == current['points'][1:]
    assert next_minute['points'][-1] == 0


def test_hour_and_calendar_day_buckets_remain_fixed_within_their_unit(tmp_path):
    from datetime import datetime
    db = Database(tmp_path/'calendar.sqlite')
    now = datetime(2026, 9, 10, 12, 10, 5).timestamp()
    insert(db, 'sample', now-70, tokens=100)
    for window in ('day', 'week', 'month'):
        first = usage(db, 'account', now)['windows'][window]
        assert usage(db, 'account', now+20)['windows'][window] == first
        if window != 'day':
            start = datetime.fromtimestamp(first['start'])
            assert (start.hour, start.minute, start.second) == (0, 0, 0)


def test_hour_curve_retains_one_minute_samples_alongside_two_minute_bars(tmp_path):
    db = Database(tmp_path/'resolutions.sqlite')
    boundary = 4_000_080
    insert(db, 'first-minute', boundary+5, tokens=20)
    insert(db, 'second-minute', boundary+65, tokens=30)
    result = usage(db, 'account', boundary+90)
    curve = chart_data(result, 'hour_curve', metric='tokens')
    bars = chart_data(result, 'hour', metric='tokens')
    assert curve['step'] == 60 and len(curve['points']) == 60
    assert bars['step'] == 120 and len(bars['points']) == 30
    assert curve['points'][-2:] == [20, 30]
    assert bars['points'][-1] == 50
    assert curve['total'] == bars['total'] == 50
    later = usage(db, 'account', boundary+110)
    assert later['windows']['hour_curve'] == result['windows']['hour_curve']
    assert later['windows']['hour'] == result['windows']['hour']


def test_pie_today_uses_local_midnight_and_rolls_only_on_next_day(tmp_path):
    from datetime import datetime, timedelta
    db = Database(tmp_path/'today.sqlite')
    midnight = datetime(2026, 9, 10).astimezone()
    start = midnight.timestamp()
    insert(db, 'yesterday', start-1, tokens=900)
    insert(db, 'midnight', start, tokens=20)
    insert(db, 'morning', start+3600, tokens=30)
    first = usage(db, 'account', start+7200)['windows']['today']
    later = usage(db, 'account', start+8000)['windows']['today']
    assert first['start'] == later['start'] == start
    assert sum(r['tokens'] for r in first['rows']) == 50
    assert first['rows'] == later['rows']
    next_start = (midnight+timedelta(days=1)).timestamp()
    insert(db, 'next-midnight', next_start, tokens=70)
    following = usage(db, 'account', next_start+10)['windows']['today']
    assert following['start'] == next_start
    assert sum(r['tokens'] for r in following['rows']) == 70


def test_pie_recent_hour_and_six_hours_are_exact_rolling_windows(tmp_path):
    db = Database(tmp_path/'rolling-pie.sqlite')
    now = 4_000_000
    insert(db, 'before-six', now-21600-.01, tokens=900)
    insert(db, 'six-boundary', now-21600, tokens=80)
    insert(db, 'before-hour', now-3600-.01, tokens=40)
    insert(db, 'hour-boundary', now-3600, tokens=20)
    insert(db, 'recent', now-10, tokens=10)
    data = usage(db, 'account', now)
    assert data['windows']['pie_hour']['start'] == now-3600
    assert data['windows']['pie_six_hours']['start'] == now-21600
    assert chart_data(data, 'pie_hour', metric='tokens')['total'] == 30
    assert chart_data(data, 'pie_six_hours', metric='tokens')['total'] == 150
    later = usage(db, 'account', now+1)
    assert later['windows']['pie_hour']['start'] == now+1-3600
    assert chart_data(later, 'pie_hour', metric='tokens')['total'] == 10
    assert chart_data(later, 'pie_six_hours', metric='tokens')['total'] == 70


def test_official_increment_allocation_uses_weights_event_dates_and_reset_cycles(tmp_path):
    from datetime import datetime
    db = Database(tmp_path/'quota-chart.sqlite')
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with db.connect() as conn:
        first = conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',?,0,10,?,?, 'reset')", (midnight-100, midnight+1000, midnight+20)).lastrowid
        second = conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',?,0,4,?,?, 'reset')", (midnight+30, midnight+2000, midnight+60)).lastrowid
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (first,midnight-100,midnight+20,10))
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (second,midnight+30,midnight+60,4))
        for id,device,ts,tokens,weight in [('old','one',midnight-10,100,1),('new','two',midnight+10,100,3),('reset','one',midnight+40,90000000,2)]:
            conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',(id,device,'a',ts,'model',tokens,weight,1))
    data = usage(db,'a',midnight+100)
    rows=data['windows']['today']['quota_rows']
    assert data['windows']['today']['quota_ready']
    assert {r['device']:r['quota'] for r in rows} == {'two':7.5,'one':4}
    assert sum(r['quota'] for r in data['windows']['cycle']['quota_rows']) == 4
    assert sum(r['quota'] for r in data['windows']['total']['quota_rows']) == 14
    with db.connect() as conn:
        conn.execute("UPDATE events SET known=0 WHERE id='reset'")
    assert not usage(db,'a',midnight+100)['windows']['today']['quota_ready']


def test_pending_quota_is_distinct_from_zero_and_resolves_on_increment(tmp_path):
    db=Database(tmp_path/'pending.sqlite')
    with db.connect() as conn:
        epoch=conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',100,0,0,999,250,'reset')").lastrowid
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',('event','one','a',200,'model',100,1,1))
    window=usage(db,'a',300)['windows']['total']
    assert window['quota_ready']
    assert window['quota_rows']==[]
    assert window['quota_pending_rows']==[{'device':'one','model':'model','bucket':0}]
    with db.connect() as conn:
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)',(epoch,100,250,1))
    window=usage(db,'a',300)['windows']['total']
    assert window['quota_pending_rows']==[]
    assert window['quota_rows'][0]['quota']==1


def test_logged_tokens_update_live_with_estimate_then_reconcile_to_official_increment(tmp_path):
    db = Database(tmp_path/'live-estimate.sqlite')
    with db.connect() as conn:
        epoch = conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',100,0,2,999,200,'reset')").lastrowid
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (epoch,100,200,2))
        for id, device, ts, tokens, weight, known in [('sample','one',150,100,10,1), ('cached','one',250,1000000,2,1), ('output','two',260,100,6,1)]:
            conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', (id,device,'a',ts,'model',tokens,weight,known))
    window = usage(db,'a',300)['windows']['total']
    assert window['quota_pending_rows'] == []
    assert {r['device']:r['quota'] for r in window['quota_estimate_rows']} == pytest.approx({'one':.4,'two':1.2})
    assert sum(r['tokens'] for r in window['rows']) == 1000200
    assert sum(r['quota'] for r in window['quota_rows']) == 2
    with db.connect() as conn:
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (epoch,200,280,1))
    calibrated = usage(db,'a',300)['windows']['total']
    assert calibrated['quota_estimate_rows'] == calibrated['quota_pending_rows'] == []
    assert {r['device']:r['quota'] for r in calibrated['quota_rows']} == {'one':2.25,'two':0.75}


def test_live_quota_never_guesses_unknown_models_or_reuses_previous_cycle_samples(tmp_path):
    db = Database(tmp_path/'estimate-boundary.sqlite')
    with db.connect() as conn:
        epoch = conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',100,0,2,999,200,'reset')").lastrowid
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (epoch,100,200,2))
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('sample','one','a',150,'model',100,10,1))
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('unknown','two','a',250,'unknown',100,0,0))
    window = usage(db,'a',300)['windows']['total']
    assert window['quota_estimate_rows'] == []
    assert window['quota_pending_rows'] == [{'device':'two','model':'unknown','bucket':0}]
    with db.connect() as conn:
        conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',300,0,0,1999,300,'reset')")
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('new','one','a',350,'model',100,10,1))
    window = usage(db,'a',400)['windows']['cycle']
    assert window['quota_estimate_rows'] == []
    assert window['quota_pending_rows'] == [{'device':'one','model':'model','bucket':0}]


def test_cache_quota_is_discounted_part_of_total_and_missing_is_not_zero(tmp_path):
    db = Database(tmp_path/'cache.sqlite')
    with db.connect() as conn:
        epoch = conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',100,0,1,999,200,'reset')").lastrowid
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (epoch,100,200,1))
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('cached','one','a',150,'gpt-6-astra',1100,.1725,1))
        conn.execute('INSERT INTO event_details(id,input_tokens,cached_input_tokens,output_tokens) VALUES (?,?,?,?)', ('cached',1000,900,100))
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('fresh','one','a',250,'gpt-6-astra',1100,.1725,1))
        conn.execute('INSERT INTO event_details(id,input_tokens,cached_input_tokens,output_tokens) VALUES (?,?,?,?)', ('fresh',1000,900,100))
    window=usage(db,'a',300)['windows']['total']
    assert window['rows'][0]['cache_tokens'] == 1800
    assert window['rows'][0]['detail_missing'] == 0
    assert window['quota_estimate_rows'][0]['quota'] == pytest.approx(1)
    assert window['quota_estimate_rows'][0]['cache_quota'] == pytest.approx(22500/172500)
    assert window['quota_rows'][0]['quota'] == 1
    assert abs(window['quota_rows'][0]['cache_quota']-22500/172500)<1e-10
    with db.connect() as conn:
        conn.execute("DELETE FROM event_details WHERE id='fresh'")
    window=usage(db,'a',300)['windows']['total']
    assert window['rows'][0]['detail_missing'] == 1100
    assert window['quota_estimate_rows'][0]['quota'] == pytest.approx(1)
    assert window['quota_estimate_rows'][0]['cache_quota'] is None
    with db.connect() as conn:
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (epoch,200,280,1))
    window=usage(db,'a',300)['windows']['total']
    assert window['quota_rows'][0]['cache_quota'] is None


def test_estimator_uses_every_confirmed_sample_in_current_cycle(tmp_path):
    db = Database(tmp_path/'cumulative-estimate.sqlite')
    with db.connect() as conn:
        epoch = conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',100,0,15,9999,700,'reset')").lastrowid
        for index, delta in enumerate([10,1,1,1,1,1]):
            start, end = 100+index*100, 200+index*100
            conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (epoch,start,end,delta))
            conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                         (f'sample-{index}','one','a',end-10,'model',100,10,1))
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('pending','one','a',750,'model',100,4,1))
    data = usage(db,'a',800)
    assert data['quota_estimate']['estimate_samples'] == 6
    assert data['quota_estimate']['estimate_pending'] == pytest.approx(1)
    assert data['quota_estimate']['remaining_estimate'] == pytest.approx(84)


def test_hour_panning_reads_past_tokens_with_later_official_calibration(tmp_path):
    db=Database(tmp_path/'pan.sqlite')
    with db.connect() as conn:
        epoch=conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',100,0,1,9999,5000,'reset')").lastrowid
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)',(epoch,100,5000,1))
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',('past','one','a',2000,'gpt-6-astra',100,1,1))
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',('now','one','a',7900,'gpt-6-astra',200,2,1))
    live=usage(db,'a',8000)
    past=usage(db,'a',8000,hour_end=4000)
    assert sum(r['tokens'] for r in live['windows']['hour_curve']['rows'])==200
    assert sum(r['tokens'] for r in past['windows']['hour_curve']['rows'])==100
    assert sum(r['quota'] for r in past['windows']['hour_curve']['quota_rows'])==1
    assert past['windows']['hour_curve']['quota_pending_rows']==[]
    assert past['windows']['today']==live['windows']['today']
    assert usage(db,'a',8000)==live


def test_hour_history_buffer_keeps_neighbor_buckets_without_expanding_live_view(tmp_path):
    db=Database(tmp_path/'hour-buffer.sqlite')
    with db.connect() as conn:
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',('older','one','a',1000,'gpt-6-astra',100,1,1))
        conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',('recent','one','a',7900,'gpt-6-astra',200,2,1))
    buffered=usage(db,'a',8000,hour_end=8000,hour_buffer=True)['windows']['hour_curve']
    live=usage(db,'a',8000)['windows']['hour_curve']
    assert buffered['count']==1560 and buffered['step']==60
    assert sum(row['tokens'] for row in buffered['rows'])==300
    assert live['count']==60 and sum(row['tokens'] for row in live['rows'])==200


def test_day_history_has_hourly_records_and_official_quota_without_changing_live_windows(tmp_path):
    db=Database(tmp_path/'day-buffer.sqlite')
    now=40*86400
    with db.connect() as conn:
        epoch=conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('a',0,0,1,9999999,?,'reset')",(now,)).lastrowid
        conn.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)',(epoch,0,now,1))
        for ident,account,at,tokens in [('old','a',now-20*86400,100),('live','a',now-100,200),('foreign','b',now-20*86400,300),('too-old','a',now-35*86400,400)]:
            conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',(ident,'one',account,at,'gpt-6-astra',tokens,1,1))
    live=usage(db,'a',now)
    buffered=usage(db,'a',now,day_end=now,day_buffer=True)
    window=buffered['windows']['day']
    assert window['count']==32*24 and window['step']==3600
    assert sum(row['tokens'] for row in window['rows'])==300
    assert min(row['first_at'] for row in window['rows'])==now-20*86400
    assert sum(row['quota'] for row in window['quota_rows'])==2/3
    assert live['windows']['day']['count']==24
    assert sum(row['tokens'] for row in live['windows']['day']['rows'])==200
    assert buffered['windows']['hour_curve']==live['windows']['hour_curve']
    assert usage(db,'a',now)==live
