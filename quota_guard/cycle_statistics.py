"""Persistent cycle comparisons from local samples, not fixed official token limits."""
import json
import time

from .token_budget import estimate_budget


def cycle_statistics(database, account, now=None):
    now = time.time() if now is None else now
    rows = []
    with database.connect() as db:
        saved = db.execute('SELECT value FROM meta WHERE key=?', ('statistics_start:'+account,)).fetchone()
        start = float(json.loads(saved[0])) if saved else 0
        pending = bool(db.execute('SELECT 1 FROM meta WHERE key=?', ('reset_candidate:'+account,)).fetchone())
        devices = {r['id']: dict(r) for r in db.execute('SELECT * FROM devices WHERE account=?', (account,))}
        epochs = db.execute('''SELECT * FROM epochs WHERE account=? AND started<=?
            AND (ended IS NULL OR ended>?) ORDER BY started,reset_at''', (account, now, start)).fetchall()
        for stored in epochs:
            epoch = dict(stored)
            cycle = f"{epoch['reset_at']:.6f}:{epoch['started']:.6f}"
            epoch['cycle'] = cycle
            is_current = epoch['ended'] is None
            end = min(now, epoch['ended']) if not is_current else now
            events = list(db.execute('''SELECT * FROM events WHERE account=? AND ts>? AND ts<=?
                ORDER BY ts,id''', (account, max(start, epoch['started']), end)))
            segments = list(db.execute('SELECT * FROM segments WHERE epoch=? ORDER BY end,id', (epoch['id'],)))
            # A mid-cycle statistics cutoff has no matching quota baseline. Keep
            # its observed tokens, but do not extrapolate the full-cycle delta.
            incomplete = start > epoch['started']
            budget, calibration = estimate_budget(epoch, events, devices, segments, now,
                previous=None, reset_pending=(is_current and pending) or incomplete)
            models = {}
            for event in events:
                models[event['model']] = models.get(event['model'], 0)+event['tokens']
            rows.append(dict(id=cycle, started=epoch['started'], ended=epoch['ended'],
                reset_at=epoch['reset_at'], used_percent=epoch['used'], baseline_percent=epoch['baseline'],
                sampled_tokens=budget['sampled_tokens'], total_tokens=budget['total_tokens'],
                source=budget['source'], sample_tokens=calibration.get('sample_tokens'),
                sample_percent=calibration.get('sample_percent'), is_current=is_current,
                change_percent=None, models=[dict(model=model, tokens=tokens)
                    for model, tokens in sorted(models.items(), key=lambda item: (-item[1], item[0]))]))
    references = []
    for row in rows:
        recent = references[-3:]
        reference = sum(r['total_tokens'] for r in recent)/len(recent) if recent else None
        row.update(reference_count=len(recent), reference_total_tokens=reference,
                   reference_starts=[r['started'] for r in recent],
                   reduction_tokens=None, reduction_percent=None)
        if reference and row['total_tokens'] is not None:
            row['reduction_tokens'] = reference-row['total_tokens']
            row['reduction_percent'] = row['reduction_tokens']/reference*100
        # The cycle being evaluated never participates in its own reference.
        if not row['is_current'] and row['ended'] <= now and row['total_tokens']:
            references.append(row)
    for previous, current in zip(rows, rows[1:]):
        if previous['total_tokens'] and current['total_tokens'] is not None:
            current['change_percent'] = (current['total_tokens']/previous['total_tokens']-1)*100
    return dict(rows=list(reversed(rows)))
