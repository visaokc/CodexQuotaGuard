"""Share confirmed maintenance costs without changing raw usage or account totals."""
from .pool_accounting import points, split, units
from .shared_policy import PERSONS


def uncovered(costs, account, person, since, through):
    ranges = [(since, through)]
    for cost in costs:
        if cost['account'] != account or cost['person'] != person:
            continue
        remaining = []
        for start, end in ranges:
            if cost['through'] <= start or cost['since'] >= end:
                remaining.append((start, end))
            else:
                if start < cost['since']:
                    remaining.append((start, cost['since']))
                if cost['through'] < end:
                    remaining.append((cost['through'], end))
        ranges = remaining
    return [dict(account=account, person=person, since=start, through=end) for start, end in ranges]


def token_shares(event, person, policy):
    """Derived member statistics only; leave stored events and curve samples intact."""
    if not any(cost['account'] == event['account'] and cost['person'] == person
               and cost['since'] < event['ts'] <= cost['through'] for cost in policy.get('shared_costs', [])):
        return [(person, event)]
    totals = split(event['tokens'], {p: 1 for p in PERSONS})
    outputs = inputs = caches = reasoning = None
    if event['input_tokens'] is not None:
        outputs = split(event['output_tokens'], totals)
        inputs = {p: totals[p]-outputs[p] for p in PERSONS}
        caches = split(event['cached_input_tokens'], inputs)
        if event['reasoning_output_tokens'] is not None:
            reasoning = split(event['reasoning_output_tokens'], outputs)
    return [(p, dict(event, tokens=totals[p], shared_cost=True,
        weight=event['weight']*totals[p]/event['tokens'] if event['tokens'] else 0,
        input_tokens=inputs[p] if inputs is not None else None,
        output_tokens=outputs[p] if outputs is not None else None,
        cached_input_tokens=caches[p] if caches is not None else None,
        reasoning_output_tokens=reasoning[p] if reasoning is not None else None)) for p in PERSONS]


def apply(attributed, policy):
    costs = policy.get('shared_costs', [])
    if not costs:
        return

    def distribute(rows):
        result = []
        for row in rows:
            matched = any(cost['account'] == row['account'] and cost['person'] == row['device']
                          and cost['since'] < row['ts'] <= cost['through'] for cost in costs)
            if not matched or row.get('baseline') or row.get('shared_cost'):
                result.append(row)
                continue
            shares = split(row['units'], {person: 1 for person in PERSONS})
            caches = split(units(row['cache_quota']), shares) if row['cache_quota'] is not None else None
            for person, amount in shares.items():
                result.append(dict(row, id=row['id']+':'+person, device=person, units=amount,
                    quota=points(amount), cache_quota=points(caches[person]) if caches is not None else None,
                    shared_cost=True))
        return result

    attributed['events'] = distribute(attributed['events'])
    for stream in attributed['streams']:
        stream['events'] = distribute(stream['events'])
