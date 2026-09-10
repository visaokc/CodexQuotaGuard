import json
import math
import statistics
import threading
import time
from datetime import datetime
from .token_budget import estimate_budget
from .sample_pool import sample_checkpoints
from .fair_allocation import allocation


class Ledger:
    def __init__(self, database):
        self.db = database
        self.lock = threading.RLock()
        self.fairness_cache = {}

    def ingest(self, report, now=None):
        now = time.time() if now is None else now
        account, device = report['account'], report['device']
        if len(account) != 64 or len(device) > 100 or not device:
            raise ValueError('设备或账号标识无效')
        cap = float(report.get('cap', 33))
        if not math.isfinite(cap) or not 0 < cap <= 100:
            raise ValueError('配额必须大于 0 且不超过 100')
        events = report.get('events', [])
        if len(events) > 1000:
            raise ValueError('上报批次过大')
        with self.lock, self.db.connect() as db:
            self.fairness_cache.pop(account, None)
            # Keep prior accounts in the ledger, but never present them as still logged in.
            db.execute('UPDATE devices SET logged_in=0,active=0,uncertain=0,unbound_active=0,unbound_uncertain=0 WHERE id=? AND account<>?', (device, account))
            db.execute('''INSERT INTO devices(account,id,name,cap,seen,scan_at,active,uncertain,logged_in)
              VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT(account,id) DO UPDATE SET
              name=excluded.name,seen=excluded.seen,scan_at=excluded.scan_at,
              active=excluded.active,uncertain=excluded.uncertain,logged_in=1''',
                       (account, device, str(report['name'])[:80], cap, now,
                        min(now, float(report.get('scan_at', now))),
                        max(0, min(10000, int(report.get('active', 0)))),
                        max(0, min(10000, int(report.get('uncertain', 0))))))
            for e in events:
                if e['device'] != device or e['account'] != account:
                    raise ValueError('事件归属不一致')
                ts, weight = float(e['ts']), float(e['weight'])
                tokens = int(e['tokens'])
                if (not math.isfinite(ts) or not math.isfinite(weight) or weight < 0
                        or tokens < 0 or ts > now+60 or len(e['id']) != 64):
                    raise ValueError('事件数据无效或设备时钟超前，请同步 Windows 时间')
                db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)',
                           (e['id'], device, account, ts, str(e['model'])[:100], tokens, weight, bool(e['known'])))
        snap = report.get('snapshot')
        if snap and snap['account'] == account and abs(now-float(snap['at'])) <= 90:
            # Single elected observer per account avoids delayed snapshots racing each other.
            with self.lock:
                leader = self.db.get('leader:'+account, {})
                if not leader or leader.get('device') == device or now-leader.get('seen', 0) > 100:
                    self.db.put('leader:'+account, dict(device=device, seen=now))
                    self.observe(dict(snap, at=now))
        return self.summary(account, now)

    def logout(self, device):
        with self.db.connect() as db:
            db.execute('UPDATE devices SET logged_in=0,active=0,uncertain=0,unbound_active=0,unbound_uncertain=0 WHERE id=?', (device,))

    def history(self, account, device, now=None):
        """Calendar buckets use the local Windows timezone, never a quota epoch."""
        now = time.time() if now is None else now
        result = dict(day={}, week={}, month={})
        with self.lock, self.db.connect() as db:
            saved = db.execute('SELECT value FROM meta WHERE key=?', ('statistics_start:'+account,)).fetchone()
            baseline = float(json.loads(saved[0])) if saved else 0
            days = db.execute("""SELECT strftime('%Y-%m-%d',ts,'unixepoch','localtime') AS day,
                SUM(tokens) AS tokens FROM events WHERE account=? AND device=? AND ts>? GROUP BY day ORDER BY day""",
                (account, device, baseline)).fetchall()
        for row in days:
            date = datetime.strptime(row['day'], '%Y-%m-%d')
            year, week, _ = date.isocalendar()
            for unit, key in [('day', row['day']), ('week', f'{year}-W{week:02}'), ('month', date.strftime('%Y-%m'))]:
                result[unit][key] = result[unit].get(key, 0)+row['tokens']
        date = datetime.fromtimestamp(now)
        year, week, _ = date.isocalendar()
        keys = dict(day=date.strftime('%Y-%m-%d'), week=f'{year}-W{week:02}', month=date.strftime('%Y-%m'))
        result['current'] = {unit: result[unit].get(key, 0) for unit, key in keys.items()}
        return result

    def set_cap(self, account, device, cap):
        cap = float(cap)
        if not math.isfinite(cap) or not 0 < cap <= 100:
            raise ValueError('配额范围为 (0, 100]')
        with self.lock, self.db.connect() as db:
            self.fairness_cache.pop(account, None)
            db.execute('UPDATE devices SET cap=? WHERE account=? AND id=?', (cap, account, device))

    def observe(self, snap):
        account, used, reset, at = snap['account'], float(snap['used']), float(snap['reset_at']), float(snap['at'])
        if not all(math.isfinite(x) for x in (used, reset, at)) or not 0 <= used <= 100:
            raise ValueError('额度快照无效')
        with self.lock, self.db.connect() as db:
            self.fairness_cache.pop(account, None)
            row = db.execute('SELECT * FROM epochs WHERE account=? ORDER BY id DESC LIMIT 1', (account,)).fetchone()
            reason = None
            card_key = 'reset_credits:'+account
            saved_card = db.execute('SELECT value FROM meta WHERE key=?', (card_key,)).fetchone()
            prior_card = json.loads(saved_card[0]) if saved_card else {}
            count = snap.get('reset_credits')
            if row is not None and at <= row['observed_at']:
                return
            if row is not None and reset < row['reset_at']-120:
                return
            if type(count) is int and count >= 0:
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (card_key, json.dumps(dict(count=count, at=at))))
            if row is None:
                reason = '首次连接；此前用量不归属任何设备'
            elif at <= row['observed_at']:
                return
            else:
                if reset < row['reset_at']-120:
                    return  # An old device snapshot must not undo a newer reset.
                # Do not clear merely because the PC clock passes resetsAt.
                advanced = reset > row['reset_at']+120
                reset_candidate = advanced or used < row['used']-.01
                if reset_candidate:
                    key = 'reset_candidate:'+account
                    candidate_row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
                    candidate = json.loads(candidate_row[0]) if candidate_row else {}
                    compatible = (candidate and abs(candidate['reset']-reset) < 120
                                  and (advanced or used < row['used']-.01))
                    scheduled = at >= row['reset_at'] and advanced
                    if scheduled or (compatible and at-candidate['at'] >= 15):
                        reason = '已确认周期刷新' if scheduled else '连续快照确认提前重置／额度回退'
                        before = candidate.get('card_before', prior_card)
                        if type(count) is int:
                            if before and 0 <= at-before['at'] <= 1800 and count < before['count']:
                                reason = '重置卡重置'
                            elif scheduled:
                                reason = '已确认周期刷新'
                            elif before and 0 <= at-before['at'] <= 1800 and count >= before['count']:
                                reason = '官方临时重置'
                            else:
                                reason = '提前重置原因未确认'
                        if ('reset_credits' in snap or prior_card) and count is None and not scheduled:
                            reason = '提前重置原因未确认'
                        db.execute('UPDATE epochs SET ended=? WHERE id=?', (at, row['id']))
                        db.execute('DELETE FROM meta WHERE key=?', (key,))
                    else:
                        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                                   (key, json.dumps(dict(reset=reset, at=candidate.get('at', at) if compatible else at,
                                                                    card_before=candidate.get('card_before', prior_card) if compatible else prior_card))))
                        return
                else:
                    db.execute('DELETE FROM meta WHERE key=?', ('reset_candidate:'+account,))
            if reason:
                # For a fresh cycle, usage already incurred before this observation is unknown.
                db.execute('INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES (?,?,?,?,?,?,?)',
                           (account, at, used, used, reset, at, reason))
                return
            if used > row['used']:
                prior = db.execute('SELECT end FROM segments WHERE epoch=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
                start = prior[0] if prior else row['started']
                db.execute('INSERT INTO segments(epoch,start,end,delta) VALUES (?,?,?,?)',
                           (row['id'], start, at, used-row['used']))
            db.execute('UPDATE epochs SET used=?,reset_at=?,observed_at=? WHERE id=?', (used, reset, at, row['id']))

    def summary(self, account, now=None, removed=()):
        now = time.time() if now is None else now
        removed = frozenset(removed)
        with self.lock, self.db.connect() as db:
            epoch = db.execute('SELECT * FROM epochs WHERE account=? ORDER BY id DESC LIMIT 1', (account,)).fetchone()
            devices = {r['id']: dict(r) for r in db.execute('SELECT * FROM devices WHERE account=? ORDER BY name', (account,))}
            for d in devices.values():
                d.update(estimated=0.0, settled=0.0, tokens=0, weight=0.0, unknown_tokens=0,
                         online=now-d['seen'] < 100 and bool(d['logged_in']) and d['id'] not in removed,
                         removed=d['id'] in removed)
            result = dict(account=account, epoch=dict(epoch) if epoch else None, devices=[],
                          unassigned=0.0, provisional=0.0, calibration=None, attribution_gaps=[],
                          allocation='cycle_weighted_v1',
                          reset_pending=bool(db.execute('SELECT 1 FROM meta WHERE key=?', ('reset_candidate:'+account,)).fetchone()))
            if epoch:
                # IDs survive deterministic rebuild and late initial-history reconciliation.
                cycle = str(int(epoch['reset_at']))
                if epoch['reason'].startswith('连续'):
                    cycle += ':'+str(int(epoch['started']))
                result['epoch']['cycle'] = cycle
                events = list(db.execute('SELECT * FROM events WHERE account=? AND ts>? ORDER BY ts', (account, epoch['started'])))
                budget_key = 'token_budget:'+account
                saved = db.execute('SELECT value FROM meta WHERE key=?', (budget_key,)).fetchone()
                previous = json.loads(saved[0]) if saved else {}
                segments = list(db.execute('SELECT * FROM segments WHERE epoch=? ORDER BY end', (epoch['id'],)))
                result['token_budget'], calibrated = estimate_budget(
                    result['epoch'], events, devices, segments, now, previous, result['reset_pending'],
                    checkpoints=sample_checkpoints(db, account))
                if calibrated and calibrated != previous:
                    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (budget_key, json.dumps(calibrated)))
                for e in events:
                    if e['device'] in devices:
                        d = devices[e['device']]
                        d['tokens'] += e['tokens']
                        d['weight'] += e['weight']
                        if not e['known']:
                            d['unknown_tokens'] += e['tokens']
                # Allocation is a cycle-wide sharing policy, not a reconstruction
                # of an official per-device bill. Late/new events revise all shares,
                # even when the official percentage has not increased again.
                weights = {}
                for e in events:
                    if e['tokens'] > 0:
                        weights[e['device']] = weights.get(e['device'], 0) + e['weight']
                total = sum(weights.values())
                unknown = any(not e['known'] and e['tokens'] > 0 for e in events)
                sole_device = len(weights) == 1
                delta = max(0.0, epoch['used']-epoch['baseline'])
                profiled = all(device in devices for device in weights)
                if not profiled or not weights or (not sole_device and (total <= 0 or unknown)):
                    result['unassigned'] = delta
                    if delta:
                        result['attribution_gaps'].append(dict(start=epoch['started'], end=epoch['observed_at'],
                            delta=delta, reason='unknown_weight' if unknown else 'missing_tokens',
                            devices=sorted(weights), unknown_models=sorted({e['model'] for e in events if not e['known']})))
                else:
                    pending = sum(s['delta'] for s in segments if now-s['end'] < 120
                                  or any(d['scan_at'] < s['end'] and d['online'] for d in devices.values()))
                    result['provisional'] = min(delta, pending)
                    for device, weight in weights.items():
                        ratio = 1.0 if sole_device else weight/total
                        devices[device]['estimated'] = delta*ratio
                        devices[device]['settled'] = (delta-result['provisional'])*ratio
                coefficients = []
                index = 0
                # Time-aligned samples remain useful for calibration only; they no
                # longer decide which device owns an individual quota increment.
                for s in segments:
                    eligible = []
                    while index < len(events) and events[index]['ts'] <= s['end']:
                        if events[index]['ts'] > s['start']:
                            eligible.append(events[index])
                        index += 1
                    weights = {}
                    unknown = any(not e['known'] for e in eligible)
                    for e in eligible:
                        weights[e['device']] = weights.get(e['device'], 0) + e['weight']
                    total = sum(weights.values())
                    if len(weights) == 1 and not unknown and total > 0 and s['delta'] >= 2:
                        coefficients.append(s['delta']/total)
                if coefficients:
                    result['calibration'] = dict(samples=len(coefficients),
                        median=statistics.median(coefficients), minimum=min(coefficients), maximum=max(coefficients))
            cached = self.fairness_cache.get(account)
            if cached is None or cached[0] != removed:
                cached = (removed, allocation(db, account, devices, removed))
                self.fairness_cache[account] = cached
            for device, values in cached[1].items():
                if device in devices:
                    devices[device].update(values)
            result['devices'] = list(devices.values())
            result['compensation_enabled'] = any(d.get('compensation_enabled', False) for d in devices.values())
            result['server_time'] = now
            return result
