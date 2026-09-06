"""Exercise the real tray and temporary, immediately cleaned startup registrations."""
import hashlib
from pathlib import Path
import sys
import threading
import time
import winreg
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import customtkinter as ctk
from quota_guard.gui import App
from quota_guard.storage import Database, defaults
from quota_guard.tray import Tray
from quota_guard import startup
from quota_guard.firewall import is_admin


folder = Path('work/background-probe-'+str(time.time_ns())).resolve()
folder.mkdir(parents=True)
root = ctk.CTk()
app = App(root, folder, defaults(), Database(folder/'local.sqlite'), demo=True)
engine = MagicMock()
engine.blocked = True
engine.wakeup = threading.Event()
engine.snapshot.return_value = dict(identity={}, summary=None, notifications=[])
app.engine = engine
app.tray = Tray(app.ui_actions.put, app.show, app.quit)
try:
    app.tray.start()
    root.update()
    time.sleep(.5)
    assert app.tray.icon.visible
    app.close()
    root.update()
    assert root.state() == 'withdrawn' and app.hidden and engine.background_mode
    engine.close.assert_not_called()
    engine.restore.assert_not_called()
    # Trigger pystray's real default menu action, delivered to the UI queue.
    app.tray.icon.menu.items[0](app.tray.icon)
    app.ui_actions.get(timeout=2)()
    root.update()
    assert root.state() == 'normal' and not app.hidden and not engine.background_mode
    engine.blocked = False
    app.tray.icon.menu.items[-1](app.tray.icon)
    app.ui_actions.get(timeout=2)()
    deadline = time.time()+10
    while not app.exited and time.time() < deadline:
        root.update()
        time.sleep(.05)
    assert app.exited
    engine.close.assert_called_once()
    print('REAL_TRAY_HIDE_SHOW_EXIT_OK', flush=True)
finally:
    app.tray.stop()
    if not app.exited:
        root.destroy()

name = 'CodexQuotaGuard-'+hashlib.sha256(str(folder).encode()).hexdigest()[:12]
try:
    startup.apply(True, folder)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, startup.RUN_KEY) as key:
        value, kind = winreg.QueryValueEx(key, name)
        assert '--background' in value and str(folder) in value
    if is_admin():
        startup.apply(True, folder, elevated=True)
        startup._ps(f"$t=Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop; "
                    "if ($t.Principal.RunLevel -ne 'Highest' -or $t.Settings.ExecutionTimeLimit -ne 'PT0S') { throw 'Task mismatch' }")
        print('TEMP_ELEVATED_LOGON_TASK_OK', flush=True)
finally:
    startup.apply(False, folder)
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, startup.RUN_KEY) as key:
    try:
        winreg.QueryValueEx(key, name)
        raise AssertionError('Test Run entry remains')
    except FileNotFoundError:
        pass
startup._ps(f"if (Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue) {{ throw 'Test task remains' }}")
print('STARTUP_ENABLE_DISABLE_AND_CLEANUP_OK', flush=True)
