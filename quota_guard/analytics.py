"""Read-only, account-scoped rolling usage from the synchronized event ledger."""
import json
import time
from datetime import datetime, timezone

from .meter import RATES


WINDOWS = {'cycle': (None, 1), 'total': (None, 1), 'today': (None, 1), 'pie_hour': (3600, 1), 'pie_six_hours': (21600, 1), 'pie_twelve_hours': (43200, 1), 'hour': (3600, 30), 'hour_curve': (3600, 60), 'six_hours': (21600, 72), 'twelve_hours': (43200, 144), 'day': (86400, 24),
           'week': (7 * 86400, 7), 'month': (30 * 86400, 30)}


def quota_display(used, cap, personal=False):
    if not personal:
        return used, cap
    return (used/cap*100 if cap > 0 else None), 100



def quota_events(db, account, now):
    """Allocate observed official increments to event times, never raw-token capacity."""
    events = list(db.execute('SELECT e.*,d.input_tokens,d.cached_input_tokens,d.output_tokens FROM events e LEFT JOIN event_details d ON d.id=e.id WHERE account=? AND ts<=? ORDER BY ts', (account, now)))
    segments = list(db.execute('SELECT s.* FROM segments s JOIN epochs e ON e.id=s.epoch WHERE e.account=? AND s.end<=? ORDER BY s.end', (account, now)))
    result, gaps, samples, index = [], [], {}, 0
    for segment in segments:
        rows = []
        while index < len(events) and events[index]['ts'] <= segment['end']:
            event = events[index]
            if event['ts'] > segment['start'] and event['tokens'] > 0:
                rows.append(event)
            index += 1
        total = sum(row['weight'] for row in rows)
        if not rows or total <= 0 or any(not row['known'] for row in rows):
            gaps.append((segment['start'], segment['end']))
            continue
        for row in rows:
            quota = segment['delta']*row['weight']/total
            fraction = cache_fraction(row)
            result.append(dict(device=row['device'], model=row['model'], ts=row['ts'],
                               quota=quota, cache_quota=quota*fraction if fraction is not None else None))
        samples.setdefault(segment['epoch'], []).append((segment['delta'], total))
    # Recent official increments calibrate weighted usage, not raw Token counts.
    rates = {epoch: sum(delta for delta, _ in rows[-5:])/sum(weight for _, weight in rows[-5:])
             for epoch, rows in samples.items()}
    return result, gaps, rates


def cache_fraction(row):
    """Cache is a discounted part of input, not additional input or output."""
    rate = RATES.get(row['model'])
    if row['input_tokens'] is None or rate is None:
        return None
    cached = row['cached_input_tokens']*rate[1]
    total = (row['input_tokens']-row['cached_input_tokens'])*rate[0]+cached+row['output_tokens']*rate[2]
    return cached/total if total > 0 else 0.


def add_quota(grouped, key, quota, cached):
    old, old_cached = grouped.get(key, (0., 0.))
    grouped[key] = (old+quota, None if cached is None or old_cached is None else old_cached+cached)


