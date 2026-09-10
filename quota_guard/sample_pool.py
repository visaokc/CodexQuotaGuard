"""Replicated scan checkpoints for common, time-aligned budget samples."""
import json


def sample_checkpoints(db, account):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts'").fetchone():
        return None
    rows = db.execute("""SELECT origin,seq,payload FROM facts WHERE account=? AND kind='profile'
        AND json_type(payload,'$.sample_checkpoint')='object' ORDER BY origin,seq""", (account,)).fetchall()
    if not rows:
        return None
    # A checkpoint arriving before its earlier event batches is not complete.
    vectors = {}
    for row in db.execute('SELECT origin,seq FROM facts WHERE account=? ORDER BY origin,seq', (account,)):
        if row['seq'] == vectors.get(row['origin'], 0)+1:
            vectors[row['origin']] = row['seq']
    latest, result = {}, {}
    for row in rows:
        through = json.loads(row['payload'])['sample_checkpoint']['through']
        result[row['origin']] = dict(through=0, declared_through=through)
        if row['seq'] <= vectors.get(row['origin'], 0):
            latest[row['origin']] = (row['seq'], through)
    for origin, (seq, through) in latest.items():
        # Late historical corrections must be rescanned and checkpointed too.
        changed = db.execute("""SELECT 1 FROM facts f,json_each(f.payload) e
            WHERE f.account=? AND f.origin=? AND f.seq>? AND f.kind='events'
            AND json_extract(e.value,'$.ts')<=? LIMIT 1""", (account, origin, seq, through)).fetchone()
        if not changed:
            result[origin]['through'] = through
    return result


def checkpoint_input(db, account, device, until):
    through = db.execute('''SELECT MAX(s.end) FROM segments s JOIN epochs e ON e.id=s.epoch
        WHERE e.account=? AND s.end<=?''', (account, until)).fetchone()[0]
    if through is None:
        return None
    revision = db.execute("""SELECT COALESCE(MAX(f.seq),0) FROM facts f,json_each(f.payload) e
        WHERE f.account=? AND f.origin=? AND f.kind='events'
        AND json_extract(e.value,'$.ts')<=?""", (account, device, through)).fetchone()[0]
    return dict(through=through, revision=revision)
