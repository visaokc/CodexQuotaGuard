import pytest

from quota_guard.cycle_pair import choose_cycles
from quota_guard.storage import Database


A, B, C = 'a'*64, 'b'*64, 'c'*64


def epoch(database, account, started, end, closed=False):
    with database.connect() as db:
        return db.execute('INSERT INTO epochs(account,started,ended,baseline,used,reset_at,observed_at,reason) VALUES (?,?,?,?,?,?,?,?)',
                          (account, started, end if closed else None, 0, 10, end, started, 'reset')).lastrowid


def selected(result):
    return [(row['account'], row['started'], row['ended']) for row in result['cycles']]


def test_offset_current_cycles_use_planned_overlap_but_never_future_consumption(tmp_path):
    database = Database(tmp_path/'current.sqlite')
    epoch(database, A, 100, 800)
    epoch(database, B, 200, 900)
    result = choose_cycles(database, [B, A], 250)
    assert result['status'] == 'matched' and result['complete']
    assert result['account_ids'] == [A, B]
    assert result['overlap_seconds'] == 600
    assert selected(result) == [(A, 100, None), (B, 200, None)]
    assert [row['matched_until'] for row in result['cycles']] == [250, 250]


def test_current_cycle_pairs_with_other_previous_cycle_when_overlap_is_greater(tmp_path):
    database = Database(tmp_path/'previous.sqlite')
    epoch(database, A, 100, 200)
    epoch(database, B, 60, 160, closed=True)
    epoch(database, B, 160, 260)
    result = choose_cycles(database, [A, B], 180)
    assert result['overlap_seconds'] == 60
    assert selected(result) == [(A, 100, None), (B, 60, 160)]
    assert [row['matched_until'] for row in result['cycles']] == [180, 160]
    # Keep the complete chosen B period, including its pre-overlap part.
    assert result['cycles'][1]['started'] == 60


def test_old_old_pair_is_excluded_even_with_much_larger_overlap(tmp_path):
    database = Database(tmp_path/'old.sqlite')
    for account in (A, B):
        epoch(database, account, -1000, 0, closed=True)
    epoch(database, A, 100, 200)
    epoch(database, B, 160, 260)
    result = choose_cycles(database, [A, B], 180)
    assert result['overlap_seconds'] == 40
    assert selected(result) == [(A, 100, None), (B, 160, None)]


def test_tie_chooses_more_recent_pair_and_is_independent_of_account_or_insert_order(tmp_path):
    rows = [(A, 100, 200, False), (B, 50, 150, True), (B, 150, 250, False)]
    outputs = []
    for n, ordered in enumerate((rows, list(reversed(rows)))):
        database = Database(tmp_path/f'tie-{n}.sqlite')
        for row in ordered:
            epoch(database, *row)
        result = choose_cycles(database, [B, A] if n else [A, B], 180)
        outputs.append(selected(result))
        assert result['complete'] and result['overlap_seconds'] == 50
    assert outputs == [[(A, 100, None), (B, 150, None)]]*2


def test_reset_card_short_cycle_can_leave_previous_cycle_as_best_match(tmp_path):
    database = Database(tmp_path/'card.sqlite')
    epoch(database, A, 100, 170, closed=True)
    epoch(database, A, 170, 270)
    epoch(database, B, 100, 200)
    result = choose_cycles(database, [A, B], 180)
    assert result['overlap_seconds'] == 70
    assert selected(result) == [(A, 100, 170), (B, 100, None)]


def test_missing_account_keeps_only_known_single_cycle_as_incomplete(tmp_path):
    database = Database(tmp_path/'missing.sqlite')
    epoch(database, A, 100, 200)
    result = choose_cycles(database, [A, B], 180)
    assert result['status'] == 'waiting' and not result['complete']
    assert result['reason'] == 'missing_account_cycle'
    assert selected(result) == [(A, 100, None)]
    assert not choose_cycles(database, [A], 180)['complete']
    assert choose_cycles(database, [], 180)['cycles'] == []


@pytest.mark.parametrize('first_end', [50, 100])
def test_non_overlapping_or_touching_cycles_are_not_a_complete_pair(tmp_path, first_end):
    database = Database(tmp_path/'no-overlap.sqlite')
    epoch(database, A, 0, first_end)
    epoch(database, B, 100, 250)
    result = choose_cycles(database, [A, B], 180)
    assert result['status'] == 'waiting' and result['reason'] == 'no_overlap'
    assert not result['complete'] and not result['cycles']


def test_only_closed_history_cannot_become_current_pair(tmp_path):
    database = Database(tmp_path/'closed.sqlite')
    for account in (A, B):
        epoch(database, account, 10, 100, closed=True)
    result = choose_cycles(database, [A, B], 180)
    assert not result['complete'] and result['reason'] == 'no_active_cycle'


def test_future_cycles_and_unrequested_accounts_cannot_influence_selection(tmp_path):
    database = Database(tmp_path/'future.sqlite')
    epoch(database, A, 100, 200)
    epoch(database, B, 120, 220)
    for account in (A, B):
        epoch(database, account, 500, 1500)
    epoch(database, C, 0, 2000)
    result = choose_cycles(database, [A, B], 180)
    assert selected(result) == [(A, 100, None), (B, 120, None)]
    assert all(row['matched_until'] <= 180 for row in result['cycles'])
    unsupported = choose_cycles(database, [A, B, C], 180)
    assert unsupported['status'] == 'unsupported'
    assert not unsupported['complete'] and not unsupported['cycles']


def test_closed_end_is_clamped_at_observation_time_for_consumption(tmp_path):
    database = Database(tmp_path/'clamped.sqlite')
    epoch(database, A, 100, 300)
    epoch(database, B, 120, 250, closed=True)
    result = choose_cycles(database, [A, B], 180)
    assert result['complete']
    assert [row['matched_until'] for row in result['cycles']] == [180, 180]
