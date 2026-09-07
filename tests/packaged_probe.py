"""Smoke packaged clients and the packaged relay without restricting Codex."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quota_guard.pairing import load_config, save_config
from quota_guard.storage import defaults
from quota_guard.accounts import enroll
from quota_guard import __version__
from quota_guard.quota import identity


def client(mode):
    folder = ROOT/'work'/('packaged-'+mode+'-'+str(time.time_ns()))
    if mode == 'live':
        folder.mkdir(parents=True)
        cfg = defaults()
        enroll(cfg, identity(cfg['codex_home']))
        save_config(folder/'settings.json', cfg)
    command = [str(ROOT/'dist'/__version__/'Codex配额管家.exe'), '--data-dir', str(folder), '--smoke-seconds', '15']
    if mode == 'demo':
        command += ['--demo']
    if mode == 'untracked':
        command += ['--background']
    process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert process.wait(timeout=90) == 0
    finally:
        if process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
    config = load_config(folder/'settings.json')
    assert config['auto_block'] is False and not config['rendezvous_url']
    if mode == 'live':
        paths = list(folder.glob('group-*.sqlite'))
        assert len(paths) == 1
        with sqlite3.connect(paths[0]) as db:
            row = db.execute('SELECT used FROM epochs ORDER BY id DESC LIMIT 1').fetchone()
            assert row is not None and 0 <= row[0] <= 100, 'No real weekly snapshot from packaged client'
    elif mode == 'untracked':
        for path in folder.glob('group-*.sqlite'):
            with sqlite3.connect(path) as db:
                assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0
    print('PACKAGED_CLIENT_'+mode.upper()+'_OK', flush=True)


async def relay_probe():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    token = secrets.token_urlsafe(32)
    proc = subprocess.Popen([str(ROOT/'dist'/__version__/'CodexQuotaRelay.exe'), '--port', str(port)],
                            env=dict(os.environ, CQG_RELAY_TOKEN=token),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        url = f'ws://127.0.0.1:{port}'
        for _ in range(150):
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                assert proc.poll() is None, 'Relay executable exited early'
                await asyncio.sleep(.2)
        headers = {'Authorization': 'Bearer '+token}
        async with connect(url, additional_headers=headers, proxy=None) as a, connect(url, additional_headers=headers, proxy=None) as b:
            await a.send(json.dumps(dict(room='a'*64, device='one')))
            assert json.loads(await a.recv())['peers'] == ['one']
            await b.send(json.dumps(dict(room='a'*64, device='two')))
            assert len(json.loads(await b.recv())['peers']) == 2
            assert len(json.loads(await a.recv())['peers']) == 2
            from quota_guard.pairing import Cipher
            ca, cb = Cipher('s'*32, 'account'), Cipher('s'*32, 'account')
            msg = ca.seal('one', 'two', 'app', {'probe': True})
            await a.send(json.dumps(msg))
            result = json.loads(await asyncio.wait_for(b.recv(), 5))
            assert cb.open(result, 'two') == {'probe': True}
        print('PACKAGED_RELAY_ENCRYPTED_TRANSFER_OK', flush=True)
    finally:
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
        proc.wait(timeout=15)


if __name__ == '__main__':
    client('demo')
    client('untracked')
    client('live')
    asyncio.run(relay_probe())
