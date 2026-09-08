"""Exercise the real pairing widgets with explicitly synthetic transport evidence."""
from pathlib import Path
import ctypes
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import customtkinter as ctk
from PIL import ImageGrab
from quota_guard.gui import App
from quota_guard.pairing import create_code
from quota_guard.storage import Database, defaults

out = Path('work/ui-preview-0.3.0')
out.mkdir(parents=True, exist_ok=True)
ctk.set_appearance_mode('dark')
root = ctk.CTk()
cfg = defaults()
cfg.update(device_id='demo-a', name='演示工作站', autostart=False, auto_update=False)
app = App(root, out, cfg, Database(out/'preview.sqlite'), demo=True)
app.account.set('演示数据 · 非实际双设备连接')
app.mesh_label.set('连接诊断：演示路径状态，不代表已完成异地验收')
app.tabs.select(app.pair_tab)
app.send_tailscale()
root.update()
assert app.pair_panel.code.startswith('CQG4.')
assert create_code(app.config).startswith('CQG4.')
for window in root.winfo_children():
    if window.winfo_toplevel() is window and window is not root:
        window.destroy()
def capture(name):
    root.update()
    time.sleep(.35)
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    ImageGrab.grab(window=hwnd).save(out/name)
capture('pairing-code.png')
app.pair_flow = dict(stage='saved')
view = dict(peers={'demo-b':dict(route='Tailscale · DERP 中转 (hkg)')},
            sync_receipts={'demo-b':time.time()}, connection=dict(transport='tailscale',ready=True))
app.pair_panel.render(app.pair_flow, view)
assert 'DERP 中转' in app.pair_panel.detail.cget('text')
capture('pairing-derp-demo.png')
view['peers']['demo-b']['route'] = 'Tailscale · 直连'
app.pair_panel.render(app.pair_flow, view)
assert '直连' in app.pair_panel.detail.cget('text')
capture('pairing-direct-demo.png')
app.pair_panel.render(dict(stage='authorizing'), dict(connection=dict(transport='tailscale',state='NeedsLogin')))
assert '授权' in app.pair_panel.detail.cget('text')
capture('pairing-login-demo.png')
app.tabs.select(app.overview)
app.table.insert('', 'end', iid='demo-b', values=('演示笔记本', '暂无近期活动',
    'Tailscale · DERP 中转 (hkg)', '0', '123,456', '1.20%', '33%'))
app.table.insert('', 'end', iid='demo-c', values=('演示工作站', '暂无近期活动',
    'Tailscale · 直连', '0', '456,789', '2.30%', '33%'))
capture('overview-routes-demo.png')
app.quit()
print('TSNET_REAL_WIDGETS_SYNTHETIC_STATES_OK')
