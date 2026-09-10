"""Offline-first replication with contiguous per-device vectors and deterministic replay."""
import hashlib
import json
import math
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class Journal:
    def __init__(self, database, ledger, device):
        self.db, self.ledger, self.device = database, ledger, device
        with self.db.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS facts (
              account TEXT NOT NULL, origin TEXT NOT NULL, seq INTEGER NOT NULL,
              ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
              PRIMARY KEY(account,origin,seq));
            CREATE INDEX IF NOT EXISTS facts_kind ON facts(account,kind,ts);
            ''')

    def append(self, account, kind, payload, now=None):
        now = time.time() if now is None else now
        with self.db.connect() as db:
            seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM facts WHERE account=? AND origin=?',
                             (account, self.device)).fetchone()[0]
        record = dict(account=account, origin=self.device, seq=seq, ts=now, kind=kind, payload=payload)
        self.merge(account, [record])
        return record

    def vector(self, account):
        vectors = {}
        with self.db.connect() as db:
            for row in db.execute('SELECT origin,seq FROM facts WHERE account=? ORDER BY origin,seq', (account,)):
                # A later sequence never acknowledges a missing earlier packet.
                previous = vectors.get(row['origin'], 0)
                if row['seq'] == previous+1:
                    vectors[row['origin']] = row['seq']
        return vectors

    def since(self, account, vector, limit=60, byte_limit=24000):
        result = []
        with self.db.connect() as db:
            for row in db.execute('SELECT * FROM facts WHERE account=? ORDER BY origin,seq', (account,)):
                if row['seq'] > vector.get(row['origin'], 0):
                    item = dict(account=row['account'], origin=row['origin'], seq=row['seq'],
                                ts=row['ts'], kind=row['kind'], payload=json.loads(row['payload']))
                    if result and len(json.dumps(result+[item], separators=(',', ':')).encode()) > byte_limit:
                        break
                    result.append(item)
                    # Keep below the WebRTC negotiated SCTP message limit after encryption.
                    if len(result) >= limit or len(json.dumps(result, separators=(',', ':')).encode()) > byte_limit:
                        break
        return result

    def _validate(self, account, r):
        if (r['account'] != account or len(account) != 64 or not 1 <= len(r['origin']) <= 100
                or type(r['seq']) is not int or not 1 <= r['seq'] <= 1e9
                or not math.isfinite(r['ts']) or r['ts'] > time.time()+60
                or r['kind'] not in ('quota', 'events', 'profile', 'cap')
                or len(json.dumps(r)) > 24000):
            raise ValueError('同步记录无效或时钟超前')
        p = r['payload']
        if r['kind'] == 'profile' and 'fairness_start' in p:
            start = p['fairness_start']
            if not isinstance(start, (int, float)) or not math.isfinite(start) or not 0 <= start <= r['ts']:
                raise ValueError('公平分配起点无效')
        if r['kind'] == 'profile' and 'cycle_reset_type' in p:
            value = p['cycle_reset_type']
            if (not isinstance(value, dict) or value.get('type') not in ('自然重置','重置卡','官方临时重置')
                    or not isinstance(value.get('started'), (int, float)) or not math.isfinite(value['started'])
                    or not 0 <= value['started'] <= r['ts']):
                raise ValueError('周期类型确认无效')
        if r['kind'] == 'events':
            if len(p) > 40:
                raise ValueError('事件批次过大')
            for e in p:
                if (e['device'] != r['origin'] or e['account'] != account or len(e['id']) != 64
                        or not math.isfinite(e['weight']) or e['weight'] < 0 or e['weight'] > 1e9
                        or type(e['tokens']) is not int or not 0 <= e['tokens'] <= 1e12
                        or not math.isfinite(e['ts']) or e['ts'] > time.time()+60 or len(e['model']) > 100):
                    raise ValueError('Token 事件无效')
        elif r['kind'] == 'quota':
            if (p['account'] != account or not all(math.isfinite(p[k]) for k in ('used', 'reset_at', 'at'))
                    or not 0 <= p['used'] <= 100 or abs(p['at']-r['ts']) > 60):
                raise ValueError('额度快照无效')
            if p.get('reset_credits') is not None and (type(p['reset_credits']) is not int or p['reset_credits'] < 0):
                raise ValueError('重置卡数量无效')
        elif r['kind'] in ('cap', 'profile'):
            if p['device'] != r['origin']:
                raise ValueError('每个设备只能设置自己的配额')
            if not math.isfinite(p['cap']) or not 0 < p['cap'] <= 100 or not 1 <= len(p['device']) <= 100:
                raise ValueError('设备配额无效')
            if r['kind'] == 'profile' and (p['device'] != r['origin'] or len(p['name']) > 80):
                raise ValueError('设备描述无效')

    def merge(self, account, records, limit=60):
        if len(records) > limit:
            raise ValueError('同步批次过大')
        quota_changed = False
        changes = []
        for r in records:
            self._validate(account, r)
        with self.db.connect() as db:
            for r in records:
                digest = hashlib.sha256(canonical(r).encode()).hexdigest()
                old = db.execute('SELECT digest FROM facts WHERE account=? AND origin=? AND seq=?',
                                 (account, r['origin'], r['seq'])).fetchone()
                if old and old[0] != digest:
                    raise ValueError('设备日志发生分叉；不要复制整个客户端数据目录到另一台设备')
                if old:
                    continue
                db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',
                           (account, r['origin'], r['seq'], r['ts'], r['kind'], canonical(r['payload']), digest))
                changes.append(r)
                quota_changed |= r['kind'] == 'quota'
        if changes:
            # Rebuild projections transactionally on every relevant change. Journals are the
            # durable source of truth; events and devices are idempotent derived views.
            self.project(account, quota_changed, changes)
        return len(changes)

    def project(self, account, quotas=True, changes=None):
        with self.ledger.lock:
            self.ledger.fairness_cache.pop(account, None)
            with self.db.connect() as db:
                if changes is None:
                    events = list(db.execute("SELECT * FROM facts WHERE account=? AND kind='events' ORDER BY ts,origin,seq", (account,)))
                else:
                    events = [dict(r, payload=canonical(r['payload'])) for r in changes if r['kind'] == 'events']
                profiles = list(db.execute("SELECT * FROM facts WHERE account=? AND kind IN ('profile','cap') ORDER BY ts,origin,seq", (account,)))
                rows = profiles + events
                for r in rows:
                    p = json.loads(r['payload'])
                    if r['kind'] == 'profile':
                        db.execute('''INSERT INTO devices(account,id,name,cap,seen) VALUES (?,?,?,?,0)
                          ON CONFLICT(account,id) DO UPDATE SET name=excluded.name''',
                                   (account, p['device'], p['name'], p['cap']))
                    elif r['kind'] == 'events':
                        for e in p:
                            db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)',
                                       (e['id'], e['device'], account, e['ts'], e['model'], e['tokens'], e['weight'], bool(e['known'])))
                # LWW caps must be applied after all profiles, including delayed profiles.
                for r in profiles:
                    if r['kind'] == 'cap':
                        p = json.loads(r['payload'])
                        db.execute('UPDATE devices SET cap=? WHERE account=? AND id=?', (p['cap'], account, p['device']))
                if quotas:
                    db.execute('DELETE FROM segments WHERE epoch IN (SELECT id FROM epochs WHERE account=?)', (account,))
                    db.execute('DELETE FROM epochs WHERE account=?', (account,))
                    db.execute('DELETE FROM meta WHERE key IN (?,?)', ('reset_candidate:'+account, 'reset_credits:'+account))
            if quotas:
                with self.db.connect() as db:
                    snapshots = [json.loads(r[0]) for r in db.execute("SELECT payload FROM facts WHERE account=? AND kind='quota' ORDER BY ts,origin,seq", (account,))]
                reduced = []
                for snap in snapshots:
                    key = (snap['used'], snap['reset_at'], snap.get('reset_credits'))
                    if len(reduced) >= 2 and key == (reduced[-1]['used'], reduced[-1]['reset_at'], reduced[-1].get('reset_credits')) == (reduced[-2]['used'], reduced[-2]['reset_at'], reduced[-2].get('reset_credits')):
                        reduced[-1] = snap
                    else:
                        reduced.append(snap)
                for snap in reduced:
                    self.ledger.observe(snap)

    def presence(self, account, peer, value, now=None):
        now = time.time() if now is None else now
        if (value.get('device') != peer or value.get('account') != account
                or abs(now-float(value['at'])) > 90):
            raise ValueError('设备心跳无效')
        with self.db.connect() as db:
            db.execute('''UPDATE devices SET seen=?,scan_at=?,active=?,uncertain=?,unbound_active=?,unbound_uncertain=?,logged_in=1
                          WHERE account=? AND id=?''',
                       (now, min(now, float(value['scan_at'])), max(0, min(10000, int(value['active']))),
                        max(0, min(10000, int(value['uncertain']))),
                        max(0, min(10000, int(value.get('unbound_active', 0)))),
                        max(0, min(10000, int(value.get('unbound_uncertain', 0)))), account, peer))
