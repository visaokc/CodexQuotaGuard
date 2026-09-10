import pystray
from .app_icon import icon_image


class Tray:
    def __init__(self, dispatch, show, quit):
        image = icon_image(64)
        self.icon = pystray.Icon('CodexQuotaGuard', image, 'Codex 配额管家 · 后台监测',
            pystray.Menu(pystray.MenuItem('显示主窗口', lambda: dispatch(show), default=True),
                         pystray.MenuItem('退出程序', lambda: dispatch(quit))))

    def start(self):
        self.icon.run_detached()

    def stop(self):
        self.icon.stop()

    def notify(self, text):
        self.icon.notify(text, 'Codex 配额管家')
