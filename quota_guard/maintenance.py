"""Member-authored maintenance intervals carried by the authenticated journal."""
from .shared_policy import PERSONS, person_for, profile


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'enabled', 'genesis'}
            or type(value['enabled']) is not bool or not isinstance(value['genesis'], str)
            or len(value['genesis']) != 64):
        raise ValueError('软件维护开关无效')


def resolve(records, rules):
    states, costs = {}, []
    for record in sorted(records, key=lambda r: (r['ts'], r['origin'], r['seq'])):
        value = record['value']
        validate(value)
        if value['genesis'] != rules['genesis'] or record['account'] != rules['accounts'][0]:
            continue
        person = person_for(rules, record['origin'], record['ts'])
        if person not in PERSONS:
            continue
        previous = states.get(person, dict(enabled=False))
        if previous['enabled'] == value['enabled']:
            continue
        if previous['enabled'] and previous['since'] < record['ts']:
            costs.extend(dict(account=a, person=person, since=previous['since'], through=record['ts'])
                         for a in rules['accounts'])
        states[person] = dict(enabled=value['enabled'], since=record['ts'])
    for person, state in states.items():
        if state['enabled']:
            costs.extend(dict(account=a, person=person, since=state['since'], through=253402300799.)
                         for a in rules['accounts'])
    return dict(maintenance=states, maintenance_costs=costs)


def cost_policy(rules):
    policy = rules.get('policy') or {}
    return dict(policy, shared_costs=policy.get('shared_costs', [])+rules.get('maintenance_costs', []))


def publish(journal, rules, config, enabled, now):
    person = person_for(rules, config['device_id'], now)
    if rules['status'] != 'ready' or person not in PERSONS or type(enabled) is not bool:
        raise ValueError('成员身份尚未就绪，无法切换软件维护模式')
    if rules.get('maintenance', {}).get(person, {}).get('enabled', False) == enabled:
        return False
    account = rules['accounts'][0]
    journal.append(account, 'profile', profile(config, account,
        maintenance=dict(enabled=enabled, genesis=rules['genesis'])), now)
    return True
