"""Read-only estimates and calendar-day reports; never post estimated debt."""
from datetime import datetime

from .shared_costs import apply, token_shares
from .shared_policy import PERSONS, person_for
from .shared_quota import segment_weights
from .pool_accounting import points, units

PERSONAL_BASE = 100/3


def reports(database, rules, attributed, billing, now):
    from .maintenance import cost_policy
    policy = cost_policy(rules)
    confirmed = billing.get('last_confirmed') or {}
    base = billing.get('people', {}) if billing['status'] == 'active' else confirmed.get('people', {})
    base_at = now if billing['status'] == 'active' else confirmed.get('at', 0)
    day_start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    start = max(0., now-(datetime.fromtimestamp(now)-day_start).total_seconds()-29*86400)
    clean = attributed.get('clean_start', {})
    estimates, incomplete = [], set()
    with database.connect() as db:
        raw = [dict(row) for row in db.execute('''SELECT e.*,d.input_tokens,d.cached_input_tokens,
            d.output_tokens,d.reasoning_output_tokens FROM events e LEFT JOIN event_details d USING(id)
            WHERE ts<=? AND tokens>0 ORDER BY ts,id''', (now,)) if row['account'] in rules['accounts']]
    def excluded(row):
        return (clean.get('state') == 'active' and row['account'] == clean['account']
                and row['ts'] <= next((e['started'] for e in attributed['epochs'][row['account']]
                    if e['started'] > clean['started'] and abs(e['reset_at']-clean['reset_at']) > 120), now))
    raw = [row for row in raw if not excluded(row)]
    for account in rules['accounts']:
        epochs = attributed['epochs'].get(account, [])
        if not epochs or not base:
            continue
        epoch = epochs[-1]
        if epoch['started'] > base_at:
            continue  # Never carry a stale balance estimate across a reset.
        streams = [s for s in attributed['streams'] if s['account'] == account
                   and s['cycle_start'] == epoch['started'] and s['ready'] and s['end'] <= base_at]
        samples = streams[-5:]
        cutoff = max([s['end'] for s in streams]+[e['ts'] for e in attributed['events']
            if e['account'] == account and e.get('baseline') and e['ts'] <= base_at]
            + [attributed['anchors'].get(account, {}).get('at', epoch['started'])])
        sample_rows = [r for r in raw if r['account'] == account
                       and any(s['start'] < r['ts'] <= s['end'] for s in samples)]
        weights = segment_weights(sample_rows, policy)
        denominator = sum(sum(v) for v in weights.values() if v is not None)
        rate = points(sum(s['units'] for s in samples))/denominator if denominator else None
        pending = [r for r in raw if r['account'] == account and r['ts'] > cutoff]
        weights = segment_weights(pending, policy)
        for row in pending:
            person = person_for(rules, row['device'], row['ts'])
            if not person:
                continue
            w = weights[row['id']]
            if rate is None or w is None:
                incomplete.add(person)
                continue
            amount, cache = units(sum(w)*rate), units(w[1]*rate)
            estimates.append(dict(id=row['id'], account=account, device=person,
                source_device=row['device'], model=row['model'], ts=row['ts'],
                units=amount, quota=points(amount), cache_quota=points(cache), estimated=True))
    projected = dict(events=estimates, streams=[])
    apply(projected, policy)
    estimates = projected['events']
    stale_reset = any(epochs and epochs[-1]['started'] > base_at for epochs in attributed['epochs'].values())
    balances = {}
    for person in PERSONS:
        available = base.get(person, {}).get('available')
        pending = sum(row['quota'] for row in estimates if row['device'] == person)
        balances[person] = dict(available_estimate=max(0., available-pending) if available is not None and not stale_reset and person not in incomplete else None,
            balance_estimated=bool(pending or confirmed), estimate_missing=person in incomplete,
            estimate_pending=pending, estimate_at=base_at)
    days = {}
    def daily(at, person):
        date = datetime.fromtimestamp(at).strftime('%Y-%m-%d')
        return days.setdefault((date, person), dict(date=date, person=person, tokens=0,
            quota=0., estimated_quota=0., pending=False))
    confirmed_ids = {r['id'].rsplit(':', 1)[0] if r.get('shared_cost') else r['id'] for r in attributed['events']}
    baseline_ranges = []
    for row in attributed['events']:
        if row.get('baseline'):
            epoch = next((e for e in attributed['epochs'][row['account']]
                if e['started'] <= row['ts'] and (e['ended'] is None or row['ts'] < e['ended'])), None)
            if epoch:
                baseline_ranges.append((row['account'], epoch['started'], row['ts']))
    estimated_ids = {r['id'].rsplit(':', 1)[0] if r.get('shared_cost') else r['id'] for r in estimates}
    for row in raw:
        if row['ts'] < start:
            continue
        person = person_for(rules, row['device'], row['ts'])
        if person not in PERSONS:
            continue
        for recipient, part in token_shares(row, person, policy):
            item = daily(row['ts'], recipient)
            item['tokens'] += part['tokens']
            if row['id'] not in confirmed_ids and row['id'] not in estimated_ids and not any(a == row['account'] and start <= row['ts'] <= end for a, start, end in baseline_ranges):
                item['pending'] = True
    for row in attributed['events']:
        if row['ts'] >= start and not excluded(row):
            daily(row['ts'], row['device'])['quota'] += row['quota']
    for row in estimates:
        original = row['id'].rsplit(':', 1)[0] if row.get('shared_cost') else row['id']
        if row['ts'] >= start and original not in confirmed_ids:
            daily(row['ts'], row['device'])['estimated_quota'] += row['quota']
    return dict(balances=balances, daily=sorted(days.values(), key=lambda r:(r['date'],r['person']), reverse=True))
