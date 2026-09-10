"""Native WebView2 desktop host; the accounting engine stays in WebController."""
import ctypes
import json
import os
from pathlib import Path
import sys
import threading
import time

from . import __version__
from .storage import atomic_json
from .tray import Tray


class DesktopHost:
    def __init__(self, window, controller, args):
        self.window, self.controller, self.args = window, controller, args
        self.tray = None
        self.ready = False
        self.quitting = False
        self.timer = None
        self.animation_callback = None
        self.closed = threading.Event()
        self.navigation_handler = None

    def invoke(self, callback):
        from System import Action
        form = self.window.native
        if form and not form.IsDisposed:
            form.BeginInvoke(Action(callback))

    def corners(self):
        hwnd = self.window.native.Handle.ToInt64()
        dwm = ctypes.windll.dwmapi
        corner, border = ctypes.c_int(2), ctypes.c_uint(0xfffffffe)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(corner), 4)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 34, ctypes.byref(border), 4)

    def prepare(self):
        def configure():
            from System.Drawing import Size
            form = self.window.native
            scale = form.DeviceDpi / 96.0
            # Removing WinForms chrome changes ClientSize after pywebview sets Size.
            form.ClientSize = Size(round(750 * scale), round(680 * scale))
            self.corners()
        self.invoke(configure)

    def animate(self, showing, done=None):
        """Only compositor opacity changes here; the browser keeps its own frame clock."""
        from System.Windows.Forms import Timer
        form = self.window.native
        if self.timer:
            self.timer.Stop()
            self.timer.Dispose()
        began = time.monotonic()
        duration = .18 if showing else .12
        form.Opacity = 0.0 if showing else 1.0
        timer = self.timer = Timer()
        timer.Interval = 8
        def tick(_sender, _event):
            t = min(1.0, (time.monotonic()-began)/duration)
            eased = 1-(1-t)**3
            form.Opacity = eased if showing else 1-.85*eased
            if t >= 1:
                timer.Stop()
                timer.Dispose()
                self.timer = None
                if done:
                    done()
                if not form.IsDisposed:
                    form.Opacity = 1.0
        self.animation_callback = tick
        timer.Tick += tick
        timer.Start()

    def show(self):
        def visible():
            from System.Windows.Forms import FormWindowState
            form = self.window.native
            form.Opacity = 0.0
            form.Show()
            if form.WindowState == FormWindowState.Minimized:
                form.WindowState = FormWindowState.Normal
            self.corners()
            if not self.args.smoke_seconds:
                form.Activate()
            self.controller._set_hidden(False)
            if self.controller._engine:
                self.controller._engine.wakeup.set()
            self.animate(True)
        self.invoke(visible)

    def hide(self):
        def hidden():
            self.controller._set_hidden(True)
            self.animate(False, self.window.native.Hide)
        self.invoke(hidden)

    def minimize(self):
        def minimized():
            from System.Windows.Forms import FormWindowState
            self.controller._set_hidden(True)
            self.animate(False, lambda: setattr(self.window.native, 'WindowState', FormWindowState.Minimized))
        self.invoke(minimized)

    def restored(self):
        def visible():
            if not self.quitting:
                self.controller._set_hidden(False)
                if not self.timer:
                    self.animate(True)
        self.invoke(visible)

    def drag(self, resize=False):
        def begin():
            user = ctypes.windll.user32
            if not user.GetAsyncKeyState(1) & 0x8000:
                return
            hwnd = self.window.native.Handle.ToInt64()
            user.ReleaseCapture()
            user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            # Let Windows run its native move/size loop rather than relaying pointer positions.
            user.SendMessageW(hwnd, 0x0112 if resize else 0x00a1, 0xf008 if resize else 2, 0)
        self.invoke(begin)

    def quit_request(self):
        if self.controller.snapshot().get('view', {}).get('blocked'):
            self.show()
            self.window.evaluate_js("window.dispatchEvent(new CustomEvent('host-quit'))")
        else:
            self.quit()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        try:
            self.controller._close()
        except Exception:
            self.quitting = False
            raise
        if self.tray:
            self.tray.stop()
        self.window.destroy()

    def closing(self):
        if self.quitting:
            return True
        self.hide()
        return False

    def dispatch(self, action):
        if action == 'shown':
            if not self.ready:
                self.ready = True
                self.prepare()
                self.restrict_navigation()
                if not self.args.background:
                    self.show()
                path, nonce = self.args.update_health, self.args.update_nonce
                if path and nonce and len(nonce) == 48:
                    expected = self.args.data_dir.resolve()/'updates'/(nonce+'.health')
                    if path.resolve() == expected:
                        atomic_json(expected, dict(nonce=nonce, version=__version__))
            return {'ok': True}
        if action == 'minimize':
            self.minimize()
        elif action == 'close':
            self.hide()
        elif action == 'quit':
            self.quit()
        elif action == 'drag':
            self.drag()
        elif action == 'resize':
            self.drag(resize=True)
        elif action == 'maximize':
            self.maximize()
        elif action == 'browse_program':
            import webview
            paths = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                                   file_types=('Programs (*.exe)',))
            return {'ok': True, 'data': list(paths or ())}
        elif action == 'export_diagnostics':
            import webview
            paths = self.window.create_file_dialog(webview.FileDialog.SAVE,
                save_filename='CodexQuotaGuard-sync-diagnostics.json', file_types=('JSON (*.json)',))
            if paths:
                result = self.controller.command('diagnostics', {})
                if result.get('ok'):
                    Path(paths[0]).write_text(json.dumps(result.get('data'), ensure_ascii=False, indent=2), encoding='utf-8')
                return result
        else:
            return {'ok': False, 'error': '未知窗口操作'}
        return {'ok': True}

    def restrict_navigation(self):
        def restrict():
            core = self.window.native.webview.CoreWebView2
            initial = str(core.Source).split('#')[0]
            def navigating(_sender, event):
                if str(event.Uri).split('#')[0] != initial:
                    event.Cancel = True
            self.navigation_handler = navigating
            core.NavigationStarting += navigating
        self.invoke(restrict)

    def maximize(self):
        def change():
            from System.Windows.Forms import FormWindowState
            form = self.window.native
            form.WindowState = FormWindowState.Normal if form.WindowState == FormWindowState.Maximized else FormWindowState.Maximized
        self.invoke(change)


