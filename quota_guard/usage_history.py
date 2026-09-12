"""Read-only statistical ranges; archiving never changes the financial ledger."""
import json


def archive_boundary(database, rules, now):
    declaration = ((rules or {}).get('policy') or {}).get('clean_start')
    if not declaration:
        return None
    from .shared_policy import contiguous
    with database.connect() as db:
        vector = contiguous(db, declaration['account'])
        hits = []
        for row in db.execute("SELECT origin,seq,payload FROM facts WHERE account=? AND kind='quota' AND ts>=? AND ts<=?",
                              (declaration['account'], declaration['started'], now)):
            value = json.loads(row['payload'])
            if (row['seq'] <= vector.get(row['origin'], 0) and value['used'] >= 100
                    and value['at'] <= now and abs(value['reset_at']-declaration['reset_at']) <= 120):
                hits.append(value['at'])
    return min(hits) if hits else None


def archive_end(database, rules, now):
    """Include late old-cycle logs in account1's archive until its real reset."""
    declaration = rules['policy']['clean_start']
    with database.connect() as db:
        row = db.execute('''SELECT MIN(started) FROM epochs WHERE account=? AND started>?
            AND ABS(reset_at-?)>120 AND started<=?''',
            (declaration['account'], declaration['started'], declaration['reset_at'], now)).fetchone()
    return row[0] if row[0] is not None else now


def range_window(database, ranges, rules=None, attributed=None, estimates=None):
    from .shared_view import person_rows
    result = dict(start=min((r['start'] for r in ranges), default=0), step=1, count=1,
                  rows=[], quota_rows=[], quota_pending_rows=[], quota_ready=False)
    if estimates is not None:
        result['quota_estimate_rows'] = []
    with database.connect() as db:
        for span in ranges:
            rows = db.execute('''SELECT device, model, 0 AS bucket, SUM(tokens) AS tokens,
                SUM(weight) AS weight, SUM(CASE WHEN known=0 THEN tokens ELSE 0 END) AS unknown,
                COUNT(*) AS event_count, COUNT(input_tokens) AS detail_count,
                MIN(ts) AS first_at, MAX(ts) AS last_at, SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens, SUM(reasoning_output_tokens) AS reasoning_tokens,
                COUNT(reasoning_output_tokens) AS reasoning_count,
                SUM(CASE WHEN reasoning_output_tokens IS NULL THEN COALESCE(output_tokens,0) ELSE 0 END) AS reasoning_missing,
                SUM(cached_input_tokens) AS cache_tokens,
                SUM(CASE WHEN input_tokens IS NULL THEN tokens ELSE 0 END) AS detail_missing
                FROM events LEFT JOIN event_details USING(id)
                WHERE account=? AND ts>=? AND ts<=? AND ts>?
                GROUP BY device, model ORDER BY device, model''',
                (span['account'], span['start'], span['end'], span.get('after', -1))).fetchall()
            for row in rows:
                value = dict(row, account=span['account'])
                result['rows'].extend(person_rows(database, value, rules, span['end'], share_tokens=True) if rules and rules.get('policy') else [value])
    if attributed is None:
        return result
    result['quota_ready'] = bool(attributed['epochs'])
    def included(event):
        return any(r['account'] == event['account'] and r['start'] <= event['ts'] <= r['end']
                   and event['ts'] > r.get('after', -1) for r in ranges)

    def grouped_events(events):
        grouped = {}
        for event in events:
            if not included(event):
                continue
            key = event['account'], event['device'], event['model']
            row = grouped.setdefault(key, dict(account=key[0], device=key[1], model=key[2], bucket=0, quota=0., cache_quota=0., shared_quota=0.))
            row['quota'] += event['quota']
            if event.get('shared_cost'):
                row['shared_quota'] += event['quota']
            row['cache_quota'] = row['cache_quota']+event['cache_quota'] if row['cache_quota'] is not None and event['cache_quota'] is not None else None
        return list(grouped.values())

    result['quota_rows'] = grouped_events(attributed['events'])
    uncovered = None
    if estimates is not None:
        from .maintenance import cost_policy
        from .shared_policy import PERSONS, person_for
        confirmed_ids = {e['id'] for e in attributed['events']}
        estimates = [e for e in estimates if e['id'] not in confirmed_ids]
        result['quota_estimate_rows'] = grouped_events(estimates)
        covered = {(e['account'], e['device'], e['id'].rsplit(':', 1)[0] if e.get('shared_cost') else e['id'])
                   for e in [*attributed['events'], *estimates]}
        costs = cost_policy(rules).get('shared_costs', []) if rules and rules.get('policy') else []
        baselines = [(e['account'], cycle['started'], e['ts']) for e in attributed['events'] if e.get('baseline')
                     for cycle in attributed['epochs'].get(e['account'], [])
                     if cycle['started'] <= e['ts'] and (cycle['ended'] is None or e['ts'] < cycle['ended'])]
        uncovered = set()
        with database.connect() as db:
            for span in ranges:
                for event in db.execute('''SELECT * FROM events WHERE account=? AND ts>=? AND ts<=? AND ts>? AND tokens>0''',
                        (span['account'], span['start'], span['end'], span.get('after', -1))):
                    person = person_for(rules, event['device'], event['ts']) if rules and rules.get('policy') else event['device']
                    person = person or event['device']
                    recipients = PERSONS if any(c['account'] == event['account'] and c['person'] == person
                        and c['since'] < event['ts'] <= c['through'] for c in costs) else [person]
                    for recipient in recipients:
                        if ((event['account'], recipient, event['id']) not in covered
                                and not any(a == event['account'] and start <= event['ts'] <= end for a, start, end in baselines)):
                            uncovered.add((event['account'], recipient, event['model']))
    for row in result['rows']:
        last = max([s['end'] for s in attributed['streams'] if s['account'] == row['account'] and s['ready']]
                   + [e['ts'] for e in attributed['events'] if e['account'] == row['account'] and e.get('baseline')], default=0)
        pending = row['last_at'] > last or any(g['account'] == row['account'] and g['end'] >= row['first_at']
                                       and g['start'] < row['last_at'] for g in attributed['gaps'])
        if pending and (uncovered is None or (row['account'], row['device'], row['model']) in uncovered):
            result['quota_pending_rows'].append({k: row[k] for k in ('account', 'device', 'model', 'bucket')})
    return result


def donut_windows(database, analytics, rules, attributed, cutoff):
    result = {}
    archived_account = ((rules or {}).get('policy') or {}).get('clean_start', {}).get('account')
    archived_until = archive_end(database, rules, analytics['at']) if cutoff is not None else -1
    def boundary(account):
        return archived_until if account == archived_account else -1
    for name in ('cycle', 'today', 'pie_hour', 'pie_six_hours', 'pie_twelve_hours', 'week', 'month', 'total'):
        source = analytics['windows'][name]
        if name == 'cycle':
            ranges = [dict(account=c['account'], start=max(c['started'], boundary(c['account'])), end=c['matched_until'],
                           after=max(c['started'], boundary(c['account']))) for c in analytics['cycle_pair']['cycles']]
        else:
            ranges = [dict(account=a, start=max(source['start'], boundary(a)), end=analytics['at'], after=boundary(a))
                      for a in analytics['account_ids']]
        result[name] = range_window(database, ranges, rules, attributed,
                                    estimates=analytics.get('live_reporting', {}).get('events', []))
    return result
