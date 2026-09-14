"""Compare the unauthenticated Ping0 egress observation with the shared baseline."""
import copy
import hmac
import secrets
import ctypes
import ipaddress
import threading
import time
import urllib.error
import urllib.request


CHECK_INTERVAL = 10


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
    return dict(ip=public_ip(lines[0].strip()))


class NetworkGuard:
    def __init__(self, reader=probe, on_result=None, process_reader=codex_running):
        self.reader, self.on_result = reader, on_result
        self.process_reader, self.codex_running = process_reader, False
        self._key = secrets.token_bytes(32)
        self._current_digest = self._active_digest = None
        self.changed = False
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

    def _digest(self, ip):
        return hmac.digest(self._key, ip.encode(), 'sha256')

    def _read(self):
        try:
            # The address exists only during this comparison. Never retain the response,
            # an address-keyed cache, geolocation, or a remote purity lookup.
            return self._digest(public_ip(self.reader()['ip'])), None
        except urllib.error.HTTPError as exc:
            return None, 'Ping0 返回 HTTP '+str(exc.code)
        except Exception:
            return None, '无法读取 Ping0 出口，请检查网络后重试'

    def check(self, force=False):
        with self.lock:
            if self.checking:
                return
            self.checking = True
        try:
            digest, error = self._read()
            try:
                running = bool(self.process_reader())
            except (OSError, AttributeError):
                running = False
            with self.lock:
                self.changed = bool(running and digest and self._active_digest
                                    and self._active_digest != digest)
                if not running:
                    self._active_digest = None
                elif digest:
                    self._active_digest = digest
                self._current_digest = digest
                self.observations = [dict(host='ping0.cc', success=digest is not None, error=error)]
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
            running, digest, changed = self.codex_running, self._current_digest, self.changed
        error = ('检测结果已过期，请重试' if rows and now-at > 30 else
                 next((row['error'] for row in rows if row['error']), None))
        verified = bool(digest and not error and now-at <= 30)
        mismatch = (not hmac.compare_digest(digest, self._digest(baseline))) if verified and baseline else None
        state = ('unconfigured' if not baseline else 'error' if error else
                 'checking' if not rows else 'mismatch' if mismatch else 'aligned' if verified else 'error')
        return dict(baseline=baseline, state=state, observations=rows, checked_at=at,
                    checking=checking, error=error, check_interval=CHECK_INTERVAL,
                    codex_running=running, verified=verified, mismatch=mismatch, changed=changed)

    def baseline_candidate(self):
        value = self.snapshot()
        if value['checking'] or not value['verified']:
            raise ValueError('请先重试检测，再设置住宅IP基准')
        try:
            # An explicitly authorized baseline update probes again; its address is
            # returned only to the administrator policy command, never to the UI.
            return public_ip(self.reader()['ip'])
        except Exception:
            raise ValueError('无法确认住宅IP基准，请重试') from None
