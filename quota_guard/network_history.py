"""Member-authored IP observations, separate from quota and maintenance accounting."""
import json
import math

from .network_guard import public_ip
from .shared_policy import PERSONS, contiguous, person_for, profile


_TEXT_LIMITS = dict(location=240, country=240, purity=40, error=320, risk_error=320)
_FIELDS = {'ip', 'checked_at', 'risk_score', 'risk_at', *_TEXT_LIMITS}
_SEMANTIC = ('ip', 'location', 'country', 'purity', 'risk_score', 'error', 'risk_error')


def validate_report(value, now, live=False):
    if not isinstance(value, dict) or set(value)-_FIELDS:
        raise ValueError('住宅IP报告无效')
    result = {key: value.get(key) for key in _FIELDS}
    at = result['checked_at']
    if (type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= now+60
            or (live and at < now-90)):
        raise ValueError('住宅IP报告时间无效')
    if result['ip'] is not None and public_ip(result['ip']) != result['ip']:
        raise ValueError('住宅IP报告地址无效')
    for key, limit in _TEXT_LIMITS.items():
        text = result[key]
        if text is not None and (not isinstance(text, str) or not 1 <= len(text) <= limit
                                 or not text.isprintable()):
            raise ValueError('住宅IP报告字段无效')
    score, risk_at = result['risk_score'], result['risk_at']
    if score is not None and (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100):
        raise ValueError('住宅IP报告评分无效')
    if risk_at is not None and (type(risk_at) not in (int, float) or not math.isfinite(risk_at)
                               or not 0 <= risk_at <= at+60):
        raise ValueError('住宅IP报告评分时间无效')
    if result['ip'] is None and not result['error']:
        raise ValueError('住宅IP报告缺少检测结果')
    return result


def signature(report):
    return tuple(report.get(key) for key in _SEMANTIC)


def publish(journal, rules, config, report, now):
    """Only IP/metadata changes become immutable facts; timestamps remain live."""
    value = validate_report(report, now, live=True)
    if not rules.get('policy') or not rules.get('accounts'):
        return False
    account = rules['accounts'][0]
    with journal.db.connect() as db:
        previous = db.execute("""SELECT payload FROM facts WHERE account=? AND origin=?
            AND kind='profile' AND json_type(payload,'$.network_report') IS NOT NULL
            ORDER BY ts DESC,seq DESC LIMIT 1""", (account, config['device_id'])).fetchone()
    if previous:
        old = json.loads(previous['payload'])['network_report']
        if value['checked_at'] <= old['checked_at'] or signature(old) == signature(value):
            return False
    journal.append(account, 'profile', profile(config, account, network_report=value), now)
    return True


def members(database, rules, devices, reports, local, now):
    """Show exactly three members, including offline observations and absent history."""
    history = {person: [] for person in PERSONS}
    latest = {}
    accounts = rules.get('accounts', [])
    if accounts:
        with database.connect() as db:
            vector = contiguous(db, accounts[0])
            rows = db.execute("""SELECT origin,seq,ts,payload FROM facts WHERE account=?
                AND kind='profile' AND ts<=? AND json_type(payload,'$.network_report') IS NOT NULL
                ORDER BY ts DESC,origin DESC,seq DESC""", (accounts[0], now))
            for row in rows:
                if row['seq'] > vector.get(row['origin'], 0):
                    continue
                payload = json.loads(row['payload'])
                value = validate_report(payload['network_report'], row['ts'])
                device = row['origin']
                latest.setdefault(device, value)
                person = person_for(rules, device, row['ts'])
                if person in history and len(history[person]) < 20:
                    history[person].append(dict(value, device=device, device_name=payload['name']))
    current = dict(latest)
    for device, report in reports.items():
        if device != local and not devices.get(device, {}).get('online'):
            continue
        try:
            value = validate_report(report, now, live=True)
        except (ValueError, TypeError):
            continue
        if value['checked_at'] >= current.get(device, {}).get('checked_at', -1):
            current[device] = value
    result = []
    bound = {row['device'] for row in rules.get('bindings', [])}
    for index, person in enumerate(PERSONS):
        attached = sorted(device for device in bound if person_for(rules, device, now) == person)
        live = [device for device in attached if device == local or devices.get(device, {}).get('online')]
        candidates = live or attached
        selected = max(candidates, key=lambda device: current.get(device, {}).get('checked_at', -1), default=None)
        name = devices.get(selected, {}).get('name')
        if not name:
            name = next((row['device_name'] for row in history[person] if row['device'] == selected),
                        '成员'+str(index+1))
        result.append(dict(id=person, name=name, online=bool(live), device=selected,
                           report=current.get(selected), history=history[person]))
    return result
