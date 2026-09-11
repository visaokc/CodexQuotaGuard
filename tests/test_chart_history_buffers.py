"""Historical chart buffers preserve aligned buckets, token counts and quota scope."""
import math

import pytest

from quota_guard.analytics import usage
from quota_guard.storage import Database


CASES = [
    ('hour_curve', 60, 26*60, dict(hour_buffer=True)),
    ('hour', 120, 26*30, dict(hour_buffer=True)),
    ('six_hours', 300, 30*12, dict(rolling_period='six_hours', rolling_buffer=True)),
    ('twelve_hours', 300, 36*12, dict(rolling_period='twelve_hours', rolling_buffer=True)),
    ('day', 3600, 32*24, dict(day_buffer=True)),
]


def request_options(window, end, options):
    key = 'day_end' if window == 'day' else 'hour_end' if window in ('hour', 'hour_curve') else 'rolling_end'
    return dict(options, **{key: end})


@pytest.mark.parametrize('window,step,count,options', CASES)
def test_history_buffer_boundaries_keep_tokens_cache_and_confirmed_quota(tmp_path, window, step, count, options):
    database = Database(tmp_path/'history.sqlite')
    now, end = 1_800_000_123, 1_800_000_123-4*3600-23
    start = math.floor(end/step)*step-(count-1)*step
    times = [start-1, start, start+step-.1, start+step, end-.1, end, now-1, now+1]
    with database.connect() as db:
        epoch = db.execute('INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES (?,?,?,?,?,?,?)',
                           ('a', start-100, 0, 14, now+86400, now, 'natural')).lastrowid
        db.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (epoch,start-100,now,14))
        for index, at in enumerate(times):
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                       (str(index),'member','a',at,'gpt-6-astra',100+index,index+1,True))
            db.execute('INSERT INTO event_details(id,input_tokens,cached_input_tokens,output_tokens) VALUES (?,?,?,?)',
                       (str(index),80+index,60,20))
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('foreign','member','b',start+10,'gpt-6-astra',99999,1,True))
    live = usage(database,'a',now)
    buffered = usage(database,'a',now,**request_options(window,end,options))
    source = buffered['windows'][window]
    assert (source['start'],source['step'],source['count']) == (start,step,count)
    included = [i for i,at in enumerate(times) if start <= at < end]
    assert sum(row['tokens'] for row in source['rows']) == sum(100+i for i in included)
    assert sum(row['cache_tokens'] for row in source['rows']) == 60*len(included)
    assert sum(row['event_count'] for row in source['rows']) == len(included)
    assert all(0 <= row['bucket'] < count for row in source['rows']+source['quota_rows'])
    assert min(row['first_at'] for row in source['rows']) == start
    assert max(row['last_at'] for row in source['rows']) == end-.1
    total_weight = sum(i+1 for i,at in enumerate(times) if at <= now)
    assert sum(row['quota'] for row in source['quota_rows']) == pytest.approx(14*sum(i+1 for i in included)/total_weight)
    assert source['quota_available'] and source['quota_ready'] and not source['quota_gaps']
    assert source['quota_pending_rows'] == []
    # Panning one period never moves the live pie, totals or other periods.
    changed = {'hour','hour_curve'} if window in ('hour','hour_curve') else {window}
    for name, value in live['windows'].items():
        if name not in changed:
            assert buffered['windows'][name] == value


@pytest.mark.parametrize('window,duration,count', [('six_hours',21600,72),('twelve_hours',43200,144)])
def test_rolling_period_can_pan_without_buffer_and_clamps_future_end(tmp_path, window, duration, count):
    database = Database(tmp_path/'rolling.sqlite')
    now = 1_800_000_123
    with database.connect() as db:
        for event,at,tokens in [('old',now-duration-3600,100),('recent',now-60,200)]:
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', (event,'member','a',at,'gpt-6-astra',tokens,1,True))
    live = usage(database,'a',now)['windows'][window]
    past = usage(database,'a',now,rolling_period=window,rolling_end=now-2*3600)['windows'][window]
    future = usage(database,'a',now,rolling_period=window,rolling_end=now+2*3600)['windows'][window]
    assert past['step'] == 300 and past['count'] == count
    assert sum(row['tokens'] for row in past['rows']) == 100
    assert sum(row['tokens'] for row in live['rows']) == 200
    assert future == live


@pytest.mark.parametrize('window,duration', [('six_hours',21600),('twelve_hours',43200)])
def test_rolling_buffer_retains_confirmation_gaps_and_pending_tokens(tmp_path, window, duration):
    database = Database(tmp_path/'gaps.sqlite')
    now = 1_800_000_123
    with database.connect() as db:
        epoch = db.execute('INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES (?,?,?,?,?,?,?)',
                           ('a',now-86400,0,1,now+86400,now-3600,'natural')).lastrowid
        db.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)', (epoch,now-7200,now-3600,1))
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('pending','member','a',now-30,'gpt-6-astra',200,1,True))
    value = usage(database,'a',now,rolling_period=window,rolling_end=now,rolling_buffer=True)['windows'][window]
    assert value['count']*value['step'] == 86400+duration
    assert value['quota_available'] and not value['quota_ready']
    assert value['quota_gaps'] == [dict(start=now-7200,end=now-3600)]
    assert value['quota_pending_rows'][0]['bucket'] == value['rows'][0]['bucket']
    assert value['quota_estimate_rows'] == []
