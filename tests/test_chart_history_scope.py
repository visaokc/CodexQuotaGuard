from contextlib import contextmanager

import pytest

from quota_guard.analytics import usage
from quota_guard.shared_policy import load_rules
from quota_guard.shared_view import shared_usage
from test_shared_billing import A, B, setup_group, add_event, close_samples


class TracedDatabase:
    def __init__(self, database):
        self.database = database
        self.path = database.path
        self.statements = []

    @contextmanager
    def connect(self):
        with self.database.connect() as connection:
            connection.set_trace_callback(self.statements.append)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)


@pytest.mark.parametrize('period', ['hour', 'six_hours', 'twelve_hours', 'day'])
@pytest.mark.parametrize('shared', [False, True])
def test_history_selection_preserves_requested_data_without_querying_other_windows(tmp_path, period, shared):
    database, journals, _ = setup_group(tmp_path)
    add_event(journals, at=200)
    add_event(journals, account=B, device='two', at=205, model='gpt-5.6-sol')
    journals['one'].append(A, 'quota', dict(account=A, used=5, reset_at=800, at=210), 210)
    close_samples(journals)
    add_event(journals, at=300)
    add_event(journals, account=B, device='three', at=310, model='unrecognized')
    rules = load_rules(database, [A, B], 400)
    rules['policy']['shared_costs'] = [dict(account=A, person='person1', since=190, through=220)]
    selected = ('hour', 'hour_curve') if period == 'hour' else (period,)
    options = (dict(hour_end=350, hour_buffer=True) if period == 'hour' else
               dict(day_end=350, day_buffer=True) if period == 'day' else
               dict(rolling_period=period, rolling_end=350, rolling_buffer=True))
    traced = TracedDatabase(database)
    if shared:
        args = (traced, 'group:test', {A:'account1', B:'account2'}, 400)
        options['rules'] = rules
        function = shared_usage
    else:
        args = (traced, A, 400)
        function = usage
    full = function(*args, **options)
    traced.statements.clear()
    result = function(*args, **options, selected_windows=selected)
    assert result['windows'] == {key:full['windows'][key] for key in selected}
    assert result.get('quota_estimate') == full.get('quota_estimate')
    if shared:
        assert result['account_estimates'] == full['account_estimates']
        assert result['billing'] == full['billing']
        assert result['official_events'] == full['official_events']
    aggregation_queries = [sql for sql in traced.statements if 'GROUP BY device, model, bucket' in sql]
    assert len(aggregation_queries) == len(selected)*(2 if shared else 1)
    assert not any('0 AS bucket' in sql for sql in traced.statements)
