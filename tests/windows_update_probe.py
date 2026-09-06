"""Signed helper integration on disposable EXEs only; no installed app is touched."""
import base64
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from quota_guard.pairing import dpapi
from quota_guard import updater as u
from quota_guard.storage import atomic_json


def scenario(root, rollback, frozen=False):
    folder = root/('frozen' if frozen else 'rollback' if rollback else 'success')
    updates = folder/'updates'
    updates.mkdir(parents=True)
    old, new = folder/'app.exe', updates/'new.exe'
    source = folder/'probe.cs'
    # Original instance exits naturally; restarted instances record launch and exit.
    old_source = '''using System; using System.IO; using System.Threading;
    class App { static void Main(string[] a) {
      if(a.Length==0) { Thread.Sleep(10000); return; }
      File.WriteAllText(Path.Combine(a[1],"old-restarted"),"ok"); } }'''
    new_source = '''using System; using System.IO; class App { static void Main(string[] a) {
      File.WriteAllText(Path.Combine(a[1],"new-started"),"ok");
      for(int i=0;i<a.Length;i++) if(a[i]=="--update-health") {
       File.WriteAllText(a[i+1],"{\\"nonce\\":\\""+a[i+3]+"\\",\\"version\\":\\"0.3.0\\"}");
      } System.Threading.Thread.Sleep(1500); } }'''
    if rollback:
        new_source = 'class App { static void Main(string[] a) { System.Environment.Exit(12); } }'
    compiler = r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    for code, dest in [(old_source, old), (new_source, new)]:
        source.write_text(code)
        subprocess.run([compiler, '/nologo', '/target:winexe', '/out:'+str(dest), str(source)], check=True)
    if frozen:
        shutil.copyfile(Path('dist/0.2.2/Codex配额管家.exe'), old)
    original = old.read_bytes()
    expected = new.read_bytes()
    keypath = Path('D:/Codex/.private/update-signing-key.dpapi')
    key = Ed25519PrivateKey.from_private_bytes(dpapi(keypath.read_bytes(), decrypt=True))
    payload = dict(schema=1, version='0.3.0', size=len(expected), sha256=hashlib.sha256(expected).hexdigest(),
        url=f'https://github.com/{u.REPOSITORY}/releases/download/v0.3.0/CodexQuotaGuard-0.3.0.exe')
    manifest = dict(payload=payload, signature=base64.b64encode(key.sign(u.canonical(payload))).decode())
    proc = subprocess.Popen([str(old)]+(['--data-dir', str(folder), '--smoke-seconds', '25'] if frozen else []), creationflags=0x08000000)
    parent_pid = proc.pid
    if frozen:
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            result = subprocess.run(['powershell', '-NoProfile', '-Command',
                f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={proc.pid}' | Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, creationflags=0x08000000)
            children = result.stdout.split()
            if children:
                parent_pid = int(children[0])
                break
            time.sleep(.2)
        assert parent_pid != proc.pid
    path = updates/'job.json'
    atomic_json(path, dict(parent=parent_pid, target=str(old), staged=str(new), data_dir=str(folder),
        background=False, current='0.2.2', manifest=manifest, nonce=secrets.token_hex(24)))
    helper = subprocess.Popen([str(Path('work/updater/CodexQuotaUpdater.exe').resolve()), '--apply', str(path)], creationflags=0x08000000)
    try:
        assert helper.wait(timeout=100) == (1 if rollback else 0)
        proc.wait(timeout=5)
        result = json.loads((updates/'result.json').read_text(encoding='utf-8'))
        assert result['ok'] is (not rollback)
        assert old.read_bytes() == (original if rollback else expected)
        deadline = time.monotonic()+10
        marker = folder/('old-restarted' if rollback else 'new-started')
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(.1)
        assert marker.exists()
        print('REAL_SIGNED_UPDATE_'+('FROZEN_ONEFILE' if frozen else 'ROLLBACK' if rollback else 'REPLACE_RESTART')+'_OK', flush=True)
    finally:
        for child in (proc, helper):
            if child.poll() is None:
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True, creationflags=0x08000000)
                child.wait(timeout=10)


if __name__ == '__main__':
    root = (Path('work')/('updater-probe-'+secrets.token_hex(4))).resolve()
    scenario(root, False)
    scenario(root, True)
    scenario(root, False, frozen=True)
