"""One official-increment attribution stream for charts, summaries and the pool."""
import json
from decimal import Decimal
from fractions import Fraction

from .pool_accounting import FULL, Pool, points, split, units
from .shared_policy import PERSONS, person_for, contiguous
from .sample_pool import sample_checkpoints


def weight(row, rates):
    rate = rates.get(row['model'])
    if not rate or row['input_tokens'] is None:
        return None
    values = (row['input_tokens']-row['cached_input_tokens'], row['cached_input_tokens'], row['output_tokens'])
    return [int(Decimal(str(r))*v*1_000_000) for r, v in zip(rate, values)]


def segment_weights(rows, policy):
    rates = policy['rates']
    result = {row['id']: weight(row, rates) for row in rows}
    if policy.get('unknown_weight') != 'interval_average_v1':
        return result
    # An explicitly authorized estimate, not a claim about an internal model's price.
    # Use the interval's Token-weighted model mix; all-unknown intervals use the
    # equal mean of the group's fixed rate table. Cache remains a separate channel.
    known = [(rates[row['model']], row['tokens']) for row in rows if row['model'] in rates and row['tokens'] > 0]
    mix = known or [(rate, 1) for rate in rates.values()]
    total = sum(count for _, count in mix)
    average = [sum(Fraction(str(rate[index]))*count for rate, count in mix)/total for index in range(3)]
    for row in rows:
        if row['model'] not in rates and row['input_tokens'] is not None:
            values = (row['input_tokens']-row['cached_input_tokens'], row['cached_input_tokens'], row['output_tokens'])
            result[row['id']] = [int(rate*value*1_000_000) for rate, value in zip(average, values)]
    return result


def attribution(database, rules, now):
    streams, events, gaps, epochs, anchors, exempt, overrides = [], [], [], {}, {}, set(), {}
    eligible = rules.get('eligible_at')
    policy = rules.get('policy')
    if not policy:
        return dict(streams=[], events=[], gaps=[], epochs={}, anchors={}, exempt=set(), overrides={})
    with database.connect() as db:
        for account in rules['accounts']:
            vector = contiguous(db, account)
            records = list(db.execute("SELECT origin,seq,ts,payload FROM facts WHERE account=? AND kind='profile' AND ts<=? ORDER BY ts,origin,seq", (account, now)))
            checks = {device: row['through'] for device, row in (sample_checkpoints(db, account) or {}).items()}
            participants = {}
            for record in records:
                if record['seq'] > vector.get(record['origin'], 0):
                    continue
                payload = json.loads(record['payload'])
                if person_for(rules, record['origin'], record['ts']):
                    participants.setdefault(record['origin'], record['ts'])
                marker = payload.get('no_debt_cycle')
                if marker:
                    exempt.add((account, marker['started'], marker['reset_at']))
                override = payload.get('cycle_reset_type')
                if override:
                    overrides[account, override['started']] = override['type']
            cycles = [dict(row) for row in db.execute('SELECT * FROM epochs WHERE account=? AND started<=? ORDER BY started,id', (account, now))]
            epochs[account] = cycles
            raw = [dict(row) for row in db.execute('''SELECT e.*,d.input_tokens,d.cached_input_tokens,d.output_tokens
                FROM events e LEFT JOIN event_details d USING(id) WHERE account=? AND ts<=? AND tokens>0 ORDER BY ts,id''', (account, now))]
            index = 0
            segments = list(db.execute('SELECT s.*,e.started AS cycle_start,e.reset_at FROM segments s JOIN epochs e ON e.id=s.epoch WHERE e.account=? AND s.end<=? ORDER BY s.end,s.start', (account, now)))
            for segment in segments:
                rows = []
                while index < len(raw) and raw[index]['ts'] <= segment['end']:
                    row = raw[index]
                    index += 1
                    if row['ts'] > segment['start']:
                        rows.append(row)
                weights = segment_weights(rows, policy)
                people = {row['id']: person_for(rules, row['device'], row['ts']) for row in rows}
                missing = not rows or any(value is None or not sum(value) for value in weights.values()) or any(p is None for p in people.values())
                waiting = any(checks.get(device, 0) < segment['end'] for device, since in participants.items() if since <= segment['end'])
                reason = '用量或模型明细待补齐' if missing else '等待成员采集确认' if waiting else ''
                entry = dict(account=account, start=segment['start'], end=segment['end'],
                             cycle_start=segment['cycle_start'], reset_at=segment['reset_at'],
                             units=units(segment['delta']), ready=not reason, reason=reason, events=[])
                if not reason:
                    shares = split(entry['units'], {key: sum(value) for key, value in weights.items()})
                    for row in rows:
                        parts = split(shares[row['id']], dict(enumerate(weights[row['id']])))
                        event = dict(account=account, device=people[row['id']], source_device=row['device'],
                                     model=row['model'], ts=row['ts'], id=row['id'], quota=points(shares[row['id']]),
                                     cache_quota=points(parts[1]), units=shares[row['id']], confirmed_at=segment['end'])
                        events.append(event)
                        entry['events'].append(event)
                else:
                    gaps.append(dict(account=account, start=entry['start'], end=entry['end'], reason=reason,
                                     devices=sorted({people[row['id']] for row in rows if people[row['id']]})))
                streams.append(entry)
            if eligible is not None:
                observations = [json.loads(row['payload']) for row in db.execute("SELECT origin,seq,payload FROM facts WHERE account=? AND kind='quota' AND ts>=? AND ts<=? ORDER BY ts,origin,seq", (account, eligible, now)) if row['seq'] <= vector.get(row['origin'], 0)]
                for observation in observations:
                    cycle = next((cycle for cycle in reversed(cycles) if cycle['started'] <= observation['at']
                        and (cycle['ended'] is None or observation['at'] < cycle['ended'])
                        and abs(cycle['reset_at']-observation['reset_at']) <= 120), None)
                    if cycle:
                        used = cycle['baseline']+sum(s['delta'] for s in segments if s['cycle_start'] == cycle['started'] and s['end'] <= observation['at'])
                        if abs(used-observation['used']) < .000001:
                            anchors[account] = dict(at=observation['at'], used=used, cycle=cycle)
                            break
    for row in policy.get('reset_types', []):
        overrides[row['account'], row['started']] = row['type']
    result = dict(streams=streams, events=events, gaps=gaps, epochs=epochs, anchors=anchors, exempt=exempt, overrides=overrides)
    from .clean_start import prepare
    prepare(database, rules, result, now)
    from .shared_costs import apply
    from .maintenance import cost_policy
    apply(result, cost_policy(rules))
    return result


