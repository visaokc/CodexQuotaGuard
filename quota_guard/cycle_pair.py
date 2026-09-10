"""Choose overlapping account cycles without mixing their consumption boundaries."""


def _end(row):
    return row['ended'] if row['ended'] is not None else row['reset_at']


def _selected(row, now):
    return dict(row, matched_until=min(now, row['ended']) if row['ended'] is not None else now)


def choose_cycles(database, accounts, now):
    """Return a current-anchored pair; planned overlap never extends consumption."""
    accounts = sorted(set(accounts))
    result = dict(status='waiting', complete=False, reason='missing_account_cycle',
                  overlap_seconds=0, account_ids=accounts, cycles=[])
    if len(accounts) > 2:
        return dict(result, status='unsupported', reason='more_than_two_accounts')
    histories = {}
    with database.connect() as db:
        for account in accounts:
            rows = [dict(row) for row in db.execute(
                'SELECT * FROM epochs WHERE account=? AND started<=? ORDER BY started,id', (account, now))]
            histories[account] = [row for row in rows if _end(row) > row['started']]
    if len(accounts) < 2 or any(not histories[account] for account in accounts):
        result['cycles'] = [_selected(histories[account][-1], now)
                            for account in accounts if histories[account]]
        if len(accounts) < 2:
            result['reason'] = 'need_two_accounts'
        return result
    active = {account: next((row['id'] for row in reversed(rows) if row['ended'] is None), None)
              for account, rows in histories.items()}
    if all(value is None for value in active.values()):
        return dict(result, reason='no_active_cycle')
    best, best_rank = None, None
    for left in histories[accounts[0]]:
        for right in histories[accounts[1]]:
            if left['id'] != active[accounts[0]] and right['id'] != active[accounts[1]]:
                continue
            overlap = min(_end(left), _end(right))-max(left['started'], right['started'])
            if overlap <= 0:
                continue
            # Later union end/start wins ties, then stable account/cycle fields.
            rank = (overlap, max(_end(left), _end(right)), min(left['started'], right['started']),
                    tuple((row['account'], row['started'], _end(row), row['id']) for row in (left, right)))
            if best_rank is None or rank > best_rank:
                best, best_rank = (left, right), rank
    if best is None:
        return dict(result, reason='no_overlap')
    return dict(result, status='matched', complete=True, reason='', overlap_seconds=best_rank[0],
                cycles=[_selected(row, now) for row in best])
