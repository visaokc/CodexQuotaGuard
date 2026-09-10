"""Read-only, account-scoped rolling usage from the synchronized event ledger."""
import json
import time
from datetime import datetime, timezone


WINDOWS = {'cycle': (None, 1), 'total': (None, 1), 'today': (None, 1), 'pie_hour': (3600, 1), 'pie_six_hours': (21600, 1), 'hour': (3600, 30), 'hour_curve': (3600, 60), 'day': (86400, 24),
           'week': (7 * 86400, 7), 'month': (30 * 86400, 30)}


def quota_display(used, cap, personal=False):
    if not personal:
        return used, cap
    return (used/cap*100 if cap > 0 else None), 100



def quota_events(db, account, now):
    """Allocate observed official increments to event times, never raw-token capacity."""
    events = list(db.execute('SELECT * FROM events WHERE account=? AND ts<=? ORDER BY ts', (account, now)))
    segments = list(db.execute('SELECT s.* FROM segments s JOIN epochs e ON e.id=s.epoch WHERE e.account=? AND s.end<=? ORDER BY s.end', (account, now)))
    result, gaps, index = [], [], 0
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
            result.append(dict(device=row['device'], model=row['model'], ts=row['ts'],
                               quota=segment['delta']*row['weight']/total))
    return result, gaps


def usage(database, account, now=None):
    now = time.time() if now is None else now
    result = {'account': account, 'at': now, 'windows': {}, 'models': []}
    models = set()
    with database.connect() as db:
        saved = db.execute('SELECT value FROM meta WHERE key=?', ('statistics_start:'+account,)).fetchone()
        baseline = float(json.loads(saved[0])) if saved else 0
        result['statistics_start'] = baseline
        epoch = db.execute('SELECT started FROM epochs WHERE account=? ORDER BY id DESC LIMIT 1',
                           (account,)).fetchone()
        result['cycle_start'] = epoch['started'] if epoch else None
        for name, (duration, count) in WINDOWS.items():
            if name == 'cycle':
                # Match the ledger's cycle boundary, including accepted clock-skew events.
                rows = db.execute('''SELECT device, model, 0 AS bucket, SUM(tokens) AS tokens,
                    SUM(weight) AS weight, SUM(CASE WHEN known=0 THEN tokens ELSE 0 END) AS unknown
                    FROM events WHERE account=? AND ts>? GROUP BY device, model ORDER BY device, model''',
                    (account, result['cycle_start'])).fetchall() if epoch else []
                result['windows'][name] = dict(start=result['cycle_start'] or now, step=max(1, now-(result['cycle_start'] or now)),
                                               count=1, rows=[dict(row) for row in rows])
                models.update(row['model'] for row in rows)
                continue
            step = duration/count if duration is not None else max(1, now)
            if name in ('pie_hour', 'pie_six_hours'):
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
                    aligned = (now//step)*step
                # Keep stable calendar buckets, including the current partial one.
                start = aligned-(count-1)*step
            rows = db.execute('''SELECT device, model,
                CAST((ts-?)/? AS INTEGER) AS bucket,
                SUM(tokens) AS tokens, SUM(weight) AS weight,
                SUM(CASE WHEN known=0 THEN tokens ELSE 0 END) AS unknown
                FROM events WHERE account=? AND ts>=? AND ts<? AND ts>?
                GROUP BY device, model, bucket ORDER BY device, model, bucket''',
                (start, step, account, start, now, baseline)).fetchall()
            result['windows'][name] = dict(start=start, step=step, count=count,
                                           rows=[dict(row) for row in rows])
            models.update(row['model'] for row in rows)
        allocated, gaps = quota_events(db, account, now)
        last_increment = db.execute('SELECT MAX(s.end) FROM segments s JOIN epochs e ON e.id=s.epoch WHERE e.account=? AND s.end<=?', (account, now)).fetchone()[0] or 0
        waiting = list(db.execute('SELECT device,model,ts FROM events WHERE account=? AND ts>? AND ts<=? AND tokens>0', (account, last_increment, now)))

        observed = db.execute('SELECT 1 FROM epochs WHERE account=? LIMIT 1', (account,)).fetchone() is not None
        pending = db.execute('SELECT 1 FROM meta WHERE key=?', ('reset_candidate:'+account,)).fetchone() is not None
        for name, window in result['windows'].items():
            start, step = max(baseline, window['start']), window['step']
            grouped = {}
            for row in allocated:
                if row['ts'] < start or row['ts'] >= now or row['ts'] <= baseline:
                    continue
                if name == 'cycle' and row['ts'] <= window['start']:
                    continue
                bucket = min(window['count']-1, int((row['ts']-window['start'])/step))
                key = (row['device'], row['model'], bucket)
                grouped[key] = grouped.get(key, 0.) + row['quota']
            waiting_keys = {(r['device'], r['model'], min(window['count']-1, int((r['ts']-window['start'])/step)))
                for r in waiting if r['ts'] >= start and r['ts'] < now and r['ts'] > baseline
                and (name != 'cycle' or r['ts'] > window['start'])}
            window['quota_pending_rows'] = [dict(device=d, model=m, bucket=b) for d, m, b in sorted(waiting_keys)]
            window['quota_rows'] = [dict(device=d, model=m, bucket=b, quota=q) for (d, m, b), q in grouped.items()]
            window['quota_ready'] = observed and not pending and not any(end > start and begin < now for begin, end in gaps)
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
