"""Compare the unauthenticated Ping0 egress observation with the shared baseline."""
import copy
import ctypes
import ipaddress
import html
import re
import threading
import time
import urllib.error
import urllib.request


CHECK_INTERVAL = 10
RISK_CACHE_SECONDS = 600


def codex_running():
    """Process-name observation only; do not open or alter another process."""
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD),
                    ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
                    ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG),
                    ('flags', wintypes.DWORD), ('name', wintypes.WCHAR*260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        return False
    try:
        entry = Entry(size=ctypes.sizeof(Entry))
        found = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while found:
            if entry.name.lower() == 'codex.exe':
                return True
            found = kernel.Process32NextW(handle, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(handle)


def public_ip(value):
    if not isinstance(value, str) or '%' in value:
        raise ValueError('住宅IP基准须为公网IP地址')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError('住宅IP基准须为公网IP地址') from None
    address = getattr(address, 'ipv4_mapped', None) or address
    if not address.is_global or address.is_multicast:
        raise ValueError('住宅IP基准须为公网IP地址')
    return str(address)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('检测地址发生跳转，请重试')


def fetch(url, limit):
    # Respect current environment/system proxies; never alter routing or send account credentials.
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'CodexQuotaGuard-NetworkCheck',
        'Cache-Control': 'no-cache', 'Accept-Language': 'zh-CN'})
    with opener.open(request, timeout=8) as response:
        body = response.read(limit+1)
        if response.status != 200 or len(body) > limit:
            raise ValueError('Ping0 检测响应无效')
    return body.decode('utf-8')


def probe():
    # Ping0 documents /geo as IP, location, ASN, organization (four lines).
    lines = fetch('https://ping0.cc/geo', 4096).strip().splitlines()
    if len(lines) != 4 or not lines[1].strip() or len(lines[1]) > 240 or not lines[1].isprintable():
        raise ValueError('Ping0 位置信息无效')
    location = lines[1].strip()
    # Keep the full location if the service returns an unexpected language.
    country = location.split()[0] if re.search(r'[\u3400-\u9fff]', location) else location
    return dict(ip=public_ip(lines[0].strip()), location=location, country=country)


def parse_risk(page, ip):
    identity = re.search(r"window\.ip\s*=\s*['\"]([^'\"]+)['\"]", page)
    if not identity or public_ip(identity[1]) != ip:
        raise ValueError('Ping0 评分对应的IP不一致')
    current = re.search(r'<div\b[^>]*class=[\"\'][^\"\']*\briskcurrent\b[^\"\']*[\"\'][^>]*>(.*?)</div>', page, re.S)
    if not current:
        raise ValueError('Ping0 尚无纯度评分')
    def field(name):
        match = re.search(r'<span\b[^>]*class=[\"\']'+name+r'[\"\'][^>]*>(.*?)</span>', current[1], re.S)
        return html.unescape(re.sub('<[^>]*>', '', match[1])).strip() if match else ''
    value, purity = field('value'), field('lab')
    if not re.fullmatch(r'\d+(?:\.\d+)?%', value) or not purity or len(purity) > 40 or not purity.isprintable():
        raise ValueError('Ping0 纯度评分格式无效')
    score = float(value[:-1])
    if not 0 <= score <= 100:
        raise ValueError('Ping0 风控值无效')
    return dict(purity=purity, risk_score=score)


def probe_risk(ip):
    return parse_risk(fetch('https://ping0.cc/ip/'+public_ip(ip), 512*1024), ip)


class NetworkGuard:
    def __init__(self, reader=probe, risk_reader=probe_risk, on_result=None, process_reader=codex_running):
        self.reader, self.risk_reader, self.on_result = reader, risk_reader, on_result
        self.process_reader, self.codex_running = process_reader, False
        self.risk_cache = {}
        self.force_risk = False
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        self.observations = []
        self.checked_at = 0
        self.checking = False

    def start(self):
        with self.lock:
            if self.thread or self.closed.is_set():
                return
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def retry(self):
        self.start()
        with self.lock:
            if not self.checking:
                self.force_risk = True
                self.wakeup.set()

    def close(self):
        self.closed.set()
        self.wakeup.set()

    def _read(self, force):
        try:
            result = self.reader()
            ip = public_ip(result['ip'])
            row = dict(host='ping0.cc', ip=ip, error=None,
                       location=result.get('location'), country=result.get('country'))
        except urllib.error.HTTPError as exc:
            return dict(host='ping0.cc', ip=None, error='Ping0 返回 HTTP '+str(exc.code))
        except Exception:
            return dict(host='ping0.cc', ip=None, error='无法读取 Ping0 当前IP，请检查网络后重试')
        now = time.time()
        if force or self.risk_cache.get('ip') != ip or now-self.risk_cache.get('risk_at', 0) >= RISK_CACHE_SECONDS:
            try:
                risk = dict(self.risk_reader(ip), risk_error=None)
            except Exception:
                risk = dict(purity=None, risk_score=None, risk_error='Ping0 纯度暂不可用')
            self.risk_cache = dict(risk, ip=ip, risk_at=now)
        row.update({k: v for k, v in self.risk_cache.items() if k != 'ip'})
        return row

    def check(self, force=False):
        with self.lock:
            if self.checking:
                return
            self.checking = True
            force = force or self.force_risk
            self.force_risk = False
        try:
            observation = self._read(force)
            try:
                running = bool(self.process_reader())
            except (OSError, AttributeError):
                running = False
            with self.lock:
                self.observations = [observation]
                self.checked_at = time.time()
                self.codex_running = running
        finally:
            with self.lock:
                self.checking = False
        if self.on_result:
            self.on_result()

    def _run(self):
        while not self.closed.is_set():
            self.wakeup.clear()
            started = time.monotonic()
            self.check()
            self.wakeup.wait(max(1, CHECK_INTERVAL-(time.monotonic()-started)))

    def snapshot(self, baseline=None, now=None):
        now = time.time() if now is None else now
        with self.lock:
            rows, at, checking = copy.deepcopy(self.observations), self.checked_at, self.checking
            running = self.codex_running
        ips = list(dict.fromkeys(row['ip'] for row in rows if row['ip']))
        error = ('检测结果已过期，请重试' if rows and now-at > 30 else
                 next((row['error'] for row in rows if row['error']), None))
        mismatch = now-at <= 30 and any(ip != baseline for ip in ips)
        state = ('unconfigured' if not baseline else 'mismatch' if mismatch else 'error' if error else
                 'checking' if not rows else 'aligned' if all(row['ip'] == baseline for row in rows) else 'mismatch')
        return dict(baseline=baseline, state=state, current_ip=' / '.join(ips) or None,
                    observations=rows, checked_at=at, checking=checking, error=error,
                    check_interval=CHECK_INTERVAL, risk_cache_seconds=RISK_CACHE_SECONDS,
                    codex_running=running,
                    **{k: (rows[0].get(k) if rows else None) for k in
                       ('country','location','purity','risk_score','risk_error','risk_at')})

    def baseline_candidate(self):
        value = self.snapshot()
        rows = value['observations']
        if (value['checking'] or time.time()-value['checked_at'] > 30 or len(rows) != 1
                or any(row['error'] or not row['ip'] for row in rows)
                or rows[0]['host'] != 'ping0.cc'):
            raise ValueError('请先重试检测，取得 Ping0 当前IP后再设置基准')
        return rows[0]['ip']
