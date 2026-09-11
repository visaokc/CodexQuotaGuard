"""Versioned group rules carried by existing authenticated profile facts."""
import copy
import hashlib
import json
import math

from .meter import RATES

PERSONS = ('person1', 'person2', 'person3')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate(value, origin, at):
    if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('admin') != origin
            or type(value.get('revision')) is not int or not 1 <= value['revision'] <= 100000
            or not timestamp(value.get('effective')) or value['effective'] > at
            or type(value.get('compensation')) is not bool or value.get('persons') != list(PERSONS)
            or not isinstance(value.get('previous'), str) or len(value['previous']) not in (0, 64)
            or not isinstance(value.get('accounts'), list) or not 1 <= len(value['accounts']) <= 2
            or len(set(value['accounts'])) != len(value['accounts'])):
        raise ValueError('共享计费规则无效')
    for account in value['accounts']:
        if not isinstance(account, str) or len(account) != 64 or any(c not in '0123456789abcdef' for c in account):
            raise ValueError('共享计费账号无效')
    bindings = value.get('bindings')
    if not isinstance(bindings, list) or len(bindings) > 48:
        raise ValueError('成员绑定无效')
    for row in bindings:
        if (not isinstance(row, dict) or row.get('person') not in PERSONS
                or not isinstance(row.get('device'), str) or not 1 <= len(row['device']) <= 100
                or not timestamp(row.get('since')) or row['since'] > value['effective']):
            raise ValueError('成员绑定无效')
    rates = value.get('rates')
    if not isinstance(rates, dict) or not 1 <= len(rates) <= 80:
        raise ValueError('共享计量权重无效')
    for model, weights in rates.items():
        if (not isinstance(model, str) or len(model) > 100 or not isinstance(weights, (tuple, list))
                or len(weights) != 3 or any(not timestamp(w) or w > 1e6 for w in weights)):
            raise ValueError('共享计量权重无效')
    overrides = value.get('reset_types', [])
    if not isinstance(overrides, list) or len(overrides) > 1000:
        raise ValueError('重置确认无效')
    for row in overrides:
        if (not isinstance(row, dict) or row.get('account') not in value['accounts']
                or not timestamp(row.get('started')) or row['started'] > value['effective']
                or row.get('type') not in ('natural', 'card', 'official')):
            raise ValueError('重置确认无效')
    costs = value.get('shared_costs', [])
    if 'rollover_since' in value and (not timestamp(value['rollover_since']) or value['rollover_since'] > value['effective']):
        raise ValueError('结余结转起点无效')
    if 'unused_expiry_from' in value and (not timestamp(value['unused_expiry_from'])
            or not value.get('rollover_since', 0) <= value['unused_expiry_from'] <= value['effective']):
        raise ValueError('未用份额到期规则起点无效')
    if value.get('unknown_weight') not in (None, 'interval_average_v1'):
        raise ValueError('未知模型分摊规则无效')
    if not isinstance(costs, list) or len(costs) > 64:
        raise ValueError('共同消费记录无效')
    for index, row in enumerate(costs):
        if (not isinstance(row, dict) or set(row) != {'account', 'person', 'since', 'through'}
                or row.get('account') not in value['accounts'] or row.get('person') not in PERSONS
                or not timestamp(row.get('since')) or not timestamp(row.get('through'))
                or not row['since'] < row['through'] <= value['effective']):
            raise ValueError('共同消费范围无效')
        if any(old['account'] == row['account'] and old['person'] == row['person']
               and max(old['since'], row['since']) < min(old['through'], row['through']) for old in costs[:index]):
            raise ValueError('共同消费范围重复')
    clean = value.get('clean_start')
    if clean is not None:
        baseline = clean.get('baseline', {}) if isinstance(clean, dict) else {}
        if (not isinstance(clean, dict) or clean.get('account') not in value['accounts']
                or not timestamp(clean.get('started')) or not timestamp(clean.get('reset_at'))
                or clean['started'] >= clean['reset_at'] or clean['started'] > value['effective']
                or baseline.get('account') not in value['accounts'] or baseline['account'] == clean['account']
                or baseline.get('person') not in PERSONS or not timestamp(baseline.get('started'))
                or not timestamp(baseline.get('reset_at')) or not timestamp(baseline.get('at'))
                or not baseline['started'] <= baseline['at'] <= value['effective']
                or baseline['reset_at'] <= baseline['started']
                or not timestamp(baseline.get('used')) or baseline['used'] > 100):
            raise ValueError('共同起点无效')
        if clean.get('trigger', 'exhaustion') not in ('exhaustion', 'immediate'):
            raise ValueError('共同起点触发方式无效')
        if clean.get('trigger') == 'immediate' and (not timestamp(clean.get('at')) or not clean['started'] <= clean['at'] <= value['effective']):
            raise ValueError('共同起点时间无效')
    if value.get('rules_locked') and value['compensation'] is not True:
        raise ValueError('共同补偿规则已锁定开启')
    return value


