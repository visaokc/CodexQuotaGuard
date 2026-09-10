"""Replay cross-cycle transfers from replicated facts; never compound a replay."""
import json


def caps_for_total(caps):
    total = sum(caps.values())
    scale = 100 / total if total > 100 else 1
    return {device: cap * scale for device, cap in caps.items()}


def redistribute(used, caps):
    """Only actual overuse borrows from others; unused quota creates no debt."""
    excess = {d: max(0., used.get(d, 0.) - cap) for d, cap in caps.items()}
    spare = {d: max(0., cap - used.get(d, 0.)) for d, cap in caps.items()}
    borrowed, available = sum(excess.values()), sum(spare.values())
    transfer = min(borrowed, available)
    return {d: (transfer * excess[d] / borrowed if borrowed else 0.)
               - (transfer * spare[d] / available if available else 0.) for d in caps}


def allocation(db, account, devices):
    caps = caps_for_total({d: row['cap'] for d, row in devices.items()})
    carry = {d: 0. for d in caps}
    has_facts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts'").fetchone()
    profiles = []
    if has_facts:
        profiles = [(r['ts'], r['kind'], json.loads(r['payload'])) for r in db.execute(
            "SELECT ts,kind,payload FROM facts WHERE account=? AND kind IN ('profile','cap') ORDER BY ts,origin,seq", (account,))]
    starts = [p['fairness_start'] for _, kind, p in profiles if kind == 'profile' and 'fairness_start' in p]
    # Once a replicated start exists, local display-only history filters cannot
    # make two peers compute different balances from the same journal.
    local_start = db.execute('SELECT value FROM meta WHERE key=?', ('statistics_start:'+account,)).fetchone()
    start = min(starts) if starts else float(json.loads(local_start[0])) if local_start else 0.
    for epoch in db.execute('SELECT * FROM epochs WHERE account=? AND ended IS NOT NULL AND started>=? ORDER BY started,reset_at', (account, start)):
        # Unknown usage before attachment must not be charged to another user.
        if epoch['baseline'] > .001:
            continue
        historical = {}
        for ts, kind, p in profiles:
            if ts >= epoch['ended']:
                break
            if p['device'] not in caps:
                continue
            if kind == 'profile' and p['device'] not in historical:
                historical[p['device']] = p['cap']
            elif kind == 'cap' and ts <= epoch['started']:
                historical[p['device']] = p['cap']
        if not profiles:
            historical = dict(caps)
        historical = caps_for_total(historical)
        rows = list(db.execute("""SELECT device,SUM(weight) AS weight,
            MIN(known) AS known FROM events WHERE account=? AND ts>? AND ts<=? AND tokens>0
            GROUP BY device ORDER BY device""", (account, epoch['started'], epoch['ended'])))
        total = sum(row['weight'] for row in rows)
        if not rows or any(row['device'] not in historical for row in rows):
            continue
        if len(rows) > 1 and (total <= 0 or any(not row['known'] for row in rows)):
            continue
        used = {row['device']: max(0., epoch['used']-epoch['baseline']) *
                (1. if len(rows) == 1 else row['weight']/total) for row in rows}
        for device, change in redistribute(used, historical).items():
            carry[device] += change
    # Debt is retained even when it exceeds one full personal allocation.
    # The usable account pool never becomes negative or exceeds its base total.
    available = {d: max(0., cap-carry[d]) for d, cap in caps.items()}
    pool, total = sum(caps.values()), sum(available.values())
    return {d: dict(carry=0. if abs(carry[d]) < 1e-9 else carry[d], fair_base_cap=cap,
                    fair_cap=available[d]*pool/total if total else 0.) for d, cap in caps.items()}
