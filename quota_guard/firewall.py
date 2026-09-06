"""Program-scoped Windows Firewall rules. Never disables a network adapter."""
import base64
import ctypes
import hashlib
import json
import os
import subprocess
from pathlib import Path

GROUP = 'CodexQuotaGuard'


def is_admin():
    return os.name == 'nt' and bool(ctypes.windll.shell32.IsUserAnAdmin())


def powershell(script):
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            capture_output=True, timeout=35, creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError('Windows 防火墙操作失败；请确认管理员权限和防火墙服务状态')
    return result.stdout.decode('utf-8-sig', errors='replace').strip()


def quote(value):
    return "'"+str(value).replace("'", "''")+"'"


def apply_script(paths):
    commands = ["$ErrorActionPreference='Stop'"]
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute() or p.suffix.lower() != '.exe' or not p.is_file():
            raise ValueError('请选择存在的 EXE 绝对路径')
        name = GROUP+'-'+hashlib.sha256(str(p).lower().encode()).hexdigest()[:16]
        commands.append(f"if(-not (Get-NetFirewallRule -Name {quote(name)} -ErrorAction SilentlyContinue)) {{ "
                        f"New-NetFirewallRule -Name {quote(name)} -DisplayName {quote(name)} -Group '{GROUP}' "
                        f"-Direction Outbound -Action Block -Enabled True -Profile Any -Program {quote(p)} | Out-Null }}")
    return '; '.join(commands)


class Firewall:
    def apply(self, paths):
        if not paths:
            raise ValueError('尚未选择需断网的 Codex EXE')
        if not is_admin():
            raise RuntimeError('自动断网需要以管理员身份运行本工具')
        powershell(apply_script(paths))

    def restore(self):
        if not is_admin():
            raise RuntimeError('恢复防火墙规则需要管理员权限')
        powershell("$ErrorActionPreference='Stop'; $rules=@(Get-NetFirewallRule -ErrorAction Stop | "
                   f"Where-Object {{$_.Group -eq '{GROUP}'}}); "
                   "if($rules.Count -gt 0){$rules | Remove-NetFirewallRule -ErrorAction Stop}; exit 0")

    def status(self):
        value = powershell(f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; @(" 
            f"Get-NetFirewallRule -Group '{GROUP}' -ErrorAction SilentlyContinue | "
            "Get-NetFirewallApplicationFilter | Select-Object -ExpandProperty Program) | ConvertTo-Json -Compress")
        if not value:
            return []
        result = json.loads(value)
        return [result] if isinstance(result, str) else result

    def pause(self, paths, database):
        # Windows Firewall may not filter loopback traffic to a local proxy.
        # Suspend ONLY selected Codex processes so a local sing-box proxy cannot bypass
        # the limit. No proxy, shell, network adapter, or unrelated process is touched.
        if not is_admin():
            raise RuntimeError('代理兼容暂停需要管理员权限')
        selected = {str(Path(p).resolve()).lower() for p in paths}
        names = ','.join(quote(Path(p).name) for p in paths)
        raw = powershell("[Console]::OutputEncoding=[Text.Encoding]::UTF8; @(Get-CimInstance Win32_Process | "
                         f"Where-Object {{$_.Name -in @({names})}} | Select-Object ProcessId,ExecutablePath) | ConvertTo-Json -Compress")
        processes = json.loads(raw) if raw else []
        if isinstance(processes, dict):
            processes = [processes]
        paused = database.get('paused_processes', [])
        for process in processes:
            path = process.get('ExecutablePath') or ''
            if str(Path(path).resolve()).lower() not in selected or int(process['ProcessId']) == os.getpid():
                continue
            pid = int(process['ProcessId'])
            handle, created = open_process(pid)
            if not handle:
                continue
            try:
                if any(p['pid'] == pid and p['created'] == created for p in paused):
                    continue
                item = dict(pid=pid, created=created, path=path)
                # Persist before suspension so the recovery executable can always find it.
                paused.append(item)
                database.put('paused_processes', paused)
                if ctypes.windll.ntdll.NtSuspendProcess(ctypes.c_void_p(handle)) != 0:
                    paused.remove(item)
                    database.put('paused_processes', paused)
                    raise RuntimeError('无法暂停选定的 Codex 进程')
            finally:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))

    def resume(self, database):
        remaining = []
        for p in database.get('paused_processes', []):
            handle, created = open_process(p['pid'])
            if not handle:
                # Distinguish an exited process from denied access.
                if ctypes.get_last_error() == 5:
                    remaining.append(p)
                continue
            try:
                if created == p['created'] and ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(handle)) != 0:
                    remaining.append(p)
            finally:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        database.put('paused_processes', remaining)
        if remaining:
            raise RuntimeError('部分 Codex 进程未能恢复，请以管理员身份执行恢复工具')


def open_process(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x0800 | 0x1000, False, pid)
    if not handle:
        return None, None
    values = [ctypes.c_uint64() for _ in range(4)]
    if not kernel.GetProcessTimes(ctypes.c_void_p(handle), *[ctypes.byref(v) for v in values]):
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return None, None
    return handle, values[0].value


def discover_programs():
    """Discover only Codex's executables, never broadly block node.exe / Python."""
    if os.name != 'nt':
        return []
    value = powershell("[Console]::OutputEncoding=[Text.Encoding]::UTF8; @(Get-CimInstance Win32_Process | "
                       "Where-Object {$_.Name -ieq 'codex.exe'} | Select-Object -ExpandProperty ExecutablePath -Unique) | ConvertTo-Json -Compress")
    paths = json.loads(value) if value else []
    if isinstance(paths, str):
        paths = [paths]
    root = Path(os.environ.get('LOCALAPPDATA', ''))/'OpenAI'/'Codex'/'bin'
    if root.is_dir():
        paths += [str(p) for p in root.glob('*/codex.exe')]
    return sorted(set(p for p in paths if p and Path(p).is_file()))
