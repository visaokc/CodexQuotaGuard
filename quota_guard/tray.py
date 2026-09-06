import pystray
from PIL import Image, ImageDraw


class Tray:
    def __init__(self, dispatch, show, quit):
        image = Image.new('RGBA', (64, 64))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((2, 2, 62, 62), radius=16, fill='#69d9bd')
        draw.arc((15, 13, 49, 51), 45, 315, fill='#142c29', width=7)
        self.icon = pystray.Icon('CodexQuotaGuard', image, 'Codex 配额管家 · 后台监测',
            pystray.Menu(pystray.MenuItem('显示主窗口', lambda: dispatch(show), default=True),
                         pystray.MenuItem('退出程序', lambda: dispatch(quit))))

    def start(self):
        self.icon.run_detached()

    def stop(self):
        self.icon.stop()

    def notify(self, text):
        self.icon.notify(text, 'Codex 配额管家')
