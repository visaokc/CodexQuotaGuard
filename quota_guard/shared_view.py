"""Multi-account views with one shared official attribution and pooled ledger."""
from .analytics import usage
from .cycle_statistics import cycle_statistics
from .cycle_pair import choose_cycles


def person_rows(database, row, rules, now):
    from .shared_policy import person_for
    bindings = [b for b in rules['bindings'] if b['device'] == row['device']
                and row.get('first_at', now) < b['since'] <= row.get('last_at', now)]
    if not bindings:
        return [dict(row, source_device=row['device'], device=person_for(rules, row['device'], row.get('first_at', now)) or row['device'])]
    groups = {}
    with database.connect() as db:
        events = list(db.execute('''SELECT e.*,d.input_tokens,d.cached_input_tokens,d.output_tokens,d.reasoning_output_tokens
            FROM events e LEFT JOIN event_details d USING(id) WHERE account=? AND device=? AND model=? AND ts>=? AND ts<=? ORDER BY ts,id''',
            (row['account'], row['device'], row['model'], row['first_at'], row['last_at'])))
    for event in events:
        person = person_for(rules, event['device'], event['ts']) or event['device']
        groups.setdefault(person, []).append(event)
    output = []
    for person, events in groups.items():
        value = dict(row, device=person, source_device=row['device'], first_at=events[0]['ts'], last_at=events[-1]['ts'])
        for key in ('tokens', 'weight'):
            value[key] = sum(e[key] for e in events)
        for field, key in (('input_tokens','input_tokens'),('output_tokens','output_tokens'),('cache_tokens','cached_input_tokens'),('reasoning_tokens','reasoning_output_tokens')):
            known = [e[key] for e in events if e[key] is not None]
            value[field] = sum(known) if known else None
        value.update(event_count=len(events), detail_count=sum(e['input_tokens'] is not None for e in events),
                     detail_missing=sum(e['tokens'] for e in events if e['input_tokens'] is None),
                     unknown=sum(e['tokens'] for e in events if not e['known']),
                     reasoning_count=sum(e['reasoning_output_tokens'] is not None for e in events),
                     reasoning_missing=sum(e['output_tokens'] or 0 for e in events if e['reasoning_output_tokens'] is None))
        output.append(value)
    return output


