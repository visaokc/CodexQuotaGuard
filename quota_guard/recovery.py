"""Local numeric evidence replay; quota correlation is not a server device invoice."""
import bisect
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from .meter import stamp, weighted
from .runtime_evidence import RuntimeEvidence


class HistoryRecovery:
    def __init__(self, database, group_db, home, device):
        self.db, self.group_db = database, group_db
        self.home, self.device = Path(home), device
        self.complete = False
        # Different installations/data roots must never share recovery cursors.
        self.source = hashlib.sha256((str(self.home.resolve())+'|'+device).encode()).hexdigest()
        self.runtime = RuntimeEvidence(database, home, device)
        with self.db.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS recovery_cursors (
                    source TEXT, path TEXT, state TEXT NOT NULL, PRIMARY KEY(source,path));
                CREATE TABLE IF NOT EXISTS recovery_usage (
                    source TEXT, id TEXT, session TEXT NOT NULL, ts REAL NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(source,id));
                CREATE INDEX IF NOT EXISTS recovery_time ON recovery_usage(source,session,ts);
                CREATE TABLE IF NOT EXISTS recovery_boundaries (
                    source TEXT, at REAL, PRIMARY KEY(source,at));
            ''')
            format_key = 'recovery_turn_format:'+self.source
            if not db.execute('SELECT 1 FROM meta WHERE key=?', (format_key,)).fetchone():
                db.execute('DELETE FROM recovery_cursors WHERE source=?', (self.source,))
                db.execute('INSERT INTO meta VALUES (?,?)', (format_key, '1'))

    def boundary(self, at):
        if at is not None:
            with self.db.connect() as db:
                db.execute('INSERT OR IGNORE INTO recovery_boundaries VALUES (?,?)', (self.source, at))

    def scan(self, since, byte_budget=16*1024*1024):
        """Read old and new files independently of billing high-water marks.

        Only numeric usage, model/provider and quota evidence are retained. Never
        store prompts or auth files. Partial lines and bounded reads resume later.
        """
        complete = True
        self.runtime.scan(since)
        for folder in ('sessions', 'archived_sessions'):
            for path in sorted((self.home/folder).rglob('*.jsonl')):
                try:
                    if path.stat().st_mtime < since:
                        continue
                    with self.db.connect() as db:
                        row = db.execute('SELECT state FROM recovery_cursors WHERE source=? AND path=?',
                                         (self.source, str(path))).fetchone()
                        state = json.loads(row[0]) if row else dict(offset=0, session=str(path), model='unknown', provider='unknown')
                        if path.stat().st_size < state['offset']:
                            state = dict(offset=0, session=str(path), model='unknown', provider='unknown')
                        with path.open('rb') as f:
                            f.seek(state['offset'])
                            while byte_budget > 0:
                                raw = f.readline()
                                if not raw or not raw.endswith(b'\n'):
                                    break
                                state['offset'] = f.tell()
                                byte_budget -= len(raw)
                                if not any(k in raw for k in (b'"session_meta"', b'"turn_context"', b'"token_count"', b'"task_started"', b'"turn_started"')):
                                    continue
                                try:
                                    self._record(db, json.loads(raw), state)
                                except (ValueError, KeyError, TypeError, OverflowError):
                                    continue
                            if byte_budget <= 0 and f.tell() < path.stat().st_size:
                                complete = False
                        db.execute('INSERT OR REPLACE INTO recovery_cursors VALUES (?,?,?)',
                                   (self.source, str(path), json.dumps(state)))
                except OSError:
                    complete = False
        self.complete = complete
        return complete

    def _record(self, db, obj, state):
        p = obj.get('payload') or {}
        if obj.get('type') == 'session_meta':
            state.update(session=p.get('id') or state['session'], provider=p.get('model_provider') or 'openai',
                         truncated=bool(p.get('history_base')))
        elif obj.get('type') == 'turn_context':
            state.update(model=p.get('model') or 'unknown', provider=p.get('model_provider') or state['provider'],
                         tier=p.get('service_tier'), turn_at=stamp(obj['timestamp']), turn_id=p.get('turn_id'))
        elif obj.get('type') == 'event_msg' and p.get('type') in ('task_started', 'turn_started'):
            state['turn_at'] = stamp(obj['timestamp'])
            state['turn_id'] = p.get('turn_id')
        elif obj.get('type') == 'event_msg' and p.get('type') == 'token_count':
            info = p.get('info') or {}
            total = info.get('total_token_usage')
            if not isinstance(total, dict):
                return
            keys = ('input_tokens', 'cached_input_tokens', 'output_tokens')
            current = [max(0, int(total.get(k, 0))) for k in keys]
            if any(x > 10**12 for x in current):
                return
            ts = stamp(obj['timestamp'])
            if not math.isfinite(ts):
                return
            rate = p.get('rate_limits') or {}
            weekly = next((rate[k] for k in ('primary', 'secondary') if isinstance(rate.get(k), dict)
                           and rate[k].get('window_minutes') == 10080), {})
            if (weekly and (not all(isinstance(weekly.get(k), (float, int)) and math.isfinite(weekly[k])
                                   for k in ('resets_at', 'used_percent'))
                            or not 0 <= weekly['used_percent'] <= 100)):
                weekly = {}
            last = info.get('last_token_usage')
            value = dict(current=current, model=state['model'], provider=state['provider'],
                         tier=state.get('tier'), turn_at=state.get('turn_at', 0), turn_id=state.get('turn_id'), truncated=state.get('truncated', False),
                         last=[max(0, int(last.get(k, 0))) for k in keys] if isinstance(last, dict) else None,
                         pool=rate.get('limit_id'), reset=weekly.get('resets_at'), used=weekly.get('used_percent'))
            eid = hashlib.sha256((state['session']+json.dumps(current)).encode()).hexdigest()
            # Same cumulative snapshot in fork/archive copies is one event. Keep
            # the earliest original timestamp, not a trailing rate-limit refresh.
            db.execute('''INSERT INTO recovery_usage VALUES (?,?,?,?,?)
                ON CONFLICT(source,id) DO UPDATE SET ts=excluded.ts,payload=CASE
                    WHEN excluded.ts<recovery_usage.ts THEN excluded.payload
                    ELSE json_set(recovery_usage.payload,'$.turn_id',json_extract(excluded.payload,'$.turn_id')) END
                WHERE excluded.ts<recovery_usage.ts OR (excluded.ts=recovery_usage.ts
                    AND json_extract(recovery_usage.payload,'$.turn_id') IS NULL)''',
                       (self.source, eid, state['session'], ts, json.dumps(value)))

    @staticmethod
    def _unresolved(result, event, reason):
        result['unresolved_events'] += 1
        result['unresolved_tokens'] += event['tokens']
        group = result['unresolved_reasons'].setdefault(reason,
            dict(events=0, tokens=0, first_at=event['ts'], last_at=event['ts']))
        group['events'] += 1
        group['tokens'] += event['tokens']
        group['first_at'] = min(group['first_at'], event['ts'])
        group['last_at'] = max(group['last_at'], event['ts'])

    def reconcile(self, account, added_at, multiplier=1, runtime_only=False):
        result = dict(recovered_events=0, recovered_tokens=0, inferred_tokens=0, runtime_tokens=0,
                      unresolved_events=0, unresolved_tokens=0, unresolved_reasons={},
                      runtime=self.runtime.summary(), scanning=not self.complete)
        if not self.complete:
            return dict(self.db.get('history_recovery:'+account, result), scanning=True)
        with self.group_db.connect() as db:
            # Inspect ALL account fingerprints; a reset timestamp is not an account ID.
            snapshots = [json.loads(r[0]) for r in db.execute("SELECT payload FROM facts WHERE kind='quota'")]
        fingerprints = defaultdict(list)
        reset_accounts = defaultdict(set)
        timelines = defaultdict(list)
        for snap in snapshots:
            fingerprints[(snap['account'], snap['reset_at'], snap['used'])].append(snap['at'])
            reset_accounts[snap['reset_at']].add(snap['account'])
            timelines[snap['account']].append(snap)
        for times in fingerprints.values():
            times.sort()
        timeline = sorted(timelines[account], key=lambda s: s['at'])
        timeline_times = [s['at'] for s in timeline]
        with self.db.connect() as db:
            existing = {r['id']: dict(json.loads(r['payload']), sent=r['sent']) for r in db.execute('SELECT * FROM outbox')}
            scope_cuts = [r[0] for r in db.execute('SELECT at FROM recovery_boundaries WHERE source=? ORDER BY at', (self.source,))]
            anchors, barriers, candidates = defaultdict(list), defaultdict(list), defaultdict(list)
            previous = {}
            rows = db.execute('SELECT * FROM recovery_usage WHERE source=? ORDER BY session,ts,id', (self.source,)).fetchall()
            self.runtime.prepare(rows, existing, scope_cuts)
            runtime_blocked = set()
            for row in rows:
                p = json.loads(row['payload'])
                old = previous.get(row['session'])
                current = p['current']
                delta = [max(0, c-b) for c, b in zip(current, old or [0, 0, 0])]
                if old is None and p['truncated']:
                    # A paginated tail is not proof that all prior cumulative usage
                    # occurred on this device/in this turn. Only its last request is.
                    delta = p['last'] or [0, 0, 0]
                    delta = [min(d, c) for d, c in zip(delta, current)]
                previous[row['session']] = [max(c, b) for c, b in zip(current, old or [0, 0, 0])]
                inputs, cached, outputs = delta
                tokens = inputs+outputs
                sid, ts = row['session'], row['ts']
                if p['provider'] != 'openai' or 'spark' in p['model'].lower() or p['pool'] not in (None, 'codex'):
                    barriers[sid].append(ts)
                    continue
                if row['id'] in existing:
                    prior = existing[row['id']]
                    if prior['sent'] == 2 or prior['account'] != account:
                        barriers[sid].append(ts)
                    elif prior.get('attribution') not in ('session_inference', 'interval_inference', 'runtime_inference'):
                        anchors[sid].append((ts, row['id']))
                    continue  # Includes sent=2 scope-race rejections. Never resurrect.
                if row['ts'] <= added_at or not tokens:
                    continue
                resets = [reset for reset in reset_accounts if isinstance(p['reset'], (int, float))
                          and abs(reset-p['reset']) <= 120]
                accounts = set().union(*(reset_accounts[reset] for reset in resets))
                times = sorted(t for reset in resets for t in fingerprints.get((account, reset, p['used']), []))
                index = bisect.bisect_left(times, row['ts'])
                nearby = times[max(0, index-1):index+1]
                index = bisect.bisect_left(timeline_times, row['ts'])
                bracket = timeline[max(0, index-1):index+1]
                # A monitoring outage may be bracketed by identical historical
                # snapshots. Never extrapolate a lone stale snapshot forward.
                plateau = (len(bracket) == 2 and bracket[0]['at'] <= row['ts'] <= bracket[1]['at']
                           and all(s['reset_at'] in resets and s['used'] == p['used'] for s in bracket))
                matched = (p['pool'] in (None, 'codex') and accounts == {account}
                           and (any(abs(t-row['ts']) <= 90 for t in nearby) or plateau))
                weight, known = weighted(p['model'], inputs, min(inputs, cached), outputs,
                                         multiplier*(2.5 if p['tier'] in ('fast', 'priority') else 1))
                event = dict(id=row['id'], device=self.device, account=account, ts=row['ts'],
                             model=p['model'], tokens=tokens, weight=weight, known=known,
                             attribution='quota_correlation', evidence=dict(reset_at=p['reset'], used=p['used'],
                             snapshot_at=min(nearby, key=lambda t: abs(t-row['ts'])) if nearby else None, tier=p['tier']))
                if matched:
                    self.runtime.anchor(row['id'], account)
                if not matched or runtime_only:
                    # A contradictory quota/account is a hard boundary. Missing
                    # evidence alone is not: it can inherit a continuous session.
                    conflicting = p['reset'] is not None and accounts != {account}
                    quota_conflict = conflicting
                    # A known configuration/account boundary also cuts an ongoing
                    # turn. A later fresh turn must not back-fill its old API tail.
                    conflicting |= any(p.get('turn_at', 0) <= cut <= ts for cut in scope_cuts)
                    same_cycle = [s for s in bracket if s['reset_at'] in resets]
                    if p['used'] is not None and same_cycle and len(bracket) == 2:
                        quota_conflict |= not min(s['used'] for s in same_cycle) <= p['used'] <= max(s['used'] for s in same_cycle)
                    conflicting |= quota_conflict
                    if quota_conflict:
                        runtime_blocked.add(row['id'])
                    if conflicting:
                        barriers[sid].append(ts)
                    candidates[sid].append(event)
                    continue
                anchors[sid].append((ts, row['id']))
                db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES (?,?)', (row['id'], json.dumps(event)))
            for sid, pending in candidates.items():
                stops = sorted(barriers[sid]+scope_cuts)
                blocks = defaultdict(list)
                for ts, eid in anchors[sid]:
                    blocks[bisect.bisect_right(stops, ts)].append((ts, eid))
                for event in pending:
                    ts = event['ts']
                    run = self.runtime.resolve(event['id'])
                    if run is not None or runtime_only:
                        if run and run.get('account') == account and event['id'] not in runtime_blocked:
                            event['attribution'] = 'runtime_inference'
                            event['evidence'].update(process=run['process'], segment=run['segment'], anchors=[run['anchor']])
                            db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES (?,?)', (event['id'], json.dumps(event)))
                        else:
                            reason = ('quota_conflict' if event['id'] in runtime_blocked else
                                      'other_account' if run and run.get('account') else
                                      (run or {}).get('reason', 'missing_runtime_evidence'))
                            self._unresolved(result, event, reason)
                        continue
                    pos = bisect.bisect_left(stops, ts)
                    witnesses = blocks[pos] if pos == len(stops) or stops[pos] != ts else []
                    if not witnesses:
                        self._unresolved(result, event, 'quota_conflict' if event['id'] in runtime_blocked
                                         else 'missing_session_evidence')
                        continue
                    before = [w for w in witnesses if w[0] < ts]
                    after = [w for w in witnesses if w[0] > ts]
                    chosen = ([max(before)] if before else []) + ([min(after)] if after else [])
                    event['attribution'] = 'interval_inference' if before and after else 'session_inference'
                    event['evidence']['anchors'] = [eid for _, eid in chosen]
                    db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES (?,?)', (event['id'], json.dumps(event)))
            # Totals survive restarts and uploads; do not report only this scan's delta.
            for row in db.execute("SELECT payload FROM outbox WHERE sent<>2 AND json_extract(payload,'$.account')=? "
                                  "AND json_extract(payload,'$.attribution') IN ('quota_correlation','session_inference','interval_inference','runtime_inference')", (account,)):
                event = json.loads(row[0])
                result['recovered_events'] += 1
                result['recovered_tokens'] += event['tokens']
                if event['attribution'] != 'quota_correlation':
                    result['inferred_tokens'] += event['tokens']
                if event['attribution'] == 'runtime_inference':
                    result['runtime_tokens'] += event['tokens']
        self.db.put('history_recovery:'+account, result)
        return result