def genesis(admin, account, devices, now):
    return dict(schema=1, admin=admin, revision=1, previous='', effective=now,
                persons=list(PERSONS), accounts=[account], compensation=False,
                bindings=[dict(device=d, person=p, since=0) for p, d in zip(PERSONS, devices)],
                rates={model: list(rate) for model, rate in RATES.items()})


def contiguous(db, account):
    result = {}
    for row in db.execute('SELECT origin,seq FROM facts WHERE account=? ORDER BY origin,seq', (account,)):
        if row['seq'] == result.get(row['origin'], 0)+1:
            result[row['origin']] = row['seq']
    return result


def load_rules(database, accounts, now):
    policies, claims = {}, []
    with database.connect() as db:
        for account in accounts:
            vector = contiguous(db, account)
            for row in db.execute("SELECT origin,seq,ts,payload FROM facts WHERE account=? AND kind='profile' AND ts<=? ORDER BY ts,origin,seq", (account, now)):
                if row['seq'] > vector.get(row['origin'], 0):
                    continue
                payload = json.loads(row['payload'])
                if 'group_policy' in payload:
                    policy = validate(payload['group_policy'], row['origin'], row['ts'])
                    policies[digest(policy)] = policy
                if 'member_claim' in payload:
                    claim = payload['member_claim']
                    if (isinstance(claim, dict) and claim.get('device') == row['origin']
                            and claim.get('person') in PERSONS and isinstance(claim.get('genesis'), str)):
                        claims.append(dict(claim, at=row['ts']))
    result = dict(status='waiting', reason='等待共享组管理员规则', policies=[], policy=None,
                  accounts=[], bindings=[], eligible_at=None, genesis='')
    roots = [(key, policy) for key, policy in policies.items() if policy['revision'] == 1 and not policy['previous']]
    if len(roots) != 1:
        return dict(result, status='conflict' if roots else 'waiting', reason='共享组规则冲突' if roots else result['reason'])
    root_hash, first = roots[0]
    chain, key, policy = [first], root_hash, first
    while True:
        children = [(h, p) for h, p in policies.items() if p['previous'] == key]
        if not children:
            break
        if len(children) != 1:
            return dict(result, status='conflict', reason='共享组规则版本冲突')
        next_key, following = children[0]
        if (following['admin'] != first['admin'] or following['revision'] != policy['revision']+1
                or following['effective'] < policy['effective'] or following['rates'] != first['rates']
                or following['accounts'][:len(policy['accounts'])] != policy['accounts']
                or following['bindings'][:len(policy['bindings'])] != policy['bindings']
                or following.get('shared_costs', [])[:len(policy.get('shared_costs', []))] != policy.get('shared_costs', [])
                or (policy.get('clean_start') and following.get('clean_start') != policy['clean_start'])
                or ('rollover_since' in policy and following.get('rollover_since') != policy['rollover_since'])
                or ('unused_expiry_from' in policy and following.get('unused_expiry_from') != policy['unused_expiry_from'])
                or (policy.get('rules_locked') and not following.get('rules_locked'))):
            return dict(result, status='conflict', reason='共享组规则链不一致')
        chain.append(following)
        key, policy = next_key, following
    if len(chain) != len(policies):
        return dict(result, status='waiting', reason='等待缺失的规则版本')
    selected = list(policy['accounts'])
    candidates = sorted(set(accounts)-set(selected))
    if len(selected) == 1 and len(candidates) == 1:
        selected += candidates
    elif len(selected) == 1 and len(candidates) > 1:
        return dict(result, status='conflict', reason='请管理员选择账号2', policy=policy, policies=chain,
                    accounts=selected, bindings=policy['bindings'], genesis=root_hash)
    bindings = copy.deepcopy(policy['bindings'])
    first_bound = {}
    for revision in chain:
        for binding in revision['bindings']:
            first_bound.setdefault(binding['person'], revision['effective'])
    explicit = {row['device'] for row in bindings}
    for person in PERSONS:
        matching = [c for c in claims if c['genesis'] == root_hash and c['person'] == person]
        if person in first_bound:
            # Confirming the same claimed identity later must not move migration.
            original = next(b for b in bindings if b['person'] == person)
            accepted = [c['at'] for c in matching if c['device'] == original['device']]
            if accepted:
                first_bound[person] = min(first_bound[person], min(accepted))
            continue
        devices = {c['device'] for c in matching if c['device'] not in explicit}
        if len(devices) > 1:
            return dict(result, status='conflict', reason='新成员身份冲突，请管理员绑定设备', policy=policy, policies=chain,
                        accounts=selected, bindings=policy['bindings'], genesis=root_hash)
        if devices:
            device = next(iter(devices))
            bindings.append(dict(device=device, person=person, since=0))
            first_bound[person] = min(c['at'] for c in matching if c['device'] == device)
    ready = len(selected) == 2 and len(first_bound) == 3
    missing = []
    if len(selected) < 2:
        missing.append('账号2')
    if len(first_bound) < 3:
        missing.append('第三位成员' if len(first_bound) == 2 else '成员加入')
    return dict(result, status='ready' if ready else 'waiting', reason='' if ready else '等待'+'和'.join(missing),
                policy=policy, policies=chain, accounts=selected, bindings=bindings, genesis=root_hash,
                eligible_at=max(first_bound.values()) if len(first_bound) == 3 else None)