def shared_usage(database, scope, accounts, now=None, rules=None, **options):
    import time
    now = time.time() if now is None else now
    sources = [(account, usage(database, account, now, **options)) for account in sorted(accounts)]
    if not sources:
        sources = [('', usage(database, '', now, **options))]
    pair = choose_cycles(database, accounts, now)
    for cycle in pair['cycles']:
        cycle.update(label=accounts[cycle['account']], usage_until=cycle['matched_until'])
    starts = [cycle['started'] for cycle in pair['cycles']]
    result = dict(account=scope, account_ids=sorted(accounts), at=now,
                  cycle_start=min(starts) if starts else None,
                  statistics_start=min((data.get('statistics_start', now) for _, data in sources), default=now),
                  models=sorted({model for _, data in sources for model in data['models']}),
                  windows={}, cycles=[], cycle_pair=pair, quota_unavailable='计费待启用')
    for key in sources[0][1]['windows']:
        windows = [data['windows'][key] for _, data in sources]
        first = windows[0]
        window = dict(start=first['start'], step=first['step'], count=first['count'],
                      rows=[], quota_rows=[], quota_pending_rows=[], quota_estimate_rows=[],
                      quota_ready=False, quota_available=False)
        if window['count'] == 1:
            window['start'] = min(w['start'] for w in windows)
            window['step'] = max(1, now-window['start'])
        for account, data in sources:
            source = data['windows'][key]
            if window['count'] != 1 and (source['start'], source['step'], source['count']) != (window['start'], window['step'], window['count']):
                raise ValueError('共享图表的时间桶未对齐')
            window['rows'].extend(dict(row, account=account) for row in source['rows'])
        result['windows'][key] = window
    # Each account contributes its complete selected cycle, not just the
    # overlapping portion and not consumption from its newer adjacent cycle.
    cycle_window = result['windows']['cycle']
    cycle_window.update(start=min(starts) if starts else now,
                        step=max(1, now-min(starts)) if starts else 1, rows=[])
    with database.connect() as db:
        for cycle in pair['cycles']:
            rows = db.execute('''SELECT device, model, 0 AS bucket, SUM(tokens) AS tokens,
                SUM(weight) AS weight, SUM(CASE WHEN known=0 THEN tokens ELSE 0 END) AS unknown,
                COUNT(*) AS event_count, COUNT(input_tokens) AS detail_count,
                MIN(ts) AS first_at, MAX(ts) AS last_at, SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens, SUM(reasoning_output_tokens) AS reasoning_tokens,
                COUNT(reasoning_output_tokens) AS reasoning_count,
                SUM(CASE WHEN reasoning_output_tokens IS NULL THEN COALESCE(output_tokens,0) ELSE 0 END) AS reasoning_missing,
                SUM(cached_input_tokens) AS cache_tokens,
                SUM(CASE WHEN input_tokens IS NULL THEN tokens ELSE 0 END) AS detail_missing
                FROM events LEFT JOIN event_details USING(id)
                WHERE account=? AND ts>? AND ts<=? GROUP BY device, model ORDER BY device, model''',
                (cycle['account'], cycle['started'], cycle['matched_until'])).fetchall()
            cycle_window['rows'].extend(dict(row, account=cycle['account']) for row in rows)
    if not options:
        for account in sorted(accounts):
            for row in cycle_statistics(database, account, now)['rows']:
                result['cycles'].append(dict(row, id=account+':'+str(row['id']),
                                             account=account, account_label=accounts[account]))
        result['cycles'].sort(key=lambda row: (row['started'], row['account']), reverse=True)
    if rules and rules.get('policy'):
        from .shared_quota import attribution, accounting
        from .shared_policy import person_for
        attributed = attribution(database, rules, now)
        for cycle in result['cycles']:
            cause = attributed['overrides'].get((cycle['account'], cycle['started']))
            if cause:
                cycle['reset_type'] = {'natural':'自然重置','card':'重置卡','official':'官方临时重置'}.get(cause, cause)
        result['quota_unavailable'] = ''
        result['billing'] = accounting(rules, attributed, now)
        for name, window in result['windows'].items():
            window['rows'] = [mapped for row in window['rows'] for mapped in person_rows(database, row, rules, now)]
            quotas = {}
            for event in attributed['events']:
                if name == 'cycle':
                    selected = next((c for c in pair['cycles'] if c['account'] == event['account']), None)
                    include = selected and selected['started'] < event['ts'] <= selected['matched_until']
                else:
                    end = min(now, options.get('hour_end', now)) if name in ('hour','hour_curve') else min(now, options.get('day_end', now)) if name == 'day' else now
                    include = window['start'] <= event['ts'] < end
                if not include:
                    continue
                bucket = min(window['count']-1, int((event['ts']-window['start'])/window['step']))
                key = event['account'], event['device'], event['model'], bucket
                row = quotas.setdefault(key, dict(account=key[0], device=key[1], model=key[2], bucket=bucket, quota=0., cache_quota=0.))
                row['quota'] += event['quota']
                row['cache_quota'] += event['cache_quota']
            window['quota_rows'] = list(quotas.values())
            last = {a: max((s['end'] for s in attributed['streams'] if s['account'] == a and s['ready']), default=0) for a in rules['accounts']}
            for row in window['rows']:
                pending = row.get('last_at', 0) > last.get(row['account'], 0) or any(
                    gap['account'] == row['account'] and gap['end'] >= row.get('first_at', now)
                    and gap['start'] < row.get('last_at', now) for gap in attributed['gaps'])
                if pending:
                    window['quota_pending_rows'].append({k: row[k] for k in ('account','device','model','bucket')})
            window['quota_ready'] = window['quota_available'] = bool(attributed['epochs'])
        result['rules'] = rules
    return result


