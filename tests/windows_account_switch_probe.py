"""Real Windows restrictions, only on an isolated disposable process and test rules."""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quota_guard.firewall as firewall
from quota_guard.accounts import enroll
from quota_guard.engine import Engine
from quota_guard.quota import identity
from quota_guard.storage import Database, defaults


def main():
    folder = Path('work/windows-switch-'+uuid.uuid4().hex[:8]).resolve()
    folder.mkdir(parents=True)
    source, exe, pulse = folder/'probe.cs', folder/'cqg-account-probe.exe', folder/'pulse.txt'
    source.write_text('''using System; using System.IO; using System.Threading;
    class Probe { static void Main(string[] args) { while(true) {
      File.AppendAllText(args[0], "x"); Thread.Sleep(100); } } }''')
    subprocess.run([r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe', '/nologo', '/out:'+str(exe), str(source)], check=True)
    home = folder/'home'
    home.mkdir()
    (home/'auth.json').write_text(json.dumps(dict(tokens=dict(account_id='test-account', access_token='fixture'))))
    config = defaults()
    config.update(codex_home=str(home), device_id='one', auto_block=True, program_paths=[str(exe)], started_at=0)
    ident = identity(home)
    enroll(config, ident, 0)
    firewall.GROUP = 'CodexQuotaGuard-Test-'+uuid.uuid4().hex[:8]
    fw = firewall.Firewall()
    def quota(_):
        return dict(account=ident['account'], at=time.time(), used=80, reset_at=time.time()+604800)
    e = Engine(Database(folder/'local.sqlite'), config, quota_reader=quota, firewall=fw)
    proc = subprocess.Popen([str(exe), str(pulse)], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        time.sleep(.5)
        e.step()
        now = time.time()
        summary = dict(account=ident['account'], epoch=dict(cycle='test-cycle', observed_at=now), reset_pending=False,
                       devices=[dict(id='one', estimated=34, settled=34, cap=33)])
        e.enforce(summary, now)
        assert e.blocked and fw.status() == [str(exe)]
        time.sleep(.2)
        size = pulse.stat().st_size
        time.sleep(.5)
        assert pulse.stat().st_size == size, 'Disposable process did not pause'
        # Real file-based identity reader, real Engine.step, no manual restore call.
        (home/'config.toml').write_text('model_provider="relay"')
        e.step()
        time.sleep(.5)
        assert not e.blocked and not e.db.get('block_state')
        assert not e.db.get('paused_processes') and not fw.status()
        assert pulse.stat().st_size > size, 'Account -> API did not resume the process'
        assert e.snapshot()['identity']['mode'] == 'api' and e.snapshot()['summary'] is None
        assert e.config['auto_block']
        print('REAL_WINDOWS_ACCOUNT_TO_API_AUTO_RESUME_AND_RULE_REMOVAL_OK')
    finally:
        try:
            fw.resume(e.db)
            fw.restore()
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == '__main__':
    main()
