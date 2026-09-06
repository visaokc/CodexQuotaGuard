"""Private, application-owned Syncthing instance for zero-configuration pairing.

Only the dedicated ciphertext directory is shared. Never share Codex's data root.
Public discovery/relays use Syncthing with a documented proxy compatibility patch.
"""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .child_job import ChildJob


DEVICE = re.compile(r'^[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}$')
BINARY_SHA256 = '380963659e52201f14accf46bd400487e88d7a1e8e21f209e3c841f0464e0da5'


def relay_address(value):
    from urllib.parse import parse_qs
    p = urlsplit(value)
    return (p.scheme == 'relay' and p.hostname and p.port and not p.username and not p.password
            and not p.fragment and DEVICE.fullmatch(parse_qs(p.query).get('id', [''])[0]))


def binary():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    path = root/'vendor'/'syncthing'/'syncthing.exe'
    if not path.is_file():
        raise RuntimeError('安装包缺少自动连接组件，请使用完整版本。')
    if hashlib.sha256(path.read_bytes()).hexdigest() != BINARY_SHA256:
        raise RuntimeError('自动连接组件校验失败，请重新解压安装包。')
    return path


def proof(secret, device):
    return 'CQG3-'+hmac.new(secret.encode(), ('join-device:'+device).encode(), hashlib.sha256).hexdigest()


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def system_proxies():
    # Python's getproxies() drops registry settings when *only* NO_PROXY exists.
    registry = getattr(urllib.request, 'getproxies_registry', lambda: {})()
    return dict(registry, **urllib.request.getproxies())


def mixed_proxy(proxy):
    """Use an existing mixed HTTP/SOCKS listener without guessing other ports."""
    parsed = urlsplit(proxy)
    if parsed.scheme != 'http' or parsed.username or not parsed.hostname:
        return proxy
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=2) as sock:
            sock.sendall(b'\x05\x01\x00')
            if sock.recv(2) == b'\x05\x00':
                return urlunsplit(('socks5', parsed.netloc, '', '', ''))
    except OSError:
        pass
    return proxy


def prepare(folder):
    """Generate stable local device credentials offline. No login or server form."""
    home = Path(folder)/'autolink'/'node'
    home.mkdir(parents=True, exist_ok=True)
    exe = binary()
    if not (home/'config.xml').exists():
        password = secrets.token_urlsafe(32)
        subprocess.run([str(exe), 'generate', '--home', str(home), '--no-port-probing',
                        '--gui-user', 'cqg', '--gui-password', '-'], input=password.encode(),
                       capture_output=True, timeout=30, check=True, creationflags=0x08000000)
    result = subprocess.run([str(exe), 'device-id', '--home', str(home)], capture_output=True,
                            timeout=15, check=True, creationflags=0x08000000)
    own = result.stdout.decode().strip()
    if not DEVICE.fullmatch(own):
        raise RuntimeError('自动连接组件未返回有效设备身份。')
    return own


