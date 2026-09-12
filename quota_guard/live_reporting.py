"""Read-only estimates and calendar-day reports; never post estimated debt."""
from datetime import datetime

from .shared_costs import apply, token_shares
from .shared_policy import PERSONS, person_for
from .shared_quota import segment_weights
from .pool_accounting import points, units
from .quota_estimation import fit_rate, project

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
    estimates, incomplete, accounts = [], set(), {}
    with database.connect() as db:
        raw = [dict(row) for row in db.execute('''SELECT e.*,d.input_tokens,d.cached_input_tokens,
            d.output_tokens,d.reasoning_output_tokens FROM events e LEFT JOIN event_details d USING(id)
            WHERE ts<=? AND tokens>0 ORDER BY ts,id''', (now,)) if row['account'] in rules['accounts']]
        reset_pending = {row['key'].split(':', 1)[1] for row in db.execute(
            "SELECT key FROM meta WHERE key LIKE 'reset_candidate:%'")}
    def excluded(row):
        return (clean.get('state') == 'active' and row['account'] == clean['account']
                and row['ts'] <= next((e['started'] for e in attributed['epochs'][row['account']]
                    if e['started'] > clean['started'] and abs(e['reset_at']-clean['reset_at']) > 120), now))
    calibration_rows = raw
    raw = [row for row in raw if not excluded(row)]
    for account in rules['accounts']:
        epochs = attributed['epochs'].get(account, [])
        if not epochs:
            continue
        epoch = epochs[-1]
        current = [r for r in raw if r['account'] == account and r['ts'] > epoch['started']]
        streams = [s for s in attributed['streams'] if s['account'] == account
                   and s['cycle_start'] == epoch['started']]
        samples, provisional_samples = [], []
        for stream in streams:
            if not stream['ready'] and stream['reason'] != '等待成员采集确认':
                continue
            sample_rows = [r for r in current if stream['start'] < r['ts'] <= stream['end']]
            sample_weights = segment_weights(sample_rows, policy)
            if sample_weights and all(v is not None for v in sample_weights.values()):
                target = samples if stream['ready'] else provisional_samples
                target.append((points(stream['units']), sum(sum(v) for v in sample_weights.values())))
        # Missing peer checkpoints must not freeze the live display. Until a
        # complete sample arrives, fit the official increment against received
        # usage only. Late facts refit this display-only estimate automatically;
        # confirmed attribution, borrowing and raw events remain unchanged.
        calibration = fit_rate(samples or provisional_samples) if account not in reset_pending else None
        if calibration is None and account not in reset_pending and len(epochs) > 1:
            # A reset starts new inventory, not an uncalibrated meter. Use only
            # the immediately preceding cycle's complete samples as a warm start;
            # any usable current-cycle sample takes precedence on the next read.
            prior = []
            for stream in attributed['streams']:
                if stream['account'] != account or stream['cycle_start'] != epochs[-2]['started'] or not stream['ready']:
                    continue
                rows = [r for r in calibration_rows if r['account'] == account and stream['start'] < r['ts'] <= stream['end']]
                weighted = segment_weights(rows, policy)
                if weighted and all(v is not None for v in weighted.values()):
                    prior.append((points(stream['units']), sum(sum(v) for v in weighted.values())))
            calibration = fit_rate(prior)

        cutoff = max([s['end'] for s in streams if s['ready'] and s['end'] <= base_at]
            + [e['ts'] for e in attributed['events']
                if e['account'] == account and e.get('baseline') and e['ts'] <= base_at]
            + [attributed['anchors'].get(account, {}).get('at', epoch['started'])])
        anchor = attributed['anchors'].get(account, {})
        official_at = max([s['end'] for s in streams]
            + [e['ts'] for e in attributed['events'] if e['account'] == account
               and e.get('baseline') and e['ts'] >= epoch['started']]
            + ([anchor['at']] if anchor.get('cycle', {}).get('started') == epoch['started'] else [])
            + [epoch['started']])
        weights = segment_weights([r for r in current if r['ts'] > min(cutoff, official_at)], policy)

        def estimate_after(boundary):
            pending = [r for r in current if r['ts'] > boundary]
            return project(pending, calibration,
                lambda row: (sum(weights[row['id']]), weights[row['id']][1])
                            if weights[row['id']] is not None else None)

        # The official stock already contains every observed increment, even when
        # member attribution is waiting. Both displays use this same calibrated
        # rate, but only deduct events not already included in their own base.
        official_estimates, official_missing = estimate_after(official_at)
        amount = sum(row['quota'] for row in official_estimates)
        blocked = bool(official_missing or account in reset_pending)
        accounts[account] = dict(used_estimate=epoch['used']+amount if not blocked else None,
            remaining_estimate=max(0., 100-epoch['used']-amount) if not blocked else None,
            balance_estimated=bool(official_estimates) and not blocked, estimate_pending=amount,
            estimate_missing=blocked, estimate_samples=calibration['samples'] if calibration else 0,
            estimate_at=official_at)
        if not base or epoch['started'] > base_at or account in reset_pending:
            continue  # Never carry a stale personal balance estimate across a reset.
        projected, missing = estimate_after(cutoff)
        for row in missing:
            person = person_for(rules, row['device'], row['ts'])
            if person:
                incomplete.update(recipient for recipient, _ in token_shares(row, person, policy))
        for row in projected:
            person = person_for(rules, row['device'], row['ts'])
            if not person:
                continue
            amount = units(row['quota'])
            cache = units(row['cache_quota']) if row['cache_quota'] is not None else None
            estimates.append(dict(id=row['id'], account=account, device=person,
                source_device=row['device'], model=row['model'], ts=row['ts'],
                units=amount, quota=points(amount), cache_quota=points(cache) if cache is not None else None,
                estimated=True))
    projected = dict(events=estimates, streams=[])
    apply(projected, policy)
    confirmed_events = {row['id']: row for row in attributed['events']}
    estimates = [dict(row, units=confirmed_events[row['id']]['units'],
                      quota=confirmed_events[row['id']]['quota'],
                      cache_quota=confirmed_events[row['id']]['cache_quota'])
                 if row['id'] in confirmed_events else row for row in projected['events']]
    stale_reset = bool(reset_pending.intersection(rules['accounts'])) or any(epochs and epochs[-1]['started'] > base_at for epochs in attributed['epochs'].values())
    balances = {}
    for person in PERSONS:
        available = base.get(person, {}).get('available')
        pending = sum(row['quota'] for row in estimates if row['device'] == person)
        balances[person] = dict(available_estimate=max(0., available-pending) if available is not None and not stale_reset and person not in incomplete else None,
            balance_estimated=bool(pending or confirmed) and not stale_reset and person not in incomplete,
            estimate_missing=stale_reset or person in incomplete,
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
    # A historical balance may still need a later confirmed debit. Charts already
    # contain that official event, so never return it again as an estimate.
    chart_estimates = [row for row in estimates if row['id'] not in confirmed_events]
    return dict(balances=balances, events=chart_estimates, accounts=accounts,
                daily=sorted(days.values(), key=lambda r:(r['date'],r['person']), reverse=True))
