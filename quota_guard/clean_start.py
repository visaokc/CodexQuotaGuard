"""An explicit shared-group settlement; raw Token and quota facts stay intact."""
import json

from .pool_accounting import units
from .shared_policy import contiguous


def prepare(database, rules, attributed, now):
    policy = rules.get('policy') or {}
    declaration = policy.get('clean_start')
    if not declaration:
        return
    baseline = declaration['baseline']
    baseline_cycle = next((c for c in attributed['epochs'].get(baseline['account'], [])
                           if abs(c['reset_at']-baseline['reset_at']) <= 120), None)
    baseline_start = baseline_cycle['started'] if baseline_cycle else baseline['started']
    if baseline_cycle:
        attributed['overrides'][baseline['account'], baseline_start] = 'natural'
    account = declaration['account']
    hit = declaration.get('at') if declaration.get('trigger') == 'immediate' else None
    cache_quota = None
    with database.connect() as db:
        from .shared_quota import weight
        from .sample_pool import sample_checkpoints
        observed = [json.loads(row[0]) for row in db.execute("SELECT payload FROM facts WHERE account=? AND kind='quota' AND ts<=? ORDER BY ts",
                                                           (baseline['account'], baseline['at']))]
        cutoff = min((row['at'] for row in observed if row['used'] == baseline['used']
                      and abs(row['reset_at']-baseline['reset_at']) <= 120), default=baseline['at'])
        rows = list(db.execute('''SELECT e.*,d.input_tokens,d.cached_input_tokens,d.output_tokens FROM events e
            LEFT JOIN event_details d USING(id) WHERE account=? AND ts>=? AND ts<=? AND tokens>0''',
            (baseline['account'], baseline_start, cutoff)))
        weights = [weight(row, policy['rates']) for row in rows]
        checks = sample_checkpoints(db, baseline['account']) or {}
        if (rows and all(w is not None for w in weights)
                and all(checks.get(row['device'], {}).get('through', 0) >= baseline['at'] for row in rows)):
            total = sum(sum(w) for w in weights)
            cache_quota = baseline['used']*sum(w[1] for w in weights)/total if total else 0.
        vector = contiguous(db, account)
        for row in db.execute("SELECT origin,seq,payload FROM facts WHERE account=? AND kind='quota' AND ts>=? AND ts<=? ORDER BY ts,origin,seq",
                              (account, declaration['started'], now)):
            value = json.loads(row['payload'])
            if (declaration.get('trigger') != 'immediate' and row['seq'] <= vector.get(row['origin'], 0) and value['used'] >= 100
                    and abs(value['reset_at']-declaration['reset_at']) <= 120):
                hit = value['at']
                break
    attributed['clean_start'] = dict(declaration, state='active' if hit is not None else 'armed', at=hit)
    # The owner explicitly identified this existing official amount. Its raw
    # logs can arrive later; replace the matching attribution, never add twice.
    def covered(row):
        return (row['account'] == baseline['account'] and row['ts'] >= baseline_start
                and row.get('confirmed_at', row['ts']) <= baseline['at'])
    attributed['events'] = [row for row in attributed['events'] if not covered(row)]
    if baseline['used']:
        attributed['events'].append(dict(account=baseline['account'], device=baseline['person'],
            source_device='', model='', ts=baseline['at'], id='clean-start-baseline',
            quota=baseline['used'], units=units(baseline['used']), cache_quota=cache_quota, baseline=True))
    attributed['gaps'] = [row for row in attributed['gaps'] if not (
        row['account'] == baseline['account'] and baseline_start <= row['start'] and row['end'] <= baseline['at'])]
    for stream in attributed['streams']:
        if stream['account'] == baseline['account'] and stream['end'] <= baseline['at'] and stream['cycle_start'] == baseline_start:
            stream.update(events=[], ready=True, reason='')
    if hit is None:
        return
    first = next((c for c in attributed['epochs'][account] if abs(c['reset_at']-declaration['reset_at']) <= 120), None)
    other = next((c for c in reversed(attributed['epochs'][baseline['account']])
                  if c['started'] <= hit and (c['ended'] is None or hit < c['ended'])), None)
    if not first or not other:
        attributed['clean_start']['state'] = 'syncing'
        return
    # Account1 receives no inventory until an observed real reset. Account2
    # keeps its own cycle; the identified initial use belongs to person3.
    known_baseline = abs(other['reset_at']-baseline['reset_at']) <= 120
    attributed['anchors'] = {
        account: dict(at=hit, used=100., cycle=first),
        baseline['account']: dict(at=other['started'], used=0. if known_baseline else other['baseline'], cycle=other),
    }
