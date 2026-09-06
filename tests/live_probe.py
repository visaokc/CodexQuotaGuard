"""Read-only real Codex path. Writes ONLY the explicitly supplied test data directory."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_guard.engine import Engine
from quota_guard.storage import Database, defaults
from quota_guard.accounts import enroll
from quota_guard.quota import identity


def main():
    folder = Path('work/live-probe')
    cfg = defaults()
    cfg.update(auto_block=False, rendezvous_url='')
    enroll(cfg, identity(cfg['codex_home']))
    engine = Engine(Database(folder/'local.sqlite'), cfg)
    engine.start()
    try:
        deadline = time.time()+100
        while time.time() < deadline:
            view = engine.snapshot()
            if view.get('summary') and view['summary'].get('epoch'):
                assert not engine.blocked
                print('LIVE_CODEX_READ_OK', dict(mode=view['identity']['mode'],
                       weekly_used=view['summary']['epoch']['used'],
                       device_count=len(view['summary']['devices']),
                       active_sessions=view['active'], auto_block=False))
                return
            time.sleep(1)
        raise AssertionError(engine.snapshot().get('error') or 'live initialization timeout')
    finally:
        engine.close()


if __name__ == '__main__':
    main()
