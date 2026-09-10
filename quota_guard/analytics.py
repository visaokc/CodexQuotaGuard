"""Read-only, account-scoped rolling usage from the synchronized event ledger."""
import time


WINDOWS = {'cycle': (None, 1), 'total': (None, 1), 'hour': (3600, 1), 'day': (86400, 24),
           'week': (7 * 86400, 28), 'month': (30 * 86400, 30)}


def quota_display(used, cap, personal=False):
    if not personal:
        return used, cap
    return (used/cap*100 if cap > 0 else None), 100


def usage(database, account, now=None):
    now = time.time() if now is None else now
    result = {'account': account, 'at': now, 'windows': {}, 'models': []}
    models = set()
    with database.connect() as db:
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
            start = now-duration if duration is not None else 0
            step = duration/count if duration is not None else max(1, now)
            rows = db.execute('''SELECT device, model,
                CAST((ts-?)/? AS INTEGER) AS bucket,
                SUM(tokens) AS tokens, SUM(weight) AS weight,
                SUM(CASE WHEN known=0 THEN tokens ELSE 0 END) AS unknown
                FROM events WHERE account=? AND ts>=? AND ts<?
                GROUP BY device, model, bucket ORDER BY device, model, bucket''',
                (start, step, account, start, now)).fetchall()
            result['windows'][name] = dict(start=start, step=step, count=count,
                                           rows=[dict(row) for row in rows])
            models.update(row['model'] for row in rows)
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
