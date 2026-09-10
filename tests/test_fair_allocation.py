import pytest

from quota_guard.fair_allocation import redistribute
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.storage import Database, defaults
from test_core import A, B, event, snap


def setup(tmp_path):
    db = Database(tmp_path/'fair.sqlite')
    l = Ledger(db)
    a, b = Journal(db, l, 'one'), Journal(db, l, 'two')
    for j in (a, b):
        j.append(A, 'profile', dict(device=j.device, name=j.device, cap=50, fairness_start=100, compensation_enabled=True), 100)
    a.append(A, 'quota', snap(100, 0), 100)
    return db, l, a, b


def use(a, b, at, one, two, used=100, reset=10000):
    for j, weight in ((a, one), (b, two)):
        if weight:
            j.append(A, 'events', [event(j.device, at, weight)], at)
    a.append(A, 'quota', snap(at+10, used, reset), at+10)


def rows(l):
    return {d['id']: d for d in l.summary(A, 2000000)['devices']}


def balances(l):
    return {d: r['carry'] for d, r in rows(l).items()}


def test_130_70_carries_once_replays_and_repayment(tmp_path):
    db, l, a, b = setup(tmp_path)
    use(a, b, 150, 130, 70)
    assert balances(l) == {'one': 0, 'two': 0}
    a.append(A, 'quota', snap(10001, 0, 700000), 10001)
    assert balances(l) == pytest.approx({'one': 15, 'two': -15})
    r = rows(l)
    assert r['one']['carry']/r['one']['fair_base_cap']*100 == 30
    assert r['two']['carry']/r['two']['fair_base_cap']*100 == -30
    assert [r[d]['fair_cap'] for d in ('one', 'two')] == [35, 65]
    assert all(d['estimated'] == 0 for d in r.values())
    for _ in range(3):
        a.project(A)
        assert balances(l) == pytest.approx({'one': 15, 'two': -15})
    assert balances(Ledger(Database(db.path))) == balances(l)
    use(a, b, 10100, 70, 130, reset=700000)
    a.append(A, 'quota', snap(700001, 0, 1400000), 700001)
    assert balances(l) == pytest.approx({'one': 0, 'two': 0}, abs=1e-8)


def test_reset_card_inherits_balance_and_empty_cycle_does_not_clear_it(tmp_path):
    _, l, a, b = setup(tmp_path)
    use(a, b, 150, 130, 70)
    a.append(A, 'quota', snap(300, 0, 700000), 300)
    assert l.summary(A, 301)['reset_pending']
    assert balances(l) == {'one': 0, 'two': 0}
    a.append(A, 'quota', snap(320, 0, 700000), 320)
    assert balances(l) == pytest.approx({'one': 15, 'two': -15})
    a.append(A, 'quota', snap(700001, 0, 1400000), 700001)
    assert balances(l) == pytest.approx({'one': 15, 'two': -15})


def test_partial_usage_only_transfers_actual_excess():
    assert redistribute({'one': 60, 'two': 20}, {'one': 50, 'two': 50}) == {'one': 10, 'two': -10}
    assert redistribute({'one': 40, 'two': 20}, {'one': 50, 'two': 50}) == {'one': 0, 'two': 0}


def test_cumulative_debt_keeps_account_pool_100_even_beyond_one_allocation(tmp_path):
    _, l, a, b = setup(tmp_path)
    for start, reset, next_reset in [(100, 10000, 700000), (10001, 700000, 1400000)]:
        use(a, b, start+50, 1, 0, reset=reset)
        a.append(A, 'quota', snap(reset+1, 0, next_reset), reset+1)
    assert balances(l) == {'one': 100, 'two': -100}
    assert {d: r['fair_cap'] for d, r in rows(l).items()} == {'one': 0, 'two': 100}


def test_late_sync_and_duplicate_pages_converge_despite_different_local_filters(tmp_path):
    _, l, a, b = setup(tmp_path)
    use(a, b, 150, 130, 70)
    a.append(A, 'quota', snap(10001, 0, 700000), 10001)
    db2 = Database(tmp_path/'peer.sqlite')
    l2 = Ledger(db2)
    peer = Journal(db2, l2, 'observer')
    db2.put('statistics_start:'+A, 20000)
    records = a.since(A, {}, limit=60)
    early = [r for r in records if r['kind'] != 'events' or r['origin'] == 'one']
    late = [r for r in records if r not in early]
    peer.merge(A, early)
    assert balances(l2) != balances(l)
    peer.merge(A, late)
    peer.merge(A, records)
    assert balances(l2) == balances(l)
    peer.project(A)
    assert balances(l2) == balances(l)