def accounting(rules, attributed, now):
    policy = rules.get('policy') or {}
    clean = attributed.get('clean_start') or {}
    result = dict(status='waiting', reason=rules.get('reason') or '等待两个账号的官方起点',
                  people={}, entries=[], anchors={}, compensation_enabled=policy.get('compensation', False),
                  active_since=None, clean_start=clean)
    if clean.get('state') == 'armed':
        result.update(status='armed', reason='账号1用尽后结清旧阶段；账号2现有用量保留，旧阶段不产生欠款')
        return result
    if clean and clean.get('state') != 'active':
        result.update(status='syncing', reason='共同起点的账号周期记录正在补齐，保留已确认用量')
        return result
    if (rules.get('status') != 'ready' and clean.get('state') != 'active') or len(attributed['anchors']) != 2:
        return result
    anchors = attributed['anchors']
    pool = Pool(rules['accounts'], rollover_since=policy.get('rollover_since'))
    actions, issues = [], []
    if policy.get('unused_expiry_from') is not None and policy['unused_expiry_from'] <= now:
        actions.append((policy['unused_expiry_from'], .25, 'policy', dict(kind='expire_saved')))
    if clean:
        actions.append((0, 0, 'policy', dict(compensation=True)))
    else:
        for revision in rules['policies']:
            actions.append((revision['effective'], 0, 'policy', revision))
    for account, anchor in anchors.items():
        actions.append((anchor['at'], 1, account, dict(kind='initial', **anchor)))
        previous = anchor['cycle']
        for cycle in attributed['epochs'][account]:
            if cycle['started'] <= previous['started']:
                continue
            actions.append((cycle['started'], 1, account, dict(kind='reset', cycle=cycle, previous=previous)))
            previous = cycle
    for stream in attributed['streams']:
        anchor = anchors[stream['account']]
        if clean and stream['account'] == clean['account'] and stream['cycle_start'] == anchor['cycle']['started']:
            continue
        if stream['end'] <= anchor['at']:
            continue
        # First new official increment can start before an unchanged migration
        # observation. Only consumption after that observation is attributable.
        rows = [row for row in stream['events'] if row['ts'] > anchor['at']]
        if stream['start'] < anchor['at'] and len(rows) != len(stream['events']):
            issues.append('迁移边界的官方增量待核对')
        if not stream['ready']:
            issues.append(stream['reason'])
        for row in rows:
            at_boundary = any(cycle['ended'] == row['ts'] and cycle['started'] == stream['cycle_start'] for cycle in attributed['epochs'][stream['account']])
            actions.append((row['ts'], .5 if at_boundary else 2, stream['account'], dict(kind='consume', event=row)))
    for row in attributed.get('events', []):
        if row.get('baseline') and row['ts'] >= anchors[row['account']]['at']:
            actions.append((row['ts'], 2, row['account'], dict(kind='consume', event=row)))
    # Stable account order and event id resolve same-time observations on every peer.
    actions.sort(key=lambda item: (item[0], item[1], item[2], item[3].get('event', {}).get('id', '')))
    for at, order, account, action in actions:
        if order == 0:
            pool.compensation(action['compensation'], at)
        elif action['kind'] == 'expire_saved':
            pool.expire_saved(at)
        elif action['kind'] == 'initial':
            cycle = action['cycle']
            pool.grant(account, max(0, FULL-units(action['used'])), [cycle['started'], cycle['reset_at']], at, initial=True)
        elif action['kind'] == 'reset':
            cycle, old = action['cycle'], action['previous']
            cause = attributed['overrides'].get((account, cycle['started']), cycle['reason'])
            cause = {'自然重置':'natural', '已确认周期刷新':'natural', '重置卡':'card', '重置卡重置':'card',
                     '官方临时重置':'official', 'natural':'natural', 'card':'card', 'official':'official'}.get(cause, 'unknown')
            exempt = (account, old['started'], old['reset_at']) in attributed['exempt']
            if cause == 'unknown' and pool.enabled and any(pool.pending[account].values()) and not exempt:
                issues.append('提前重置原因待确认')
            pool.reset(account, [cycle['started'], cycle['reset_at']], at, cause,
                       remaining=max(0, FULL-units(cycle['baseline'])), exempt=exempt)
        else:
            event = action['event']
            try:
                pool.spend(account, event['device'], event['units'], at)
            except ValueError:
                issues.append('官方余额与消费边界待核对')
    result.update(status='syncing' if issues else 'active', reason='；'.join(dict.fromkeys(issues)),
                  people=pool.summary(), entries=pool.entries[-1000:], compensation_enabled=pool.enabled,
                  anchors={a: dict(at=row['at'], remaining=100-row['used']) for a, row in anchors.items()},
                  active_since=min(row['at'] for row in anchors.values()))
    if clean:
        result['active_since'] = clean['at']
        result['entries'].insert(0, dict(kind='clean_start', at=clean['at'], account=clean['account'],
            amount=100. if clean.get('trigger') != 'immediate' else None,
            balances=dict(person1=50., person2=50., person3=0.) if clean.get('trigger') != 'immediate' else {},
            reason='账号1旧周期仅归档、不计新账；账号2当前周期起算' if clean.get('trigger') == 'immediate' else '旧阶段两人等分结清，不留欠款'))
    if issues:
        for person in result['people'].values():
            person.update(available=None, rollover=None, by_account={a: None for a in pool.accounts}, pending=None, confirmed=None, debt=None, fair_usage=None)
    return result


def last_confirmed(database, rules, attributed, now):
    """Historical balance only: never substitute incomplete attribution for live inventory."""
    anchors, clean = attributed['anchors'], attributed.get('clean_start', {})
    pending = [s for s in attributed['streams'] if not s['ready'] and s['account'] in anchors
               and s['end'] > anchors[s['account']]['at']
               and not (clean and s['account'] == clean['account']
                        and s['cycle_start'] == anchors[s['account']]['cycle']['started'])]
    if not pending:
        return None
    at = min(s['end'] for s in pending)-0.000001
    if at < max([a['at'] for a in anchors.values()]+[clean.get('at') or 0]):
        return None
    previous = accounting(rules, attribution(database, rules, at), at)
    if previous['status'] != 'active':
        return None
    return dict(at=at, people=previous['people'], pending_quota=sum(points(s['units']) for s in pending))
