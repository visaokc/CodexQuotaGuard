"""Local numeric evidence replay; quota correlation is not a server device invoice."""
import bisect
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from .meter import stamp, weighted, usage_delta
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
            format_key = 'recovery_detail_format:'+self.source
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
        with self.db.connect() as db:
            offsets = {row['path']: row['offset'] for row in db.execute(
                "SELECT path,json_extract(state,'$.offset') AS offset FROM recovery_cursors WHERE source=?",
                (self.source,))}
        for folder in ('sessions', 'archived_sessions'):
            for path in sorted((self.home/folder).rglob('*.jsonl')):
                try:
                    stat = path.stat()
                    if stat.st_mtime < since or offsets.get(str(path)) == stat.st_size:
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
                         reasoning=total.get('reasoning_output_tokens'), last_reasoning=last.get('reasoning_output_tokens') if isinstance(last,dict) else None,
                         tier=state.get('tier'), turn_at=state.get('turn_at', 0), turn_id=state.get('turn_id'), truncated=state.get('truncated', False),
                         last=[max(0, int(last.get(k, 0))) for k in keys] if isinstance(last, dict) else None,
                         pool=rate.get('limit_id'), reset=weekly.get('resets_at'), used=weekly.get('used_percent'))
            eid = hashlib.sha256((state['session']+json.dumps(current)).encode()).hexdigest()
            # Same cumulative snapshot in fork/archive copies is one event. Keep
            # the earliest original timestamp, not a trailing rate-limit refresh.
            db.execute('''INSERT INTO recovery_usage VALUES (?,?,?,?,?)
                ON CONFLICT(source,id) DO UPDATE SET ts=excluded.ts,payload=CASE
                    WHEN excluded.ts<recovery_usage.ts THEN excluded.payload
                    WHEN excluded.ts=recovery_usage.ts THEN excluded.payload ELSE recovery_usage.payload END
                WHERE excluded.ts<recovery_usage.ts OR (excluded.ts=recovery_usage.ts
                    AND (json_extract(recovery_usage.payload,'$.turn_id') IS NULL
                    OR json_type(recovery_usage.payload,'$.reasoning') IS NULL))''',
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
            previous, previous_reasoning, legacy_high = {}, {}, {}
            rows = db.execute('SELECT * FROM recovery_usage WHERE source=? ORDER BY session,ts,id', (self.source,)).fetchall()
            # Reuse decoding within this reconciliation only. A later call still
            # rereads every witness, including late facts and rewritten evidence.
            payloads = {row['id']: json.loads(row['payload']) for row in rows}
            requests=defaultdict(list)
            existing_order={eid:index for index,eid in enumerate(existing)}
            for row in rows:
                last=payloads[row['id']].get('last')
                if isinstance(last,list) and len(last)==3:
                    requests[(row['session'],row['ts'],tuple(last))].append(row['id'])
            duplicates=set()
            for ids in requests.values():
                if len({existing[eid]['account'] for eid in ids if eid in existing})>1:
                    continue
                selected=min(ids,key=lambda eid:existing_order.get(eid,len(existing)))
                duplicates.update(eid for eid in ids if eid!=selected)
            self.runtime.prepare(rows, existing, scope_cuts, payloads=payloads)
            runtime_blocked = set()
            for row in rows:
                p = payloads[row['id']]
                old = previous.get(row['session'])
                current = p['current']
                legacy_before=legacy_high.get(row['session'],[0,0,0])
                legacy_delta=[max(0,c-b) for c,b in zip(current,legacy_before)]
                legacy_high[row['session']]=[max(c,b) for c,b in zip(current,legacy_before)]
                delta = usage_delta(current,old or [0,0,0],p.get('last'))
                if old is None and (p['truncated'] or p.get('last') is not None):
                    # A paginated tail is not proof that all prior cumulative usage
                    # occurred on this device/in this turn. Only its last request is.
                    delta = p['last'] or [0, 0, 0]
                    delta = [min(d, c) for d, c in zip(delta, current)]
                previous[row['session']] = current
                if row['id'] in duplicates:
                    prior=existing.get(row['id'])
                    if prior and prior['sent']!=2 and prior['account']==account:
                        self._repair_components(db,prior,row['id'],(0,0,0),legacy_delta,0,multiplier)
                    continue
                inputs, cached, outputs = delta
                before_reasoning=previous_reasoning.get(row['session'])
                current_reasoning=p.get('reasoning')
                reasoning=None
                if delta==p.get('last'):
                    reasoning=p.get('last_reasoning')
                elif type(current_reasoning) is int and type(before_reasoning) is int and not any(c<b for c,b in zip(current,old or [0,0,0])):
                    reasoning=max(0,current_reasoning-before_reasoning)
                elif old is None and not p['truncated']:
                    reasoning=current_reasoning
                previous_reasoning[row['session']]=current_reasoning if type(current_reasoning) is int else None
                if type(reasoning) is not int or not 0 <= reasoning <= outputs:
                    reasoning=None
                tokens = inputs+outputs
                sid, ts = row['session'], row['ts']
                if (p['provider'] != 'openai' or 'spark' in p['model'].lower()
                        or (p['pool'] not in (None,'codex') and row['id'] not in existing)):
                    barriers[sid].append(ts)
                    continue
                if row['id'] in existing:
                    prior = existing[row['id']]
                    if prior['sent'] == 2 or prior['account'] != account:
                        barriers[sid].append(ts)
                    else:
                        # A last-request witness must confirm every repaired component;
                        # the first cumulative checkpoint can contain imported history.
                        if delta==p.get('last'):
                            self._repair_components(db,prior,row['id'],(inputs,min(inputs,cached),outputs),legacy_delta,reasoning,multiplier*(2.5 if p['tier'] in ('fast','priority') else 1))
                        # Enrich a known event, preserving its ID and original bill.
                        # Re-uploading the same ID adds details without extra Token.
                        matching=all(prior.get(k,v)==v for k,v in zip(('input_tokens','cached_input_tokens','output_tokens'),(inputs,min(inputs,cached),outputs)))
                        if prior['tokens'] == tokens and matching and ('input_tokens' not in prior or (reasoning is not None and 'reasoning_output_tokens' not in prior)):
                            enriched = {k:v for k,v in prior.items() if k != 'sent'}
                            enriched.update(input_tokens=inputs, cached_input_tokens=min(inputs,cached), output_tokens=outputs)
                            if reasoning is not None:
                                enriched['reasoning_output_tokens']=reasoning
                            db.execute('UPDATE outbox SET payload=?,sent=0 WHERE id=?', (json.dumps(enriched),row['id']))
                        if prior.get('attribution') not in ('session_inference', 'interval_inference', 'runtime_inference'):
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
                             input_tokens=inputs, cached_input_tokens=min(inputs,cached), output_tokens=outputs,
                             attribution='quota_correlation', evidence=dict(reset_at=p['reset'], used=p['used'],
                             snapshot_at=min(nearby, key=lambda t: abs(t-row['ts'])) if nearby else None, tier=p['tier']))
                if reasoning is not None:
                    event['reasoning_output_tokens']=reasoning
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

    @staticmethod
    def _repair_components(db,prior,eid,actual,legacy_delta,reasoning,multiplier=1):
        """Append a revision with its expected prior bill; retain original facts."""
        keys=('input_tokens','cached_input_tokens','output_tokens')
        if prior['tokens']==actual[0]+actual[2] and all(prior.get(k,v)==v for k,v in zip(keys,actual)):
            return
        original=[prior[k] for k in keys] if all(k in prior for k in keys) else [legacy_delta[0],min(legacy_delta[:2]),legacy_delta[2]]
        base,known=weighted(prior['model'],*original,1)
        if original[0]+original[2]==prior['tokens'] and base>0 and .05<=prior['weight']/base<=50:
            multiplier=prior['weight']/base
        weight,known=weighted(prior['model'],*actual,multiplier)
        repair={k:v for k,v in prior.items() if k not in ('sent','reasoning_output_tokens')}
        repair.update(dict(zip(keys,actual)),tokens=actual[0]+actual[2],weight=weight,known=known,
                      replaces=dict(tokens=prior['tokens'],weight=prior['weight']))
        if reasoning is not None:
            repair['reasoning_output_tokens']=reasoning
        db.execute('UPDATE outbox SET payload=?,sent=0 WHERE id=?',(json.dumps(repair),eid))
