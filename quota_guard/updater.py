"""Signed, bounded GitHub updates. No account credentials enter this transport."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .storage import atomic_json

REPOSITORY = 'visaokc/CodexQuotaGuard'
MANIFEST_URL = f'https://github.com/{REPOSITORY}/releases/latest/download/update.json'
PUBLIC_KEY = 'QovxN8MJSTuLJm+tbdH7MuaOK87b9Q1aFCpLBDqs5II='
MAX_SIZE = 300 * 1024 * 1024


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('仅接受正式版版本号')
    return tuple(map(int, value.split('.')))


def verify(manifest, current=None):
    payload = manifest['payload']
    Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY, validate=True)).verify(
        base64.b64decode(manifest['signature'], validate=True), canonical(payload))
    v = payload['version']
    version(v)
    if payload['schema'] != 1 or payload['url'] != f'https://github.com/{REPOSITORY}/releases/download/v{v}/CodexQuotaGuard-{v}.exe':
        raise ValueError('更新来源或格式不匹配')
    if type(payload['size']) is not int or not 0 < payload['size'] <= MAX_SIZE:
        raise ValueError('更新包大小无效')
    if not re.fullmatch('[a-f0-9]{64}', payload['sha256']):
        raise ValueError('更新包摘要无效')
    if current is not None and version(v) <= version(current):
        return None
    return payload


def safe_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or p.hostname not in (
            'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
            'github-releases.githubusercontent.com'):
        raise ValueError('更新重定向不是 GitHub HTTPS 下载地址')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, safe_url(newurl))


def open_url(url):
    from .autolink import system_proxies
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(system_proxies()), Redirect())
    return opener.open(urllib.request.Request(safe_url(url), headers={
        'User-Agent': 'CodexQuotaGuard-Updater', 'Cache-Control': 'no-cache'}), timeout=30)


def check(current):
    with open_url(MANIFEST_URL) as response:
        raw = response.read(16385)
    if len(raw) > 16384:
        raise ValueError('更新清单过大')
    manifest = json.loads(raw)
    return manifest if verify(manifest, current) else None


def file_valid(path, payload):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size != payload['size']:
        return False
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest() == payload['sha256']


def download(manifest, folder):
    payload = verify(manifest)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/('CodexQuotaGuard-'+payload['version']+'.exe')
    if file_valid(target, payload):
        return target
    part = target.with_suffix('.part')
    try:
        with open_url(payload['url']) as response, part.open('wb') as out:
            size = 0
            while chunk := response.read(1024*1024):
                size += len(chunk)
                if size > payload['size']:
                    raise ValueError('更新下载超出签名清单声明的大小')
                out.write(chunk)
        if not file_valid(part, payload):
            raise ValueError('更新包校验失败，未安装')
        os.replace(part, target)
        return target
    finally:
        part.unlink(missing_ok=True)


def launch_helper(manifest, staged, folder, background):
    if not getattr(sys, 'frozen', False):
        raise ValueError('源码运行不支持原地更新，请使用发布版 EXE')
    folder = Path(folder).resolve()
    update_dir = folder/'updates'
    update_dir.mkdir(parents=True, exist_ok=True)
    helper = update_dir/'CodexQuotaUpdater.exe'
    shutil.copy2(Path(sys._MEIPASS)/'vendor'/'CodexQuotaUpdater.exe', helper)
    job = dict(parent=os.getpid(), target=str(Path(sys.executable).resolve()), staged=str(Path(staged).resolve()),
        data_dir=str(folder), background=bool(background), manifest=manifest,
        current=__import__('quota_guard').__version__, nonce=os.urandom(24).hex())
    path = update_dir/'job.json'
    atomic_json(path, job)
    process = subprocess.Popen([str(helper), '--apply', str(path)], creationflags=0x08000000)
    # The helper must validate and open the exact parent process before it exits.
    ready = update_dir/(job['nonce']+'.ready')
    deadline = time.monotonic()+20
    while time.monotonic() < deadline:
        if ready.exists():
            ready.unlink()
            return
        if process.poll() is not None:
            break
        time.sleep(.1)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    raise RuntimeError('更新助手未就绪，当前版本保持运行')


def parent_handle(pid, target):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    size = wintypes.DWORD(32768)
    text = ctypes.create_unicode_buffer(size.value)
    if not kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)) or Path(text.value).resolve() != target:
        kernel.CloseHandle(handle)
        raise ValueError('更新目标与父进程不一致')
    return kernel, handle


def replace_when_unlocked(source, target):
    # A onefile bootloader can outlive its child briefly while cleaning its temp directory.
    deadline = time.monotonic()+20
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.25)


def apply_job(path):
    path = Path(path).resolve()
    job = json.loads(path.read_text(encoding='utf-8'))
    payload = verify(job['manifest'], job['current'])
    if payload is None:
        raise ValueError('拒绝降级或重复安装')
    target, staged = Path(job['target']).resolve(), Path(job['staged']).resolve()
    folder = Path(job['data_dir']).resolve()
    if path.parent != folder/'updates' or staged.parent != path.parent or target.suffix.lower() != '.exe' or target == staged:
        raise ValueError('更新路径不匹配')
    if not re.fullmatch('[a-f0-9]{48}', job['nonce']) or not file_valid(staged, payload):
        raise ValueError('更新包或事务标识校验失败')
    backup = target.with_name(target.name+'.update-backup')
    replacement = target.with_name(target.name+'.update-new')
    health = path.parent/(job['nonce']+'.health')
    ready = path.parent/(job['nonce']+'.ready')
    kernel, handle = parent_handle(job['parent'], target)
    replaced = False
    parent_exited = False
    child = None
    args = [str(target), '--data-dir', str(folder)] + (['--background'] if job['background'] else [])
    try:
        # Test directory write access and verify the same-volume replacement before stopping the app.
        shutil.copyfile(staged, replacement)
        if not file_valid(replacement, payload):
            raise ValueError('落盘校验失败')
        ready.write_text('ready')
        if kernel.WaitForSingleObject(handle, 90000) != 0:
            raise RuntimeError('等待原程序退出超时，未更新')
        parent_exited = True
        shutil.copyfile(target, backup)
        replace_when_unlocked(replacement, target)
        replaced = True
        child = subprocess.Popen(args+['--update-health', str(health), '--update-nonce', job['nonce']], creationflags=0x08000000)
        deadline = time.monotonic()+60
        while time.monotonic() < deadline:
            if health.exists():
                result = json.loads(health.read_text(encoding='utf-8'))
                if result == dict(nonce=job['nonce'], version=payload['version']):
                    atomic_json(path.parent/'result.json', dict(ok=True, version=payload['version']))
                    backup.unlink(missing_ok=True)
                    return
            if child.poll() is not None:
                break
            time.sleep(.3)
        raise RuntimeError('新版启动检查未通过，已回滚旧版')
    except Exception as e:
        if replaced:
            if child and child.poll() is None:
                # Only the new instance started by this update transaction.
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True, creationflags=0x08000000)
                child.wait(timeout=10)
            replace_when_unlocked(backup, target)
        if parent_exited:
            subprocess.Popen(args, creationflags=0x08000000)
        atomic_json(path.parent/'result.json', dict(ok=False, version=payload['version'], error=str(e)))
        raise
    finally:
        kernel.CloseHandle(handle)
        replacement.unlink(missing_ok=True)
        health.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)