def test_unknown_baseline_and_unknown_weights_never_charge_another_user(tmp_path):
    _, l, a, b = setup(tmp_path)
    unknown = event('two', 150, 0, known=False)
    a.append(A, 'events', [event('one', 150, 1)], 150)
    b.append(A, 'events', [unknown], 150)
    a.append(A, 'quota', snap(200, 100), 200)
    a.append(A, 'quota', snap(10001, 10, 700000), 10001)
    assert balances(l) == {'one': 0, 'two': 0}
    use(a, b, 10100, 1, 0, reset=700000)
    a.append(A, 'quota', snap(700001, 0, 1400000), 700001)
    assert balances(l) == {'one': 0, 'two': 0}


def test_new_caps_do_not_rewrite_old_transfers_and_accounts_are_isolated(tmp_path):
    _, l, a, b = setup(tmp_path)
    use(a, b, 150, 130, 70)
    a.append(A, 'quota', snap(10001, 0, 700000), 10001)
    a.append(A, 'cap', dict(device='one', cap=40), 10100)
    b.append(A, 'cap', dict(device='two', cap=60), 10100)
    assert balances(l) == pytest.approx({'one': 15, 'two': -15})
    assert {d: r['fair_cap'] for d, r in rows(l).items()} == {'one': 25, 'two': 75}
    a.append(B, 'profile', dict(device='one', name='one', cap=50), 100)
    assert l.summary(B)['devices'][0]['carry'] == 0


def test_new_user_does_not_inherit_earlier_borrowing(tmp_path):
    db, l, a, b = setup(tmp_path)
    use(a, b, 150, 130, 70)
    a.append(A, 'quota', snap(10001, 0, 700000), 10001)
    c = Journal(db, l, 'three')
    c.append(A, 'profile', dict(device='three', name='three', cap=20, fairness_start=10001), 10002)
    assert balances(l) == pytest.approx({'one': 15, 'two': -15, 'three': 0})
    assert sum(d['fair_cap'] for d in rows(l).values()) == pytest.approx(100)


def test_default_mode_and_adjusted_limit_are_independent_of_display(tmp_path):
    assert defaults()['quota_display'] == 'personal'
    from test_account_scope import setup as engine_setup
    e, _, step, *_ = engine_setup(tmp_path)
    step(100)
    e.config.update(auto_block=True, quota_display='personal')
    summary = dict(account=A, epoch=dict(cycle='cycle', observed_at=100), reset_pending=False,
        allocation='cycle_weighted_v1', unassigned=0,
        devices=[dict(id='one', cap=50, fair_cap=35, estimated=36, settled=36)])
    e.enforce(summary, 101)
    assert e.blocked
    summary['devices'][0].update(fair_cap=65)
    e.enforce(summary, 102)
    assert not e.blocked


def test_removed_profile_does_not_reduce_active_caps_or_leave_stale_cache(tmp_path):
    db, l, a, b = setup(tmp_path)
    old = Journal(db, l, 'old')
    old.append(A, 'profile', dict(device='old', name='old', cap=33), 100)
    use(a, b, 150, 33, 31, used=64)
    l.summary(A, 200)
    result = {d['id']: d for d in l.summary(A, 200, removed={'old'})['devices']}
    assert result['one']['fair_base_cap'] == 50
    assert result['one']['fair_cap'] == 50
    assert result['one']['estimated'] / result['one']['fair_base_cap'] * 100 == pytest.approx(66)
    assert result['two']['fair_base_cap'] == 50
    assert result['old']['removed']
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM devices WHERE id='old'").fetchone()
    restored = {d['id']: d for d in l.summary(A, 200)['devices']}
    assert restored['one']['fair_base_cap'] == pytest.approx(50/133*100)