class LinkNode:
    def __init__(self, folder, config, local_test=False):
        self.folder, self.config = Path(folder).resolve(), dict(config)
        self.home = self.folder/'autolink'/'node'
        self.device = prepare(folder)
        self.secret = config['group_secret']
        self.group = 'cqg3-'+hashlib.sha256(self.secret.encode()).hexdigest()
        self.shared = self.folder/'autolink'/'ciphertext'/self.group
        self.shared.mkdir(parents=True, exist_ok=True)
        self.local_test = local_test
        self.process = None
        self.job = None
        self.api = None
        self.api_key = None
        self.connections = {}
        self.last_poll = 0

    def request(self, route, data=None, method=None):
        raw = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.api+'/rest/'+route, data=raw, method=method,
            headers={'X-API-Key': self.api_key, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as response:
            body = response.read(4*1024*1024)
            return json.loads(body) if body else None

    def start(self):
        tree = ET.parse(self.home/'config.xml')
        root = tree.getroot()
        marker = self.home/'group.txt'
        previous = marker.read_text() if marker.exists() else ''
        for item in list(root.findall('folder')):
            root.remove(item)
        for item in list(root.findall('device')):
            if previous != self.group and item.attrib['id'] != self.device:
                root.remove(item)
        def put(parent, name, value):
            child = parent.find(name)
            if child is None:
                child = ET.SubElement(parent, name)
            child.text = str(value)
        existing = {d.attrib['id'] for d in root.findall('device')}
        for peer in self.config.get('link_peers', []):
            if not DEVICE.fullmatch(peer):
                raise ValueError('匹配码设备身份无效')
            if peer not in existing and peer != self.device:
                item = ET.SubElement(root, 'device', id=peer, name='CQG peer', introducer='true', skipIntroductionRemovals='true')
                put(item, 'address', 'dynamic')
                for address in self.config.get('link_addresses', {}).get(peer, []):
                    if relay_address(address):
                        ET.SubElement(item, 'address').text = address
                put(item, 'autoAcceptFolders', 'false')
        own = next(d for d in root.findall('device') if d.attrib['id'] == self.device)
        own.set('name', proof(self.secret, self.device))
        gui = root.find('gui')
        self.api_key = gui.findtext('apikey')
        self.api = 'http://127.0.0.1:'+str(free_port())
        put(gui, 'address', self.api.removeprefix('http://'))
        options = root.find('options')
        for item in list(options.findall('listenAddress')):
            options.remove(item)
        if self.local_test or not self.config.get('force_relay'):
            ET.SubElement(options, 'listenAddress').text = 'tcp://127.0.0.1:0' if self.local_test else 'tcp://0.0.0.0:0'
        if not self.local_test:
            ET.SubElement(options, 'listenAddress').text = 'dynamic+https://relays.syncthing.net/endpoint'
        for key, value in dict(globalAnnounceEnabled=not self.local_test, localAnnounceEnabled=False,
            relaysEnabled=not self.local_test, natEnabled=False, startBrowser=False,
            crashReportingEnabled=False, urAccepted=-1, autoUpgradeIntervalH=0,
            reconnectionIntervalS=5, relayReconnectIntervalM=1, announceLANAddresses=False).items():
            put(options, key, str(value).lower())
        template = copy.deepcopy(root.find('defaults/folder'))
        template.attrib.update(id=self.group, label='CodexQuotaGuard ciphertext', path=str(self.shared),
                              fsWatcherDelayS='1', rescanIntervalS='300', ignorePerms='true')
        for d in list(template.findall('device')):
            template.remove(d)
        for d in root.findall('device'):
            ET.SubElement(template, 'device', id=d.attrib['id'])
        put(template, 'maxConflicts', 0)
        root.insert(0, template)
        tree.write(self.home/'config.xml', encoding='utf-8', xml_declaration=True)
        marker.write_text(self.group)
        env = dict(os.environ, STNOUPGRADE='1', STNORESTART='1', GOMAXPROCS='2')
        if not self.local_test and not (env.get('ALL_PROXY') or env.get('all_proxy')):
            proxies = system_proxies()
            proxy = proxies.get('https') or proxies.get('http')
            if proxy:
                env['ALL_PROXY'] = mixed_proxy(proxy)
                env['ALL_PROXY_NO_FALLBACK'] = '1'
                env.setdefault('HTTPS_PROXY', proxy)
                env.setdefault('HTTP_PROXY', proxy)
        self.process = subprocess.Popen([str(binary()), 'serve', '--home', str(self.home), '--no-browser',
            '--no-console', '--no-upgrade', '--no-restart', '--log-level', 'WARN',
            '--log-file', str(self.home/'runtime.log'), '--log-max-size', '2097152', '--log-max-old-files', '1'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, creationflags=0x08000004)
        try:
            self.job = ChildJob(self.process)
        except Exception:
            self.process.terminate()
            self.process.wait(timeout=5)
            raise
        deadline = time.monotonic()+25
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('自动连接组件启动失败，可能已有同一数据目录的实例。')
            try:
                if self.request('system/status')['myID'] == self.device:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.2)
        self.close()
        raise RuntimeError('自动连接组件启动超时，可点击重试；无需填写网络参数。')

    def poll(self):
        pending = self.request('cluster/pending/devices')
        devices = self.request('config/devices')
        ids = {d['deviceID'] for d in devices}
        for peer, value in pending.items():
            if peer in ids or len(ids) >= 16 or not DEVICE.fullmatch(peer):
                continue
            if not hmac.compare_digest(str(value.get('name', '')), proof(self.secret, peer)):
                continue
            self.request('config/devices', dict(deviceID=peer, name='CQG peer', addresses=['dynamic'],
                introducer=False, skipIntroductionRemovals=True, autoAcceptFolders=False), 'POST')
            folder = self.request('config/folders/'+self.group)
            folder['devices'].append(dict(deviceID=peer))
            self.request('config/folders/'+self.group, folder, 'PUT')
            ids.add(peer)
        self.connections = self.request('system/connections').get('connections', {})
        return self.connections

    def invitation_addresses(self):
        status = self.request('system/status')
        return list(dict.fromkeys(address for value in status.get('connectionServiceStatus', {}).values()
            for address in value.get('wanAddresses', []) if relay_address(address)))

    def close(self):
        try:
            if self.process and self.process.poll() is None:
                try:
                    self.request('system/shutdown', {}, 'POST')
                    self.process.wait(timeout=8)
                except Exception:
                    subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                                   capture_output=True, creationflags=0x08000000)
                    self.process.wait(timeout=8)
        finally:
            if self.job:
                self.job.close()
                self.job = None
