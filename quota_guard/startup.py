"""Current-user logon startup; elevated limiting uses an interactive scheduled task."""
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import winreg

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def launch_command(folder):
    if getattr(sys, 'frozen', False):
        executable, args = sys.executable, []
    else:
        executable = str(Path(sys.executable).with_name('pythonw.exe'))
        args = [str(Path(__file__).resolve().parents[1]/'main.py')]
    args += ['--data-dir', str(Path(folder).resolve()), '--background']
    return executable, args


def _ps(script):
    command = base64.b64encode((script+'; exit 0').encode('utf-16le')).decode()
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    if result.returncode:
        raise RuntimeError('无法更新自启动任务；请以管理员身份运行后重试。')


def apply(enabled, folder, elevated=False):
    name = 'CodexQuotaGuard-'+hashlib.sha256(str(Path(folder).resolve()).encode()).hexdigest()[:12]
    executable, args = launch_command(folder)
    quote = lambda text: "'"+text.replace("'", "''")+"'"
    if enabled and elevated:
        script = ("$ErrorActionPreference='Stop'; "
            f'$a=New-ScheduledTaskAction -Execute {quote(executable)} -Argument {quote(subprocess.list2cmdline(args))}; '
            '$u=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name; '
            '$t=New-ScheduledTaskTrigger -AtLogOn -User $u; '
            '$p=New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Highest; '
            '$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero); '
            f'Register-ScheduledTask -TaskName {quote(name)} -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null')
        _ps(script)
    else:
        _ps("$ErrorActionPreference='Stop'; "+
            f'if (Get-ScheduledTask -TaskName {quote(name)} -ErrorAction SilentlyContinue) {{ Unregister-ScheduledTask -TaskName {quote(name)} -Confirm:$false }}')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled and not elevated:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, subprocess.list2cmdline([executable, *args]))
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