def usage(database, account, now=None, hour_end=None, hour_buffer=False, day_end=None, day_buffer=False,
          rolling_period=None, rolling_end=None, rolling_buffer=False):
    now = time.time() if now is None else now
    result = {'account': account, 'at': now, 'windows': {}, 'models': []}
    models = set()
    with database.connect() as db:
        saved = db.execute('SELECT value FROM meta WHERE key=?', ('statistics_start:'+account,)).fetchone()
        baseline = float(json.loads(saved[0])) if saved else 0
        result['statistics_start'] = baseline
        epoch = db.execute('SELECT id, started FROM epochs WHERE account=? ORDER BY id DESC LIMIT 1',
                           (account,)).fetchone()
        result['cycle_start'] = epoch['started'] if epoch else None
        for name, (duration, count) in WINDOWS.items():
            if hour_buffer and name in ('hour', 'hour_curve'):
                duration *= 26
                count *= 26
            if day_buffer and name == 'day':
                duration *= 32
                count *= 32
            if rolling_buffer and name == rolling_period and name in ('six_hours', 'twelve_hours'):
                duration += 86400
                count += 288
            until = min(now,hour_end) if hour_end is not None and name in ('hour','hour_curve') else now
            if day_end is not None and name == 'day':
                until = min(now,day_end)
            if rolling_end is not None and name == rolling_period and name in ('six_hours', 'twelve_hours'):
                until = min(now,rolling_end)
            if name == 'cycle':
                # Match the ledger's cycle boundary, including accepted clock-skew events.
                rows = db.execute('''SELECT device, model, 0 AS bucket, SUM(tokens) AS tokens,
                    SUM(weight) AS weight, SUM(CASE WHEN known=0 THEN tokens ELSE 0 END) AS unknown, COUNT(*) AS event_count, COUNT(input_tokens) AS detail_count, MIN(ts) AS first_at, MAX(ts) AS last_at, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(reasoning_output_tokens) AS reasoning_tokens, COUNT(reasoning_output_tokens) AS reasoning_count, SUM(CASE WHEN reasoning_output_tokens IS NULL THEN COALESCE(output_tokens,0) ELSE 0 END) AS reasoning_missing,
                    SUM(cached_input_tokens) AS cache_tokens, SUM(CASE WHEN input_tokens IS NULL THEN tokens ELSE 0 END) AS detail_missing
                    FROM events LEFT JOIN event_details USING(id) WHERE account=? AND ts>? GROUP BY device, model ORDER BY device, model''',
                    (account, result['cycle_start'])).fetchall() if epoch else []
                result['windows'][name] = dict(start=result['cycle_start'] or now, step=max(1, now-(result['cycle_start'] or now)),
                                               count=1, rows=[dict(row) for row in rows])
                models.update(row['model'] for row in rows)
                continue
            step = duration/count if duration is not None else max(1, now)
            if name in ('pie_hour', 'pie_six_hours', 'pie_twelve_hours'):
                start = now-duration
            elif name == 'today':
                local = datetime.fromtimestamp(now, timezone.utc).astimezone()
                start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                step = max(1, now-start)
            elif duration is None:
                start = baseline
            else:
                if name in ('week', 'month'):
                    local = datetime.fromtimestamp(now, timezone.utc).astimezone()
                    aligned = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                else:
                    aligned = (until//step)*step
                # Keep stable calendar buckets, including the current partial one.
                start = aligned-(count-1)*step
            rows = db.execute('''SELECT device, model,
                CAST((ts-?)/? AS INTEGER) AS bucket,
                SUM(tokens) AS tokens, SUM(weight) AS weight,
                SUM(CASE WHEN known=0 THEN tokens ELSE 0 END) AS unknown, COUNT(*) AS event_count, COUNT(input_tokens) AS detail_count, MIN(ts) AS first_at, MAX(ts) AS last_at, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(reasoning_output_tokens) AS reasoning_tokens, COUNT(reasoning_output_tokens) AS reasoning_count, SUM(CASE WHEN reasoning_output_tokens IS NULL THEN COALESCE(output_tokens,0) ELSE 0 END) AS reasoning_missing,
                SUM(cached_input_tokens) AS cache_tokens, SUM(CASE WHEN input_tokens IS NULL THEN tokens ELSE 0 END) AS detail_missing
                FROM events LEFT JOIN event_details USING(id) WHERE account=? AND ts>=? AND ts<? AND ts>?
                GROUP BY device, model, bucket ORDER BY device, model, bucket''',
                (start, step, account, start, until, baseline)).fetchall()
            result['windows'][name] = dict(start=start, step=step, count=count,
                                           rows=[dict(row) for row in rows])
            models.update(row['model'] for row in rows)
        allocated, gaps, _ = quota_events(db, account, now)
        last_increment = db.execute('SELECT MAX(s.end) FROM segments s JOIN epochs e ON e.id=s.epoch WHERE e.account=? AND s.end<=?', (account, now)).fetchone()[0] or 0
        waiting = list(db.execute('SELECT e.*,d.input_tokens,d.cached_input_tokens,d.output_tokens FROM events e LEFT JOIN event_details d ON d.id=e.id WHERE account=? AND ts>? AND ts<=? AND tokens>0', (account, last_increment, now)))

        observed = db.execute('SELECT 1 FROM epochs WHERE account=? LIMIT 1', (account,)).fetchone() is not None
        pending = db.execute('SELECT 1 FROM meta WHERE key=?', ('reset_candidate:'+account,)).fetchone() is not None
        for name, window in result['windows'].items():
            until = min(now,hour_end) if hour_end is not None and name in ('hour','hour_curve') else now
            if day_end is not None and name == 'day':
                until = min(now,day_end)
            if rolling_end is not None and name == rolling_period and name in ('six_hours', 'twelve_hours'):
                until = min(now,rolling_end)
            start, step = max(baseline, window['start']), window['step']
            grouped = {}
            for row in allocated:
                if row['ts'] < start or row['ts'] >= until or row['ts'] <= baseline:
                    continue
                if name == 'cycle' and row['ts'] <= window['start']:
                    continue
                bucket = min(window['count']-1, int((row['ts']-window['start'])/step))
                key = (row['device'], row['model'], bucket)
                add_quota(grouped, key, row['quota'], row['cache_quota'])
            waiting_keys = set()
            for row in waiting:
                if row['ts'] < start or row['ts'] >= until or row['ts'] <= baseline:
                    continue
                if name == 'cycle' and row['ts'] <= window['start']:
                    continue
                key = (row['device'], row['model'], min(window['count']-1, int((row['ts']-window['start'])/step)))
                waiting_keys.add(key)
            # Log counts continue immediately. Quota changes only after an
            # observed official increment; never extrapolate unconfirmed usage.
            window['quota_estimate_rows'] = []
            window['quota_pending_rows'] = [dict(device=d, model=m, bucket=b) for d, m, b in sorted(waiting_keys)]
            window['quota_rows'] = [dict(device=d, model=m, bucket=b, quota=q, cache_quota=c) for (d, m, b), (q,c) in grouped.items()]
            window['quota_ready'] = observed and not pending and not any(end > start and begin < until for begin, end in gaps)
            if ((hour_buffer and name in ('hour','hour_curve')) or (day_buffer and name == 'day')
                    or (rolling_buffer and name == rolling_period and name in ('six_hours', 'twelve_hours'))):
                window['quota_available'] = observed and not pending
                window['quota_gaps'] = [dict(start=begin,end=end) for begin,end in gaps if end > start and begin < until]
    result['models'] = sorted(models)
    return result


def chart_data(snapshot, window='day', model=None, metric='weight', device=None):
    """Unknown model weights fall back to Token for the complete selected window."""
    source = snapshot.get('windows', {}).get(window, {})
    rows = [row for row in source.get('rows', [])
            if (model is None or row['model'] == model) and (device is None or row['device'] == device)]
    unknown = sum(row['unknown'] for row in rows)
    effective = 'tokens' if metric == 'weight' and unknown else metric
    points = [0.0] * source.get('count', WINDOWS[window][1])
    devices = {}
    for row in rows:
        value = row[effective]
        points[row['bucket']] += value
        devices[row['device']] = devices.get(row['device'], 0) + value
    total = sum(points)
    return dict(points=points, devices=devices, total=total, unknown=unknown,
                metric=effective, start=source.get('start', 0), step=source.get('step', 1),
                shares={device: value / total * 100 if total else 0
                        for device, value in devices.items()})
