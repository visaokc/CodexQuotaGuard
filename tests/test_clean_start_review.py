"""Independent edge-case review of the explicitly declared shared clean start."""
import pytest

from test_clean_start import setup_clean, book
from test_shared_billing import A, B, add_event, close_samples
from quota_guard.shared_policy import load_rules
from quota_guard.shared_view import shared_usage


def claim_third(db,journals):
    rules=load_rules(db,[A,B],300)
    journals['three'].append(B,'profile',dict(device='three',name='third',cap=50,
        member_claim=dict(device='three',person='person3',genesis=rules['genesis'])),300)


def exhaust(journals):
    journals['one'].append(A,'quota',dict(account=A,used=100,reset_at=800,at=200),200)


def test_baseline_plateau_does_not_drop_later_official_increment_from_chart(tmp_path):
    db,journals,_=setup_clean(tmp_path)
    exhaust(journals)
    claim_third(db,journals)
    # 2% was already reached at119, then reported unchanged at the declared120
    # baseline. A request at119.5 belongs to the next official increment.
    journals['one'].append(B,'quota',dict(account=B,used=2,reset_at=850,at=119),119)
    add_event(journals,account=B,device='three',at=110)
    add_event(journals,account=B,device='three',at=119.5)
    add_event(journals,account=B,device='one',at=210)
    journals['one'].append(B,'quota',dict(account=B,used=5,reset_at=850,at=220),220)
    close_samples(journals)
    ledger,attributed=book(db)
    assert ledger['status']=='active'
    assert sum(row['available'] for row in ledger['people'].values())==pytest.approx(95)
    assert sum(row['quota'] for row in attributed['events'] if row['account']==B)==pytest.approx(5)
    view=shared_usage(db,'group:review',{A:'账号1',B:'账号2'},400,rules=load_rules(db,[A,B],400))
    assert sum(row['quota'] for row in view['windows']['cycle']['quota_rows'])==pytest.approx(5)


def test_late_older_account2_history_moves_start_without_changing_existing_charge(tmp_path):
    db,journals,_=setup_clean(tmp_path)
    exhaust(journals)
    before,_=book(db)
    claim_third(db,journals)
    journals['three'].append(B,'quota',dict(account=B,used=0,reset_at=850,at=80),80)
    add_event(journals,account=B,device='three',at=95)
    close_samples(journals)
    after,attributed=book(db)
    assert after['status']=='active'
    assert after['people']==before['people']
    assert after['people']['person3']['fair_usage']==2
    assert sum(row['quota'] for row in attributed['events'] if row['account']==B)==2
    with db.connect() as sql:
        assert sql.execute('SELECT MIN(started) FROM epochs WHERE account=?',(B,)).fetchone()[0]==80
        assert sql.execute('SELECT SUM(tokens) FROM events WHERE account=?',(B,)).fetchone()[0]==1100


def test_account2_reset_before_account1_exhaustion_does_not_carry_old_two_percent(tmp_path):
    db,journals,_=setup_clean(tmp_path)
    # Early official reset of B before A is exhausted; its existing2% expires.
    journals['one'].append(B,'quota',dict(account=B,used=0,reset_at=1550,at=150,reset_credits=2),150)
    journals['one'].append(B,'quota',dict(account=B,used=0,reset_at=1550,at=170,reset_credits=2),170)
    exhaust(journals)
    current,_=book(db)
    assert current['status']=='active'
    assert sum(p['available'] for p in current['people'].values())==100
    assert all(p['fair_usage']==p['debt']==0 for p in current['people'].values())
    assert all(p['by_account'][A]==0 for p in current['people'].values())


def test_expiring_account1_without_exhaustion_does_not_invent_clean_start(tmp_path):
    db,journals,_=setup_clean(tmp_path)
    journals['one'].append(A,'quota',dict(account=A,used=0,reset_at=1500,at=801),801)
    current,_=book(db,900)
    assert current['status']=='armed'
    assert current['active_since'] is None
    assert not any(row['kind']=='clean_start' for row in current['entries'])


def test_incomplete_clean_start_history_never_falls_back_to_normal_migration_inventory():
    from quota_guard.shared_quota import accounting
    from quota_guard.shared_policy import genesis
    policy=genesis('one',A,['one','two','three'],90)
    policy.update(accounts=[A,B],compensation=True,rules_locked=True)
    first=dict(started=100,ended=None,reset_at=800,baseline=92,used=100,reason='natural')
    # A partially synchronized peer has only B's first post-clean observation.
    # prepare cannot locate B at the clean instant and leaves status='syncing'.
    other=dict(started=300,ended=None,reset_at=850,baseline=2,used=2,reason='natural')
    rules=dict(status='ready',reason='',policy=policy,policies=[policy],accounts=[A,B])
    attributed=dict(clean_start=dict(state='syncing',at=200,account=A),
        anchors={A:dict(at=100,used=92,cycle=first),B:dict(at=300,used=2,cycle=other)},
        epochs={A:[first],B:[other]},streams=[],events=[],overrides={},exempt=set())
    result=accounting(rules,attributed,400)
    assert result['status']!='active'
    assert not result['people'] or all(value['available'] is None for value in result['people'].values())