def run(args, config, database):
    import webview
    from .web_controller import WebController
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    html = root/'frontend'/'dist'/'index.html'
    if not html.exists():
        raise RuntimeError('界面文件缺失，请重新解压完整安装包。')
    controller = WebController(args.data_dir, config, database, demo=args.demo,
        startup_enabled=not args.demo and not args.smoke_seconds and not args.no_autostart)
    window = webview.create_window('Codex 配额管家 · '+__version__, str(html), js_api=controller,
        width=750, height=680, min_size=(730, 650), hidden=True, frameless=True, easy_drag=False,
        shadow=True, focus=not bool(args.smoke_seconds), background_color='#0c0d0f')
    host = DesktopHost(window, controller, args)
    controller._window_handler = host.dispatch
    window.events.closing += host.closing
    window.events.minimized += lambda: controller._set_hidden(True)
    window.events.restored += host.restored
    window.events.closed += host.closed.set
    def begin():
        try:
            controller._start()
            if not args.demo:
                host.tray = Tray(lambda callback: threading.Thread(target=callback, daemon=True).start(),
                                 host.show, host.quit_request)
                host.tray.start()
            if args.smoke_seconds:
                timer = threading.Timer(args.smoke_seconds, host.quit)
                timer.daemon = True
                timer.start()
        except Exception:
            host.quitting = True
            window.destroy()
            raise
    if args.background:
        controller._set_hidden(True)
    if (args.demo or args.smoke_seconds) and os.environ.get('CQG_DEBUG_PORT', '').isdigit():
        webview.settings['REMOTE_DEBUGGING_PORT'] = int(os.environ['CQG_DEBUG_PORT'])
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
    webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
    webview.settings['DRAG_REGION_SELECTOR'] = '[data-no-automatic-drag]'
    webview.settings['ALLOW_FILE_URLS'] = False
    webview.start(begin, gui='edgechromium', private_mode=True,
                  storage_path=str(args.data_dir/'webview-cache'), icon=str(root/'assets'/'app.ico'))
    if not host.quitting:
        controller._close()
    if host.tray:
        host.tray.stop()