def shared_overview(database, scope, accounts, members, local, now, analytics, removed=()):
    removed = set(removed)
    people = {device: dict(value) for device, value in members.items() if device not in removed}
    records = {}
    cards = []
    with database.connect() as db:
        for account, label in sorted(accounts.items()):
            row = db.execute('SELECT * FROM epochs WHERE account=? ORDER BY id DESC LIMIT 1', (account,)).fetchone()
            pending = db.execute('SELECT 1 FROM meta WHERE key=?', ('reset_candidate:'+account,)).fetchone() is not None
            cards.append(dict(account=account, label=label, epoch=dict(row) if row else None,
                              reset_pending=pending))
            for row in db.execute('SELECT * FROM devices WHERE account=?', (account,)):
                if row['id'] in removed:
                    continue
                person = people.setdefault(row['id'], dict(id=row['id'], name=row['name'], online=False, accounts=[]))
                if account not in person.setdefault('accounts', []):
                    person['accounts'].append(account)
                if row['id'] not in records or row['seen'] > records[row['id']]['seen']:
                    records[row['id']] = dict(row)
    totals = {}
    for row in analytics['windows']['cycle']['rows']:
        totals[row['device']] = totals.get(row['device'], 0) + row['tokens']
    devices = []
    for device, person in sorted(people.items()):
        record = records.get(device, {})
        online = device == local or person.get('online', False)
        current = person.get('current_account')
        activity = record if online and current == record.get('account') else {}
        devices.append(dict(id=device, name=person.get('name') or record.get('name') or device[:8],
            cap=200/3, fair_base_cap=200/3, fair_cap=None, carry=0,
            estimated=None, settled=None, quota_pending=True, tokens=totals.get(device, 0),
            weight=0, unknown_tokens=0, seen=now if online else record.get('seen', 0),
            scan_at=record.get('scan_at', 0), online=online, logged_in=bool(current), removed=False,
            active=activity.get('active', 0), uncertain=activity.get('uncertain', 0),
            unbound_active=activity.get('unbound_active', 0), unbound_uncertain=activity.get('unbound_uncertain', 0)))
        person.update(id=device, local=device == local, online=online)
    summary = dict(account=scope, epoch=None, devices=devices, server_time=now,
                   compensation_enabled=False, allocation='shared_preparing', reset_pending=False,
                   unassigned=0, provisional=0, attribution_gaps=[],
                   token_budget=dict(sampled_tokens=sum(d['tokens'] for d in devices), total_tokens=None,
                                     used_tokens=None, source='两账号合计 · 统一计费待启用'))
    if analytics.get('rules', {}).get('policy'):
        from .shared_policy import PERSONS, person_for
        rules, billing = analytics['rules'], analytics['billing']
        cards.sort(key=lambda card: rules['accounts'].index(card['account']))
        people_rows = []
        for index, person in enumerate(PERSONS):
            attached = sorted(device for device in people if person_for(rules, device, now) == person)
            live = [people[device] for device in attached if people[device].get('online') or device == local]
            device_rows = [d for d in devices if d['id'] in attached]
            quota = sum(row['quota'] for row in analytics['windows']['cycle']['quota_rows'] if row['device'] == person)
            value = billing.get('people', {}).get(person, {})
            debt, available = value.get('debt'), value.get('available')
            pending = any(row['device'] == person for row in analytics['windows']['cycle']['quota_pending_rows'])
            name = next((people[d]['name'] for d in attached if people[d].get('name')), '待加入成员' if index == 2 else '成员'+str(index+1))
            people_rows.append(dict(id=person, name=name, avatar=person+'.jpg', device_ids=attached,
                local=local in attached, online=bool(live), joined=bool(attached), cap=200/3, fair_base_cap=200/3,
                fair_cap=200/3, estimated=quota, settled=quota, quota_pending=pending,
                carry=200/3-available+debt-quota if debt is not None and available is not None else 0,
                fair_usage=200/3-available+debt if debt is not None and available is not None else None,
                tokens=totals.get(person, 0), available=available, debt=debt, pending_debt=value.get('pending'),
                confirmed_debt=value.get('confirmed'), by_account=value.get('by_account', {}), removed=False,
                active=sum(d.get('active', 0) for d in device_rows), uncertain=sum(d.get('uncertain', 0) for d in device_rows),
                unbound_active=sum(d.get('unbound_active', 0) for d in device_rows), unbound_uncertain=sum(d.get('unbound_uncertain', 0) for d in device_rows),
                seen=max((d.get('seen', 0) for d in device_rows), default=0), logged_in=bool(live)))
        summary.update(devices=people_rows, compensation_enabled=billing['compensation_enabled'],
                       allocation='shared_official_v1', billing_status=billing['status'], billing_reason=billing['reason'])
        summary['token_budget']['sampled_tokens'] = sum(row['tokens'] for row in people_rows)
        summary['token_budget']['source'] = '两账号完整配对周期 · Token 实记'
        return dict(summary=summary, account_summaries=cards, members=people_rows, billing=billing)
    return dict(summary=summary, account_summaries=cards, members=list(people.values()))
