"""Lifecycle and capability-protected IPC for the bundled userspace tsnet node."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import urllib.request
from urllib.parse import urlsplit

from .child_job import ChildJob


def tail_ip(value):
    if not isinstance(value, str):
        return False
    try:
        ip = ipaddress.ip_address(value)
        return ip in ipaddress.ip_network('100.64.0.0/10') if ip.version == 4 else ip in ipaddress.ip_network('fd7a:115c:a1e0::/48')
    except (ValueError, TypeError):
        return False


def auth_url(value):
    try:
        p = urlsplit(value)
        return p.scheme == 'https' and p.hostname == 'login.tailscale.com' and not p.username and not p.password and p.port in (None, 443)
    except ValueError:
        return False


def route_label(probe):
    if not isinstance(probe, dict) or probe.get('Err'):
        return 'Tailscale · 路径待确认'
    if probe.get('PeerRelay'):
        return 'Tailscale · 自建 Peer Relay'
    if probe.get('Endpoint'):
        return 'Tailscale · 直连'
    if probe.get('DERPRegionID'):
        return 'Tailscale · DERP 中转 ('+str(probe.get('DERPRegionCode') or probe['DERPRegionID'])+')'
    return 'Tailscale · 路径待确认'


class EmbeddedNode:
    def __init__(self, folder, device):
        self.folder, self.device = Path(folder), device
        self.process = self.job = self.api = None
        self.token = secrets.token_urlsafe(32)

    def start(self):
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        binary = root/'vendor'/'tsnet'/'cqg-tsnet.exe'
        manifest = root/'vendor'/'tsnet'/'manifest.json'
        if not binary.is_file() or not manifest.is_file():
            raise RuntimeError('安装包缺少内嵌 Tailscale 组件，请使用完整新版安装包')
        if hashlib.sha256(binary.read_bytes()).hexdigest() != json.loads(manifest.read_text())['sha256']:
            raise RuntimeError('内嵌 Tailscale 组件校验失败')
        state = self.folder/'tailscale'
        state.mkdir(parents=True, exist_ok=True)
        # Do not inherit unrelated machine-wide auth keys, control servers or exit-node configuration.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('TS_', 'TSNET_'))}
        env['TS_NO_LOGS_NO_SUPPORT'] = 'true'
        self.process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, creationflags=0x08000004)
        try:
            self.job = ChildJob(self.process)
            self.process.stdin.write((json.dumps(dict(Dir=str(state.resolve()),
                Hostname='cqg-'+hashlib.sha256(self.device.encode()).hexdigest()[:12], Token=self.token))+'\n').encode())
            self.process.stdin.flush()
            result = queue.Queue(maxsize=1)
            def read():
                result.put(self.process.stdout.readline(4096))
            threading.Thread(target=read, daemon=True).start()
            raw = result.get(timeout=30)
            self.api = json.loads(raw)['api']
            p = urlsplit(self.api)
            if p.scheme != 'http' or p.hostname != '127.0.0.1' or not p.port or p.username or p.path:
                raise RuntimeError('内嵌连接接口无效')
        except Exception:
            self.close()
            raise

    def request(self, method, data=None):
        req = urllib.request.Request(self.api+'/'+method, data=json.dumps(data or {}).encode(),
            headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=7) as response:
            return json.loads(response.read(9*1024*1024))

    def close(self):
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
            if self.process.stdout:
                self.process.stdout.close()
        if self.job:
            self.job.close()
            self.job = None
