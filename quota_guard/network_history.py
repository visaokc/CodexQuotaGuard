"""Member-authored IP observations, separate from quota and maintenance accounting."""
import hashlib
import json
import math

from .network_guard import public_ip
from .shared_policy import PERSONS, contiguous, person_for, profile


_TEXT_LIMITS = dict(location=240, country=240, purity=40, error=320, risk_error=320)
_FIELDS = {'ip', 'checked_at', 'risk_score', 'risk_at', 'previous_ip', 'codex_running', *_TEXT_LIMITS}


def validate_report(value, now, live=False):
    if not isinstance(value, dict) or set(value)-_FIELDS:
        raise ValueError('住宅IP报告无效')
    result = {key: value.get(key) for key in _FIELDS}
    result['codex_running'] = value.get('codex_running', False)
    if type(result['codex_running']) is not bool:
        raise ValueError('住宅IP报告运行状态无效')
    at = result['checked_at']
    if (type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= now+60
            or (live and at < now-90)):
        raise ValueError('住宅IP报告时间无效')
    if result['ip'] is not None and public_ip(result['ip']) != result['ip']:
        raise ValueError('住宅IP报告地址无效')
    previous = result['previous_ip']
    if previous is not None and (public_ip(previous) != previous or not result['ip']
                                or previous == result['ip'] or not result['codex_running']):
        raise ValueError('住宅IP变更记录无效')
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


def validate_change(value, now):
    if (not isinstance(value, dict) or set(value) != {'checked_at'}
            or type(value['checked_at']) not in (int, float)
            or not math.isfinite(value['checked_at']) or not 0 <= value['checked_at'] <= now+60):
        raise ValueError('住宅IP变更记录无效')
    return dict(value)


def redact_record(record):
    """Canonical privacy migration, also applied to legacy records before merging."""
    if record['kind'] != 'profile':
        return record
    if 'network_report' not in record['payload']:
        return dict(record, ts=float(record['ts'])) if 'network_change' in record['payload'] else record
    payload = dict(record['payload'])
    report = validate_report(payload.pop('network_report'), record['ts'])
    if report['previous_ip']:
        payload['network_change'] = dict(checked_at=report['checked_at'])
    # SQLite stores the envelope timestamp as REAL; replays must use the same digest.
    return dict(record, ts=float(record['ts']), payload=payload)


def redact_persisted(database):
    """Erase legacy report payloads without removing fact sequence positions."""
    from .journal import canonical
    with database.connect() as db:
        rows = db.execute("""SELECT * FROM facts WHERE kind='profile'
            AND json_type(payload,'$.network_report') IS NOT NULL""").fetchall()
        pending = db.execute("SELECT 1 FROM meta WHERE key='network_history_erasure_pending'").fetchone()
        if not rows and not pending:
            return 0
        db.execute('PRAGMA secure_delete=ON')
        for row in rows:
            original = {key: row[key] for key in ('account','origin','seq','ts','kind')}
            original['payload'] = json.loads(row['payload'])
            record = redact_record(original)
            digest = hashlib.sha256(canonical(record).encode()).hexdigest()
            db.execute('UPDATE facts SET payload=?,digest=? WHERE account=? AND origin=? AND seq=?',
                       (canonical(record['payload']), digest, row['account'], row['origin'], row['seq']))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('network_history_erasure_pending','true')")
        db.commit()
        db.execute('VACUUM')
        if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0:
            db.execute("DELETE FROM meta WHERE key='network_history_erasure_pending'")
    return len(rows)


def publish(journal, rules, config, report, now):
    """Persist only change times. Current IP and metadata stay in memory."""
    value = validate_report(report, now)
    if not value['previous_ip'] or not rules.get('policy') or not rules.get('accounts'):
        return False
    account = rules['accounts'][0]
    with journal.db.connect() as db:
        previous = db.execute("""SELECT payload FROM facts WHERE account=? AND origin=?
            AND kind='profile' AND json_type(payload,'$.network_change') IS NOT NULL
            ORDER BY ts DESC,seq DESC LIMIT 1""", (account, config['device_id'])).fetchone()
    if previous:
        old = json.loads(previous['payload'])['network_change']
        if value['checked_at'] <= old['checked_at']:
            return False
    journal.append(account, 'profile', profile(config, account,
                   network_change=dict(checked_at=value['checked_at'])), now)
    return True


def members(database, rules, devices, reports, local, now):
    """Show exactly three members, including offline observations and absent history."""
    history = {person: [] for person in PERSONS}
    accounts = rules.get('accounts', [])
    if accounts:
        with database.connect() as db:
            vector = contiguous(db, accounts[0])
            rows = db.execute("""SELECT origin,seq,ts,payload FROM facts WHERE account=?
                AND kind='profile' AND ts<=? AND json_type(payload,'$.network_change') IS NOT NULL
                ORDER BY ts DESC,origin DESC,seq DESC""", (accounts[0], now))
            for row in rows:
                if row['seq'] > vector.get(row['origin'], 0):
                    continue
                payload = json.loads(row['payload'])
                value = validate_change(payload['network_change'], row['ts'])
                device = row['origin']
                person = person_for(rules, device, row['ts'])
                if person in history and len(history[person]) < 20:
                    history[person].append(dict(value, device=device, device_name=payload['name']))
    current = {}
    for device, report in reports.items():
        try:
            value = validate_report(report, now)
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
