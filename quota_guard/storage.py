import json
import os
import secrets
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def defaults():
    return dict(version=1, device_id=str(uuid.uuid4()), name=socket.gethostname(),
                rendezvous_url='', fingerprint='', relay_token='', group_secret=secrets.token_urlsafe(32),
                stun_url='stun:stun.l.google.com:19302', force_relay=False,
                quota=33.0, multiplier=1.0, tracked_accounts={}, autostart=True, auto_update=True,
                codex_home=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')),
                quota_display="personal", interval=30, auto_block=False, program_paths=[], started_at=time.time())


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors (path TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS counters (session TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS devices (
              account TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
              cap REAL NOT NULL, seen REAL NOT NULL, scan_at REAL NOT NULL DEFAULT 0,
              active INTEGER NOT NULL DEFAULT 0, uncertain INTEGER NOT NULL DEFAULT 0,
              logged_in INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(account,id));
            CREATE TABLE IF NOT EXISTS epochs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, started REAL NOT NULL,
              ended REAL, baseline REAL NOT NULL, used REAL NOT NULL, reset_at REAL NOT NULL,
              observed_at REAL NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS segments (
              id INTEGER PRIMARY KEY AUTOINCREMENT, epoch INTEGER NOT NULL,
              start REAL NOT NULL, end REAL NOT NULL, delta REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, device TEXT NOT NULL, account TEXT NOT NULL,
              ts REAL NOT NULL, model TEXT NOT NULL, tokens INTEGER NOT NULL,
              weight REAL NOT NULL, known INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS events_time ON events(account,ts);
            CREATE TABLE IF NOT EXISTS event_details (
              id TEXT PRIMARY KEY, input_tokens INTEGER NOT NULL,
              cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(devices)')}
            for name in ('unbound_active', 'unbound_uncertain'):
                if name not in columns:
                    db.execute(f'ALTER TABLE devices ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
            if 'reasoning_output_tokens' not in {row['name'] for row in db.execute('PRAGMA table_info(event_details)')}:
                db.execute('ALTER TABLE event_details ADD COLUMN reasoning_output_tokens INTEGER')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))
