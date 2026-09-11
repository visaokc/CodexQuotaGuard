"""Journal facts and quota replay become visible as one committed projection."""
import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.storage import Database


A = 'a'*64


def journal_fixture(tmp_path):
    database = Database(tmp_path/'projection.sqlite')
    ledger = Ledger(database)
    journal = Journal(database,ledger,'one')
    journal.append(A,'profile',dict(device='one',name='First member',cap=50),100)
    for at,used in ((101,0),(200,47),(300,93)):
        journal.append(A,'quota',dict(account=A,at=at,used=used,reset_at=10000,reset_credits=2),at)
    return database, ledger, journal


def updated_records():
    payloads = [('profile',310,dict(device='one',name='Updated member',cap=50)),
                ('events',350,[dict(id='e'*64,account=A,device='one',ts=349,model='gpt-6-astra',
                                   tokens=100,weight=1,known=True,input_tokens=80,cached_input_tokens=60,output_tokens=20)]),
                ('quota',400,dict(account=A,at=400,used=96,reset_at=10000,reset_credits=2))]
    return [dict(account=A,origin='one',seq=5+i,ts=at,kind=kind,payload=payload)
            for i,(kind,at,payload) in enumerate(payloads)]


def read_projection(path):
    # Independent SQLite connection: readers do not share the writer's Python lock.
    with sqlite3.connect(path,timeout=1) as db:
        return dict(epochs=db.execute('SELECT started,ended,used,observed_at FROM epochs WHERE account=? ORDER BY started',(A,)).fetchall(),
                    segments=db.execute('SELECT s.start,s.end,s.delta FROM segments s JOIN epochs e ON e.id=s.epoch WHERE e.account=? ORDER BY s.end',(A,)).fetchall(),
                    facts=db.execute('SELECT seq,kind,payload FROM facts WHERE account=? ORDER BY origin,seq',(A,)).fetchall(),
                    devices=db.execute('SELECT id,name,cap FROM devices WHERE account=? ORDER BY id',(A,)).fetchall(),
                    events=db.execute('SELECT * FROM events WHERE account=? ORDER BY id',(A,)).fetchall(),
                    details=db.execute('SELECT d.* FROM event_details d JOIN events e ON e.id=d.id WHERE e.account=? ORDER BY d.id',(A,)).fetchall(),
                    reset_credits=db.execute('SELECT value FROM meta WHERE key=?',('reset_credits:'+A,)).fetchone())


@pytest.mark.parametrize('operation',['project','merge'])
def test_reader_never_sees_deleted_or_half_replayed_epochs(tmp_path,monkeypatch,operation):
    database,ledger,journal=journal_fixture(tmp_path)
    previous=read_projection(database.path)
    entered,release=threading.Event(),threading.Event()
    observe=ledger.observe
    calls=[]
    failures=[]

    def paused_observe(*args,**kwargs):
        value=observe(*args,**kwargs)
        calls.append(args[0]['at'])
        if len(calls)==1:
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release quota replay')
        return value

    def rebuild():
        try:
            if operation=='project':
                journal.project(A)
            else:
                journal.merge(A,updated_records())
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(ledger,'observe',paused_observe)
    worker=threading.Thread(target=rebuild)
    worker.start()
    try:
        assert entered.wait(5),'writer did not reach first replayed quota'
        during=read_projection(database.path)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not failures
    assert during==previous
    final=read_projection(database.path)
    if operation=='project':
        assert final==previous
    else:
        assert final['epochs']==[(101,None,96,400)]
        assert sum(row[2] for row in final['segments'])==96
        assert len(final['facts'])==len(previous['facts'])+3
        assert final['devices']==[('one','Updated member',50)]
        assert len(final['events'])==len(final['details'])==1
        assert json.loads(final['reset_credits'][0])==dict(count=2,at=400)


@pytest.mark.parametrize('operation',['project','merge'])
def test_replay_failure_rolls_back_all_projection_and_new_facts(tmp_path,monkeypatch,operation):
    database,ledger,journal=journal_fixture(tmp_path)
    previous=read_projection(database.path)
    observe=ledger.observe
    calls=[]

    def fail_during_replay(*args,**kwargs):
        observe(*args,**kwargs)
        calls.append(args[0]['at'])
        if len(calls)==2:
            raise RuntimeError('isolated replay failure')

    monkeypatch.setattr(ledger,'observe',fail_during_replay)
    with pytest.raises(RuntimeError,match='isolated replay failure'):
        if operation=='project':
            journal.project(A)
        else:
            journal.merge(A,updated_records())
    assert read_projection(database.path)==previous
    assert A not in ledger.fairness_cache
    # A retry must replay again rather than acknowledging an unprojected fact.
    monkeypatch.setattr(ledger,'observe',observe)
    journal.merge(A,updated_records())
    assert read_projection(database.path)['epochs']==[(101,None,96,400)]


def test_standalone_observe_api_keeps_reset_and_credit_behavior(tmp_path):
    database=Database(tmp_path/'observe.sqlite')
    ledger=Ledger(database)
    ledger.observe(dict(account=A,at=100,used=90,reset_at=200,reset_credits=2))
    ledger.observe(dict(account=A,at=201,used=1,reset_at=10000,reset_credits=2))
    with database.connect() as db:
        rows=db.execute('SELECT started,ended,used,reason FROM epochs ORDER BY started').fetchall()
        assert tuple(rows[0])==(100,201,90,'首次连接；此前用量不归属任何设备')
        assert tuple(rows[1])==(201,None,1,'已确认周期刷新')
        assert db.execute('SELECT COUNT(*) FROM segments').fetchone()[0]==0


def test_commit_failure_keeps_previous_projection_and_invalidates_fairness_cache(tmp_path,monkeypatch):
    database,ledger,journal=journal_fixture(tmp_path)
    ledger.summary(A,350)
    assert A in ledger.fairness_cache
    previous=read_projection(database.path)
    connect=database.connect

    @contextmanager
    def rejected_commit():
        with connect() as db:
            yield db
            raise sqlite3.OperationalError('isolated commit rejection')

    monkeypatch.setattr(database,'connect',rejected_commit)
    with pytest.raises(sqlite3.OperationalError,match='isolated commit rejection'):
        journal.merge(A,updated_records())
    assert read_projection(database.path)==previous
    assert A not in ledger.fairness_cache
