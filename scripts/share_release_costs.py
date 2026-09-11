"""Preview/apply the explicitly authorized repair interval before release upload."""
import argparse
import ctypes
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.pairing import load_config
from quota_guard.shared_costs import uncovered
from quota_guard.shared_policy import load_rules, person_for, publish_change
from quota_guard.shared_quota import attribution, accounting
from quota_guard.storage import Database


def copy_database(source, target):
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as old, closing(sqlite3.connect(target)) as new:
        old.backup(new)
        assert new.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--account-index', type=int, choices=(1, 2), required=True)
    parser.add_argument('--since', required=True, help='Unix timestamp; cycle only for the initial authorized adjustment')
    parser.add_argument('--through', type=float)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--average-unknown', action='store_true', help='Enable the explicitly authorized average-weight convention')
    parser.add_argument('--enable-rollover', action='store_true', help='Enable authorized unused-balance carry from this rule onward')
    parser.add_argument('--expire-unused', action='store_true', help='End unused carry; each account expires its unused inventory at reset')
    args = parser.parse_args()
    if args.enable_rollover and args.expire_unused:
        parser.error('Choose only one unused-balance rule')
    folder = args.data_dir.resolve()
    now = time.time()
    through = args.through if args.through is not None else now
    mutex = None
    if args.apply and os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, 'Local\\CodexQuotaGuard-'+hashlib.sha256(str(folder).encode()).hexdigest()[:16])
        if not mutex or ctypes.get_last_error() == 183:
            raise SystemExit('Stop the quota client before applying a release adjustment')
    cfg = load_config(folder/'settings.json')
    source = folder/('group-v2-'+hashlib.sha256(cfg['group_secret'].encode()).hexdigest()[:20]+'.sqlite')
    with tempfile.TemporaryDirectory(prefix='cqg-release-cost-') as temp:
        clone = Path(temp)/'preview.sqlite'
        copy_database(source, clone)
        database = Database(clone)
        with database.connect() as db:
            accounts = [row[0] for row in db.execute('SELECT DISTINCT account FROM epochs')]
        rules = load_rules(database, accounts, now)
        assert rules['status'] == 'ready' and rules['policy']['admin'] == cfg['device_id']
        account = rules['accounts'][args.account_index-1]
        person = person_for(rules, cfg['device_id'], now)
        with database.connect() as db:
            cycle = db.execute('SELECT started FROM epochs WHERE account=? AND started<=? ORDER BY started DESC LIMIT 1', (account, through)).fetchone()
            since = cycle['started'] if args.since == 'cycle' else float(args.since)
            assert 0 <= since < through <= now
            rows = [dict(row) for row in db.execute('SELECT * FROM events WHERE account=? AND ts>? AND ts<=?', (account, since, through))
                    if person_for(rules, row['device'], row['ts']) == person]
        previous = rules['policy'].get('shared_costs', [])
        additions = uncovered(previous, account, person, since, through)
        additions = [cost for cost in additions if any(cost['since'] < row['ts'] <= cost['through'] for row in rows)]
        selected = [row for row in rows if any(cost['since'] < row['ts'] <= cost['through'] for cost in additions)]
        report = dict(applied=False, account_index=args.account_index, person=person, since=since, through=through,
                      events=len(selected), tokens=sum(row['tokens'] for row in selected), intervals=additions,
                      duplicate=not additions, split='1/3 each')
        changes = dict(shared_costs=previous+additions) if additions else {}
        if args.average_unknown and rules['policy'].get('unknown_weight') != 'interval_average_v1':
            changes['unknown_weight'] = 'interval_average_v1'
        if args.enable_rollover and 'rollover_since' not in rules['policy']:
            changes['rollover_since'] = now
        if args.expire_unused and 'unused_expiry_from' not in rules['policy']:
            changes['unused_expiry_from'] = now
        report['rollover_since'] = changes.get('rollover_since', rules['policy'].get('rollover_since'))
        report['unused_expiry_from'] = changes.get('unused_expiry_from', rules['policy'].get('unused_expiry_from'))
        if changes:
            publish_change(Journal(database, Ledger(database), cfg['device_id']), rules, cfg['device_id'], cfg['name'], cfg['quota'], changes, now)
            revised = load_rules(database, accounts, now)
            assert revised['status'] == 'ready' and revised['policy'].get('shared_costs', []) == previous+additions
            attributed = attribution(database, revised, now)
            projected = accounting(revised, attributed, now)
            report['billing_status'] = projected['status']
            report['confirmed_shared_quota'] = sum(row['quota'] for row in attributed['events']
                if row.get('shared_cost') and row['account'] == account
                and any(cost['since'] < row['ts'] <= cost['through'] for cost in additions))
            report['available_percent'] = {key: round(value['available']/value['available_cap']*100, 3)
                if value['available'] is not None and value['available_cap'] else None
                for key, value in projected['people'].items()}
            if args.apply:
                backup = folder/'backups'/('release-cost-'+time.strftime('%Y%m%d-%H%M%S'))
                backup.mkdir(parents=True)
                copy_database(source, backup/source.name)
                shutil.copy2(folder/'settings.json', backup/'settings.json.bak')
                live = Database(source)
                fresh = load_rules(live, accounts, now)
                assert fresh['policy'] == rules['policy'], 'Rule changed after preview'
                with live.connect() as db:
                    before = [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY id')]
                publish_change(Journal(live, Ledger(live), cfg['device_id']), fresh, cfg['device_id'], cfg['name'], cfg['quota'], changes, now)
                confirmed = load_rules(live, accounts, now)
                assert confirmed['status'] == 'ready' and confirmed['policy'] == revised['policy']
                assert accounting(confirmed, attribution(live, confirmed, now), now) == projected
                with live.connect() as db:
                    assert before == [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY id')]
                report.update(applied=True, backup=str(backup), revision=confirmed['policy']['revision'])
                (backup/'adjustment.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))


if __name__ == '__main__':
    main()
