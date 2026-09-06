"""Exercise real pause/resume and scoped rules on our OWN disposable C# process."""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quota_guard.firewall as firewall
from quota_guard.storage import Database


def main():
    folder = Path('work/windows-safety').resolve()
    folder.mkdir(parents=True, exist_ok=True)
    source, exe, pulse = folder/'probe.cs', folder/'cqg-pause-probe.exe', folder/'pulse.txt'
    source.write_text('''using System; using System.IO; using System.Threading;
    class Probe { static void Main(string[] args) { while(true) {
      File.AppendAllText(args[0], "x"); Thread.Sleep(100); } } }''')
    subprocess.run([r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe', '/nologo', '/out:'+str(exe), str(source)], check=True)
    db = Database(folder/'test.sqlite')
    fw = firewall.Firewall()
    firewall.GROUP = 'CodexQuotaGuard-Test-'+uuid.uuid4().hex[:8]
    proc = subprocess.Popen([str(exe), str(pulse)], creationflags=0x08000000)
    try:
        time.sleep(.5)
        fw.apply([str(exe)])
        assert any(str(exe).lower() == p.lower() for p in fw.status())
        fw.pause([str(exe)], db)
        time.sleep(.2)
        before = pulse.stat().st_size
        fw.pause([str(exe)], db)  # Repeated enforcement must not add a second suspend count.
        time.sleep(.5)
        assert pulse.stat().st_size == before
        fw.resume(db)
        time.sleep(.5)
        assert pulse.stat().st_size > before
        fw.restore()
        assert not fw.status()
        assert not db.get('paused_processes')
        print('WINDOWS_PAUSE_RESUME_AND_SCOPED_FIREWALL_OK')
    finally:
        try:
            fw.resume(db)
            fw.restore()
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == '__main__':
    main()
