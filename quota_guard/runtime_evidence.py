"""Read-only runtime/turn evidence from Codex's local structured log database."""
import bisect
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path


TURN = re.compile(r'(?:^|[ {])turn\.id=([A-Za-z0-9_-]{1,100})(?=[ }])')


class RuntimeEvidence:
    def __init__(self, database, home, device):
        self.db, self.home = database, Path(home)
        self.source = hashlib.sha256((str(self.home.resolve())+'|'+device).encode()).hexdigest()
        self.available, self.complete = False, True
        self.keys, self.owners, self.witnesses = {}, defaultdict(set), defaultdict(list)
        with self.db.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runtime_cursors (
                    source TEXT, path TEXT, last_id INTEGER, PRIMARY KEY(source,path));
                CREATE TABLE IF NOT EXISTS runtime_turns (
                    source TEXT, thread TEXT, turn TEXT, process TEXT, first_at REAL, last_at REAL,
                    PRIMARY KEY(source,thread,turn,process));
                CREATE TABLE IF NOT EXISTS runtime_auth_cuts (
                    source TEXT, process TEXT, at REAL, PRIMARY KEY(source,process,at));
            ''')

    def scan(self, since, row_budget=20000):
        self.available, self.complete = False, True
        for path in sorted(self.home.glob('logs_*.sqlite')):
            try:
                with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as src:
                    src.row_factory = sqlite3.Row
                    columns = {r['name'] for r in src.execute('PRAGMA table_info(logs)')}
                    if not {'id', 'ts', 'ts_nanos', 'target', 'thread_id', 'process_uuid', 'feedback_log_body'} <= columns:
                        continue
                    self.available = True
                    maximum = src.execute('SELECT COALESCE(MAX(id),0) FROM logs').fetchone()[0]
                    with self.db.connect() as db:
                        old = db.execute('SELECT last_id FROM runtime_cursors WHERE source=? AND path=?',
                                         (self.source, str(path))).fetchone()
                        cursor = old[0] if old and old[0] <= maximum else None
                        if cursor is None:
                            first = src.execute('SELECT id FROM logs WHERE ts>=? ORDER BY ts,ts_nanos,id LIMIT 1', (since-300,)).fetchone()
                            cursor = first[0]-1 if first else maximum
                        # Fetch bounded span prefixes, not arbitrary tool output or
                        # conversation bodies. No credentials or prompts are persisted.
                        rows = src.execute('''SELECT id,ts,ts_nanos,target,thread_id,process_uuid,
                            CASE WHEN thread_id IS NOT NULL AND target LIKE 'codex_core::%'
                                 THEN substr(feedback_log_body,1,900)
                                 WHEN target='codex_app_server::outgoing_message'
                                 THEN substr(feedback_log_body,1,100) ELSE '' END AS prefix
                            FROM logs WHERE id>? AND id<=? ORDER BY id LIMIT ?''',
                                           (cursor, maximum, max(0, row_budget))).fetchall()
                        for row in rows:
                            cursor = row['id']
                            process = row['process_uuid']
                            if not isinstance(process, str) or not 1 <= len(process) <= 150:
                                continue
                            ts = row['ts']+row['ts_nanos']/1e9
                            prefix = row['prefix'] or ''
                            if (row['target'] == 'codex_app_server::outgoing_message'
                                    and prefix.startswith('app-server event: account/updated ')):
                                db.execute('INSERT OR IGNORE INTO runtime_auth_cuts VALUES (?,?,?)', (self.source, process, ts))
                            elif (row['thread_id'] and prefix.startswith(('session_loop{', 'turn{', 'submission_dispatch{'))):
                                # Do not match a quoted turn.id inside tool/prompt text.
                                span = prefix.split('}:session_task.run', 1)[0].split('ToolCall:', 1)[0]
                                match = TURN.search(span)
                                if match:
                                    db.execute('''INSERT INTO runtime_turns VALUES (?,?,?,?,?,?)
                                        ON CONFLICT(source,thread,turn,process) DO UPDATE SET
                                        first_at=MIN(first_at,excluded.first_at),last_at=MAX(last_at,excluded.last_at)''',
                                               (self.source, row['thread_id'], match[1], process, ts, ts))
                        row_budget -= len(rows)
                        db.execute('INSERT OR REPLACE INTO runtime_cursors VALUES (?,?,?)', (self.source, str(path), cursor))
                        self.complete &= cursor >= maximum
            except (sqlite3.Error, OSError):
                # An unreadable/locked source must not silently authorize inference.
                self.available, self.complete = True, False
        return self.complete

    def prepare(self, rows, existing, scope_cuts=()):
        self.keys, self.owners, self.witnesses = {}, defaultdict(set), defaultdict(list)
        turns, cuts = defaultdict(list), defaultdict(list)
        with self.db.connect() as db:
            for r in db.execute('SELECT * FROM runtime_turns WHERE source=?', (self.source,)):
                turns[(r['thread'], r['turn'])].append(dict(r))
            for r in db.execute('SELECT process,at FROM runtime_auth_cuts WHERE source=? ORDER BY at', (self.source,)):
                cuts[r['process']].append(r['at'])
        for row in rows:
            p = json.loads(row['payload'])
            matches = turns.get((row['session'], p.get('turn_id')), [])
            has_turn = bool(matches)
            matches = [r for r in matches if r['first_at']-1 <= row['ts'] <= r['last_at']+2]
            if len(matches) == 1:
                r = matches[0]
                # Bind at the beginning of this execution of the turn. Reading
                # its trailing token log later does not change the originating run.
                at = max(r['first_at'], p.get('turn_at', r['first_at']))
                index = bisect.bisect_right(cuts[r['process']], at)
                if index < len(cuts[r['process']]) and cuts[r['process']][index] <= row['ts']:
                    # A turn can issue multiple requests. Without request-level
                    # auth evidence, an in-turn switch cannot be treated as an
                    # old request's tail just because the turn ID stayed the same.
                    self.keys[row['id']] = None
                    continue
                segment = cuts[r['process']][index-1] if index else 0
                scope_index = bisect.bisect_right(scope_cuts, at)
                if scope_index:
                    # Observed configuration changes cut inheritance for NEW
                    # turns, without reassigning an old process's in-flight turn.
                    segment = max(segment, scope_cuts[scope_index-1])
                self.keys[row['id']] = (r['process'], segment)
                if p.get('provider', 'openai') != 'openai':
                    # Explicit API/provider evidence defeats an account-only run
                    # inference even if an auth notification was not retained.
                    self.owners[self.keys[row['id']]].add('provider_conflict')
            elif has_turn:
                self.keys[row['id']] = None  # Ambiguous runtime: never guess one.
        for eid, event in existing.items():
            if event['sent'] != 2 and event.get('attribution') not in ('session_inference', 'interval_inference', 'runtime_inference'):
                self.anchor(eid, event['account'])

    def anchor(self, eid, account):
        key = self.keys.get(eid)
        if key:
            self.owners[key].add(account)
            self.witnesses[(key, account)].append(eid)

    def resolve(self, eid):
        if not self.available:
            return None
        if not self.complete:
            return dict(reason='indexing')
        if eid not in self.keys:
            return None
        key = self.keys[eid]
        if not key:
            return dict(reason='ambiguous_runtime')
        owners = self.owners[key]
        if len(owners) != 1:
            return dict(reason='identity_conflict' if owners else 'unbound_runtime')
        account = next(iter(owners))
        return dict(account=account, process=key[0], segment=key[1],
                    anchor=self.witnesses[(key, account)][0])

    def summary(self):
        with self.db.connect() as db:
            count = db.execute('SELECT COUNT(DISTINCT process) FROM runtime_turns WHERE source=?', (self.source,)).fetchone()[0]
        return dict(available=self.available, scanning=not self.complete, processes=count)