def person_for(rules, device, at):
    matches = [row for row in rules.get('bindings', []) if row['device'] == device and row['since'] <= at]
    return max(enumerate(matches), key=lambda item: (item[1]['since'], item[0]))[1]['person'] if matches else None


def profile(config, account, **extra):
    return dict(device=config['device_id'], name=config['name'],
                cap=config.get('tracked_accounts', {}).get(account, {}).get('cap', config['quota']), **extra)


def publish_change(journal, rules, device, name, cap, changes, now):
    previous = rules.get('policy')
    if not previous or previous['admin'] != device:
        raise ValueError('只有共享组管理员可以修改共同规则')
    if 'rollover_since' in previous and changes.get('rollover_since', previous['rollover_since']) != previous['rollover_since']:
        raise ValueError('结余结转起点不可修改')
    if 'unused_expiry_from' in previous and changes.get('unused_expiry_from', previous['unused_expiry_from']) != previous['unused_expiry_from']:
        raise ValueError('未用份额到期规则起点不可修改')
    policy = copy.deepcopy(previous)
    policy.update(changes, revision=previous['revision']+1, previous=digest(previous), effective=now)
    validate(policy, device, now)
    journal.append(policy['accounts'][0], 'profile', dict(device=device, name=name, cap=cap, group_policy=policy), now)
    return policy
