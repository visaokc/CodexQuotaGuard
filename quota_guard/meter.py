"""Local token weights, NOT an official subscription-quota conversion rate."""
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

# Credits per million tokens, used ONLY as initial relative weights.
# Source: https://learn.chatgpt.com/docs/pricing, checked 2026-09-06.
RATES = {
    'gpt-6-astra': (250, 25, 1250),
    'gpt-5.6-sol': (100, 10, 500),
    'gpt-5.6-terra': (50, 5, 300),
    'gpt-5.6-luna': (5, .5, 30),
    'gpt-5.5': (125, 12.5, 750),
    'gpt-5.4': (62.5, 6.25, 375),
    'gpt-5.4-mini': (18.75, 1.875, 113),
}


def weighted(model, inputs, cached, outputs, multiplier):
    rate = RATES.get(model)
    if rate is None:
        return 0.0, False
    return (max(0, inputs-cached)*rate[0] + cached*rate[1] + outputs*rate[2]) / 1e6 * multiplier, True


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class Scanner:
    """Incremental, transactional cursors + durable, deduplicated upload outbox.

    Initial attachment establishes high-water marks, never attributes old history.
    Only token_count numeric fields leave this module. Prompts are never stored.
    """
    def __init__(self, database, home, device, start, allowed_accounts=None):
        self.db, self.home, self.device, self.start = database, Path(home), device, start
        self.allowed_accounts = allowed_accounts
        self.scope_since = start

    def boundary(self, now):
        """Drain ambiguous history, then require a fresh turn before binding any account."""
        self.scan('')
        self.scope_since = now
        with self.db.connect() as db:
            for row in db.execute('SELECT path,state FROM cursors').fetchall():
                state = json.loads(row['state'])
                state['account'] = ''
                db.execute('UPDATE cursors SET state=? WHERE path=?', (json.dumps(state), row['path']))

    def files(self):
        for folder in ('sessions', 'archived_sessions'):
            root = self.home / folder
            if root.is_dir():
                yield from root.rglob('*.jsonl')

    def seed(self, account=''):
        if self.db.get('scanner_seeded'):
            self.refresh_activity()
            return
        # Read header + bounded tail instead of scanning years of conversation text.
        for path in self.files():
            try:
                with path.open('rb') as f:
                    first = f.readline()
                    size = path.stat().st_size
                    offset = max(len(first), size-2*1024*1024)
                    f.seek(offset)
                    if offset > len(first):
                        f.readline()
                    data = f.read()
                safe_size = size if data.endswith(b'\n') else size-len(data.rsplit(b'\n', 1)[-1])
                state = dict(offset=safe_size, session=str(path), model='unknown')
                rows = []
                for line in [first, *data.splitlines()]:
                    try:
                        row = json.loads(line)
                        rows.append(row)
                    except (ValueError, UnicodeError):
                        pass
                with self.db.connect() as db:
                    for row in rows:
                        self._record(db, row, state, account, 1, seed=True)
                    db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (str(path), json.dumps(state)))
            except OSError:
                continue
        self.db.put('scanner_seeded', True)

    def refresh_activity(self, now=None):
        """Recover startup activity without changing billing cursors or high water."""
        now = time.time() if now is None else now
        for path in self.files():
            try:
                if now-path.stat().st_mtime > 7200:
                    continue
                with self.db.connect() as db:
                    row = db.execute('SELECT state FROM cursors WHERE path=?', (str(path),)).fetchone()
                    if not row:
                        continue  # The incremental scanner will handle new files.
                    state = json.loads(row[0])
                    observed = {}
                    with path.open('rb') as f:
                        offset = max(0, path.stat().st_size-2*1024*1024)
                        f.seek(offset)
                        if offset:
                            f.readline()
                        for raw in f.read().splitlines(keepends=True):
                            if not raw.endswith(b'\n'):
                                continue
                            try:
                                obj = json.loads(raw)
                                self._activity_record(obj, observed, stamp(obj['timestamp']))
                            except (ValueError, KeyError, TypeError):
                                continue
                    state.update(observed)
                    # Old clients did not retain an independent activity account.
                    state.setdefault('activity_account', '')
                    db.execute('UPDATE cursors SET state=? WHERE path=?', (json.dumps(state), str(path)))
            except OSError:
                continue

    @staticmethod
    def _activity_record(obj, state, event_time):
        if event_time < state.get('activity', 0):
            return
        if obj.get('type') == 'turn_context':
            payload = obj.get('payload') or {}
            state.update(active=True, activity=event_time, closed_turn=None,
                         activity_turn=payload.get('turn_id'))
            return
        if obj.get('type') == 'response_item':
            if not state.get('closed_turn'):
                state.update(active=True, activity=event_time)
            return
        if obj.get('type') != 'event_msg':
            return
        payload = obj.get('payload') or {}
        kind = payload.get('type')
        if kind in ('task_started', 'turn_started'):
            state.update(active=True, activity=event_time, closed_turn=None, activity_turn=payload.get('turn_id'))
        elif kind in ('task_complete', 'turn_complete', 'turn_completed', 'turn_aborted', 'task_failed'):
            turn = payload.get('turn_id')
            if turn and state.get('activity_turn') and turn != state['activity_turn']:
                return
            state.update(active=False, activity=event_time, closed_turn=turn or True)
        elif kind in ('token_count', 'token_usage_record', 'item_completed', 'agent_message'):
            turn = payload.get('turn_id')
            if state.get('closed_turn') and (not turn or turn == state['closed_turn']):
                return  # Trailing accounting does not reopen an explicitly ended turn.
            state.update(active=True, activity=event_time)
            if turn:
                state.update(activity_turn=turn, closed_turn=None)

    def scan(self, account, multiplier=1.0):
        count = 0
        # Most retained logs are unchanged. Read their offsets once instead of
        # opening a SQLite connection for every historical file on every tick.
        with self.db.connect() as db:
            offsets = {row['path']: row['offset'] for row in db.execute(
                "SELECT path,json_extract(state,'$.offset') AS offset FROM cursors")}
        for path in self.files():
            try:
                if offsets.get(str(path)) == path.stat().st_size:
                    continue
                with self.db.connect() as db:
                    row = db.execute('SELECT state FROM cursors WHERE path=?', (str(path),)).fetchone()
                    state = json.loads(row[0]) if row else dict(offset=0, session=str(path), model='unknown')
                    if path.stat().st_size == state['offset']:
                        continue
                    if path.stat().st_size < state['offset']:
                        state['offset'] = 0
                    with path.open('rb') as f:
                        f.seek(state['offset'])
                        # Bound work per file, finish remaining bytes on the next scan.
                        consumed = 0
                        while consumed < 8*1024*1024:
                            line = f.readline()
                            if not line or not line.endswith(b'\n'):
                                break
                            state['offset'] = f.tell()
                            consumed += len(line)
                            try:
                                obj = json.loads(line)
                            except (ValueError, UnicodeError):
                                continue
                            count += self._record(db, obj, state, account, multiplier)
                    db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (str(path), json.dumps(state)))
            except OSError:
                continue
        return count

    def _record(self, db, obj, state, account, multiplier, seed=False):
        p = obj.get('payload') or {}
        try:
            event_time = stamp(obj['timestamp'])
        except (KeyError, ValueError, TypeError):
            event_time = 0
        # Current login is not evidence for old/ongoing unbound turns. Never lazily
        # attach a token_count to whichever account happens to be selected now.
        allowed = account and (self.allowed_accounts is None or account in self.allowed_accounts)
        boundary = obj.get('type') == 'turn_context' or (
            obj.get('type') == 'event_msg' and p.get('type') in ('task_started', 'turn_started'))
        if boundary:
            state['account'] = account if allowed and not seed and event_time > self.scope_since else ''
            state['activity_account'] = state['account']
        if obj.get('type') == 'session_meta':
            state['session'] = p.get('id', state['session'])
        if obj.get('type') == 'turn_context':
            state['model'] = p.get('model', 'unknown')
            state['provider'] = p.get('model_provider', state.get('provider', 'openai'))
        if obj.get('type') == 'session_meta':
            state['provider'] = p.get('model_provider', 'openai')
        self._activity_record(obj, state, event_time)
        if obj.get('type') != 'event_msg' or p.get('type') != 'token_count':
            return 0
        info = p.get('info') or {}
        total = info.get('total_token_usage')
        if not isinstance(total, dict):
            return 0
        keys = ('input_tokens', 'cached_input_tokens', 'output_tokens')
        current = [max(0, int(total.get(k, 0))) for k in keys]
        old = db.execute('SELECT value FROM counters WHERE session=?', (state['session'],)).fetchone()
        previous = json.loads(old[0]) if old else [0, 0, 0]
        # Replayed history / fork copies / archived moves must not move high water backwards.
        if all(c <= p for c, p in zip(current, previous)):
            return 0
        db.execute('INSERT OR REPLACE INTO counters VALUES (?,?)',
                   (state['session'], json.dumps([max(c, p) for c, p in zip(current, previous)])))
        if (seed or not allowed or state.get('account') != account
                or state.get('provider', 'unknown') != 'openai'):
            return 0
        try:
            ts = stamp(obj['timestamp'])
        except (KeyError, ValueError, TypeError):
            return 0
        if ts <= max(self.start, self.scope_since):
            return 0
        inputs, cached, outputs = [max(0, c-p) for c, p in zip(current, previous)]
        if inputs+outputs == 0:
            return 0
        cached = min(inputs, cached)
        model = state.get('model', 'unknown')
        # Spark has a separate quota pool; never put its usage into the Codex main pool.
        if 'spark' in model.lower():
            return 0
        weight, known = weighted(model, inputs, cached, outputs, multiplier)
        event_id = hashlib.sha256((state['session']+json.dumps(current)).encode()).hexdigest()
        event = dict(id=event_id, device=self.device, account=account, ts=ts, model=model,
                     tokens=inputs+outputs, weight=weight, known=known)
        result = db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES (?,?)',
                            (event_id, json.dumps(event)))
        return result.rowcount

    def activity(self, now=None, account=None):
        now = time.time() if now is None else now
        sessions = {}
        with self.db.connect() as db:
            for r in db.execute('SELECT state FROM cursors'):
                s = json.loads(r[0])
                sid = s['session']
                if s.get('activity', 0) >= sessions.get(sid, {}).get('activity', 0):
                    sessions[sid] = s
        eligible = [s for s in sessions.values() if s.get('provider', 'unknown') == 'openai'
                    and (account is None or s.get('activity_account', '') == account)]
        active = sum(1 for s in eligible if s.get('active') and 0 <= now-s.get('activity', 0) < 120)
        uncertain = sum(1 for s in eligible if s.get('active') and 120 <= now-s.get('activity', 0) < 7200)
        return active, uncertain

    def pending(self, limit=1000, account=None):
        with self.db.connect() as db:
            if account:
                rows = db.execute("SELECT payload FROM outbox WHERE sent=0 AND json_extract(payload,'$.account')=? ORDER BY rowid LIMIT ?", (account, limit))
            else:
                rows = db.execute('SELECT payload FROM outbox WHERE sent=0 ORDER BY rowid LIMIT ?', (limit,))
            return [json.loads(r[0]) for r in rows]

    def checkpoint(self):
        with self.db.connect() as db:
            return db.execute('SELECT COALESCE(MAX(rowid),0) FROM outbox').fetchone()[0]

    def reject_since(self, checkpoint):
        with self.db.connect() as db:
            db.execute('UPDATE outbox SET sent=2 WHERE rowid>?', (checkpoint,))

    def ack(self, ids):
        with self.db.connect() as db:
            db.executemany('UPDATE outbox SET sent=1 WHERE id=?', [(i,) for i in ids])
