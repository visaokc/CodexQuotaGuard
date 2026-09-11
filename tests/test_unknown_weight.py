import pytest

from test_shared_billing import A, B, add_event, close_samples, setup_group
from quota_guard.shared_policy import load_rules, publish_change
from quota_guard.shared_quota import attribution, accounting


def enable(db, journals):
    return publish_change(journals['one'], load_rules(db, [A, B], 300), 'one', 'one', 50,
                          dict(unknown_weight='interval_average_v1'), 300)


@pytest.mark.parametrize('model', ['gpt-6-astra', 'codex-auto-review'])
def test_authorized_unknown_weight_conserves_quota_and_preserves_unknown_token_records(tmp_path, model):
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, account=B, model=model)
    unknown = add_event(journals, account=B, device='three', at=205, model='codex-auto-review')
    journals['one'].append(B, 'quota', dict(account=B, used=10, reset_at=850, at=210), 210)
    close_samples(journals)
    rules = load_rules(db, [A, B], 400)
    assert accounting(rules, attribution(db, rules, 400), 400)['status']=='syncing'
    enable(db, journals)
    rules = load_rules(db, [A, B], 400)
    data = attribution(db, rules, 400)
    book = accounting(rules, data, 400)
    assert book['status']=='active'
    assert [p['fair_usage'] for p in book['people'].values()] == pytest.approx([5,0,5])
    assert sum(e['units'] for e in data['events'])==10*10**9
    assert all(0<=e['cache_quota']<=e['quota'] for e in data['events'])
    with db.connect() as connection:
        row = connection.execute('SELECT model,tokens FROM events WHERE id=?', (unknown['id'],)).fetchone()
    assert tuple(row)==('codex-auto-review', 1100)


def test_unknown_weight_does_not_mask_missing_token_details_or_bad_policy(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    event = add_event(journals, account=B, model='codex-auto-review')
    journals['one'].append(B, 'quota', dict(account=B, used=1, reset_at=850, at=210), 210)
    close_samples(journals)
    enable(db, journals)
    with db.connect() as connection:
        connection.execute('DELETE FROM event_details WHERE id=?', (event['id'],))
    rules = load_rules(db, [A, B], 400)
    assert accounting(rules, attribution(db, rules, 400), 400)['status']=='syncing'
    with pytest.raises(ValueError, match='未知模型'):
        publish_change(journals['one'], rules, 'one', 'one', 50, dict(unknown_weight='free'), 400)