@pytest.mark.parametrize('after,reason,carry', [(1,'重置卡重置',15),(2,'官方临时重置',0),(3,'官方临时重置',0),(None,'提前重置原因未确认',0)])
def test_reset_credit_counts_classify_early_reset_and_skip_official_debt(tmp_path,after,reason,carry):
    db,l,a,b=setup(tmp_path)
    a.append(A,'quota',dict(snap(120,0),reset_credits=1),120)
    a.append(A,'quota',dict(snap(130,0),reset_credits=2),130)
    assert len(l.summary(A,135)['devices']) == 2
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM epochs').fetchone()[0] == 1
    use(a,b,150,130,70)
    a.append(A,'quota',dict(snap(200,100),reset_credits=2),200)
    for at in (300,320):
        a.append(A,'quota',dict(snap(at,0,700000),reset_credits=after),at)
    assert l.summary(A,321)['epoch']['reason'] == reason
    assert balances(l) == pytest.approx({'one':carry,'two':-carry})
    a.project(A)
    assert balances(l) == pytest.approx({'one':carry,'two':-carry})


def test_natural_reset_with_unchanged_cards_still_settles_debt(tmp_path):
    db,l,a,b=setup(tmp_path)
    use(a,b,150,130,70)
    a.append(A,'quota',dict(snap(9990,100),reset_credits=2),9990)
    a.append(A,'quota',dict(snap(10001,0,700000),reset_credits=2),10001)
    assert l.summary(A,10002)['epoch']['reason']=='已确认周期刷新'
    assert balances(l)==pytest.approx({'one':15,'two':-15})


def test_official_temporary_reset_retains_existing_carry(tmp_path):
    db,l,a,b=setup(tmp_path)
    use(a,b,150,130,70)
    a.append(A,'quota',snap(10001,0,700000),10001)
    assert balances(l)==pytest.approx({'one':15,'two':-15})
    a.append(A,'quota',dict(snap(10020,0,700000),reset_credits=2),10020)
    use(a,b,10100,130,70,reset=700000)
    for at in (10200,10220):
        a.append(A,'quota',dict(snap(at,0,1400000),reset_credits=2),at)
    assert balances(l)==pytest.approx({'one':15,'two':-15})


def test_compensation_defaults_off_and_overuse_is_not_capped(tmp_path):
    db=Database(tmp_path/'default-off.sqlite');l=Ledger(db)
    a,b=Journal(db,l,'one'),Journal(db,l,'two')
    for j in (a,b):j.append(A,'profile',dict(device=j.device,name=j.device,cap=50),100)
    a.append(A,'quota',snap(100,0),100)
    use(a,b,150,110,90)
    r=rows(l)
    assert r['one']['estimated']/r['one']['fair_base_cap']*100==pytest.approx(110)
    assert l.summary(A)['compensation_enabled'] is False
    a.append(A,'quota',snap(10001,0,700000),10001)
    assert balances(l)=={'one':0,'two':0}
    assert {d:r['fair_cap'] for d,r in rows(l).items()}=={'one':50,'two':50}


def test_disabling_compensation_and_reenabling_never_bills_disabled_cycles(tmp_path):
    db,l,a,b=setup(tmp_path)
    use(a,b,150,130,70)
    a.append(A,'quota',snap(10001,0,700000),10001)
    assert balances(l)=={'one':15,'two':-15}
    a.append(A,'profile',dict(device='one',name='one',cap=50,compensation_enabled=False),10010)
    assert balances(l)=={'one':0,'two':0}
    use(a,b,10100,130,70,reset=700000)
    a.append(A,'quota',snap(700001,0,1400000),700001)
    assert balances(l)=={'one':0,'two':0}
    a.append(A,'profile',dict(device='one',name='one',cap=50,compensation_enabled=True),700010)
    assert balances(l)=={'one':0,'two':0}
    peerdb=Database(tmp_path/'switch-peer.sqlite');peer=Journal(peerdb,Ledger(peerdb),'peer')
    peer.merge(A,a.since(A,{},limit=60))
    assert peer.ledger.summary(A)['compensation_enabled'] is True
    assert balances(peer.ledger)==balances(l)


def test_compensation_switch_rejects_non_boolean_values(tmp_path):
    _,_,a,_=setup(tmp_path)
    with pytest.raises(ValueError,match='补偿开关'):
        a.append(A,'profile',dict(device='one',name='one',cap=50,compensation_enabled='false'),101)
