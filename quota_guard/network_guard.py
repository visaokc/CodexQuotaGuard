"""Compare unauthenticated domain egress probes with an administrator's baseline."""
import copy
import ipaddress
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


HOSTS = ('chatgpt.com', 'api.openai.com')


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


def probe(host):
    if host not in HOSTS:
        raise ValueError('未知检测地址')
    # Respect current environment/system proxies; never alter routing or use account credentials.
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request('https://'+host+'/cdn-cgi/trace', headers={
        'User-Agent': 'CodexQuotaGuard-NetworkCheck', 'Cache-Control': 'no-cache'})
    with opener.open(request, timeout=8) as response:
        body = response.read(4097)
        if response.status != 200 or len(body) > 4096:
            raise ValueError('出口检测响应无效')
    fields = dict(line.split('=', 1) for line in body.decode('ascii').splitlines() if '=' in line)
    if fields.get('h') != host:
        raise ValueError('出口检测响应无效')
    return public_ip(fields.get('ip'))


class NetworkGuard:
    def __init__(self, reader=probe):
        self.reader = reader
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
                self.wakeup.set()

    def close(self):
        self.closed.set()
        self.wakeup.set()

    def _read(self, host):
        try:
            return dict(host=host, ip=public_ip(self.reader(host)), error=None)
        except urllib.error.HTTPError as exc:
            error = '检测服务返回 HTTP '+str(exc.code)
        except Exception:
            error = '无法确认该域名的出口IP，请检查网络后重试'
        return dict(host=host, ip=None, error=error)

    def check(self):
        with self.lock:
            if self.checking:
                return
            self.checking = True
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                observations = list(executor.map(self._read, HOSTS))
            with self.lock:
                self.observations = observations
                self.checked_at = time.time()
        finally:
            with self.lock:
                self.checking = False

    def _run(self):
        while not self.closed.is_set():
            self.wakeup.clear()
            self.check()
            self.wakeup.wait(30)

    def snapshot(self, baseline=None, now=None):
        now = time.time() if now is None else now
        with self.lock:
            rows, at, checking = copy.deepcopy(self.observations), self.checked_at, self.checking
        ips = list(dict.fromkeys(row['ip'] for row in rows if row['ip']))
        error = ('检测结果已过期，请重试' if rows and now-at > 90 else
                 next((row['error'] for row in rows if row['error']), None))
        mismatch = now-at <= 90 and any(ip != baseline for ip in ips)
        state = ('unconfigured' if not baseline else 'mismatch' if mismatch else 'error' if error else
                 'checking' if not rows else 'aligned' if all(row['ip'] == baseline for row in rows) else 'mismatch')
        return dict(baseline=baseline, state=state, current_ip=' / '.join(ips) or None,
                    observations=rows, checked_at=at, checking=checking, error=error)

    def baseline_candidate(self):
        value = self.snapshot()
        rows = value['observations']
        if (value['checking'] or time.time()-value['checked_at'] > 60 or len(rows) != len(HOSTS)
                or any(row['error'] or not row['ip'] for row in rows)
                or len({row['ip'] for row in rows}) != 1):
            raise ValueError('请先重试检测，确认两个域名的出口IP一致后再设置基准')
        return rows[0]['ip']
