"""Render isolated demo widgets and exercise chart filters, notes and native frame restore.
Runs only when explicitly invoked; does not read user account data or move the mouse.
"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import customtkinter as ctk
from PIL import ImageGrab
from quota_guard.gui import App
from quota_guard.storage import Database, defaults
from quota_guard.analytics import usage

out = Path(__file__).resolve().parents[3]/'outputs'/'compact-ui'
out.mkdir(parents=True, exist_ok=True)
scratch = Path('work/compact-preview')
scratch.mkdir(parents=True, exist_ok=True)
ctk.set_appearance_mode('dark')
ctk.set_widget_scaling(float(__import__('os').environ.get('PREVIEW_SCALE', '1')))
ctk.set_window_scaling(1)
root = ctk.CTk()
root.withdraw()
cfg = defaults()
cfg.update(device_id='a', name='橙猫猫', tracked_accounts={'demo': {'cap': 50, 'label': '演示账号'}},
           device_notes={'demo': {'b': '渲染工作站'}}, auto_update=False)
app = App(root, scratch, cfg, Database(scratch/'local.sqlite'), demo=True)
root.geometry('750x550+40+40')
db = Database(scratch/'charts.sqlite')
now = time.time()
with db.connect() as conn:
    conn.execute('DELETE FROM events')
    for day in range(30):
        for hour in range(24):
            for j, model in enumerate(['gpt-6-astra', 'gpt-5.6-sol']):
                ts = now-((29-day)*24+hour)*3600-120
                device = 'a' if (hour+j)%3 else 'b'
                tokens = int(30000 + 130000*abs(__import__('math').sin(hour*.61+day*.8+j)))
                weight = tokens/1000000 * (250 if j==0 else 100)
                conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                    (f'{day}-{hour}-{j}',device,'demo',ts,model,tokens,weight,1))
with db.connect() as conn:
    conn.execute('DELETE FROM epochs')
    conn.execute("INSERT INTO epochs(account,started,baseline,used,reset_at,observed_at,reason) VALUES ('demo',?,0,50,?,?, 'fixture')",
                 (now-2*86400, now+4*86400, now))
devices = [dict(id='a', name='橙猫猫', active=1, uncertain=0, online=True, tokens=76580000,
                estimated=16.76, cap=50, seen=now, scan_at=now),
           dict(id='b', name='DESKTOP-N416O9F', active=0, uncertain=0, online=True, tokens=148360000,
                estimated=33.24, cap=50, seen=now, scan_at=now)]
view = dict(identity=dict(mode='account',account='demo', label='示例账号 · 演示数据', plan='pro'),
            summary=dict(account='demo',epoch=dict(used=50,baseline=0,reset_at=now+4*86400),
                         devices=devices,unassigned=0,provisional=0,reset_pending=False),
            analytics=usage(db,'demo',now), auto_block=False, active=1, status='已同步 · 演示数据，非实际账本',
            peers={'b': {'route':'Tailscale · 直连'}})
errors=[]
root.report_callback_exception=lambda *args: errors.append(str(args[1]))
app.render(view)

user = ctypes.windll.user32

def settle():
    until = time.monotonic()+.5
    while time.monotonic()<until:
        root.update()
        time.sleep(.015)

def capture(name):
    settle()
    hwnd = user.GetParent(root.winfo_id())
    rect=wintypes.RECT()
    user.GetWindowRect(hwnd, ctypes.byref(rect))
    ImageGrab.grab(bbox=(rect.left,rect.top,rect.right,rect.bottom)).save(out/name)

# Isolated synthetic window, shown without activation or mouse input.
root.update_idletasks()
hwnd=user.GetParent(root.winfo_id())
user.SetWindowLongW(hwnd, -20, user.GetWindowLongW(hwnd, -20) | 0x08000020)
user.EnableWindow(hwnd, False)
root.deiconify()
user.ShowWindow(hwnd,4)
root.attributes('-topmost', True)
capture('overview.png')
corner = ctypes.c_int()
assert ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4) == 0 and corner.value == 2
viewport = app.overview
assert app.table.winfo_rooty()+app.table.winfo_height() <= viewport.winfo_rooty()+viewport.winfo_height()
assert not isinstance(app.overview, ctk.CTkScrollableFrame)
for row, _ in app.table.rows.values():
    assert row.winfo_height() >= row.winfo_reqheight()-1, ('Device content clipped', root.winfo_geometry(), row.winfo_height(), row.winfo_reqheight(), app.table.winfo_height(), app.table.winfo_reqheight())
    assert row.winfo_rooty()+row.winfo_height() <= app.table.winfo_rooty()+app.table.winfo_height(), 'Device row clipped'
assert sum(app.pie_chart.target) == sum(d['tokens'] for d in devices)
# Selection survives both value refreshes and a temporary empty projection.
import copy
app.table._select('a')
for _ in range(12):
    app.render(view)
empty_projection = dict(view, summary=None)
app.render(empty_projection)
app.render(view)
assert app.table.selection() == ('a',)
assert app.table.rows['a'][0].cget('fg_color') == '#1d2b40'
assert app.table.rows['a'][0].cget('border_color') == '#547db7'
assert not app.pie_detail.get()
offset, _ = app.pie_chart.legend_geometry(app.pie_chart.winfo_height())
app.pie_chart.motion(SimpleNamespace(x=175, y=offset+10))
settle()
assert app.pie_detail.get().endswith(' Token') and '%' not in app.pie_detail.get()
assert not any('%' in app.pie_chart.itemcget(item, 'text') for item in app.pie_chart.find_all()
               if app.pie_chart.type(item) == 'text')
capture('pie-hover-footer.png')
app.pie_chart.leave()
settle()
assert not app.pie_detail.get()

draws = []
draw = app.line_chart.draw
app.line_chart.draw = lambda: (draws.append(1), draw())[-1]
app.drag_window(SimpleNamespace(x_root=100, y_root=100))
for i in range(10):
    app.move_window(SimpleNamespace(x_root=100+i, y_root=100+i))
    settle()
app.end_move()
assert not draws, 'Moving an unchanged window must not rerasterize its charts'
app.line_chart.draw = draw
user.SetWindowPos(hwnd, 0, 40, 40, 0, 0, 0x0015)
assert not user.GetWindowLongW(hwnd,-16) & 0x00C40000
assert app.meter.master is not app.overview
size = (root.winfo_width(), root.winfo_height())
app.begin_resize(SimpleNamespace(x_root=0, y_root=0))
app.resize_window(SimpleNamespace(x_root=20, y_root=10))
settle()
assert root.winfo_width() == size[0]+20 and root.winfo_height() == size[1]+10, (size, root.winfo_geometry())
user.SetWindowPos(hwnd, 0, 0, 0, *size, 0x0016)
settle()
assert app.limit_panel.master is app.settings
assert 'cap' not in app.table.columns
import copy
retired = copy.deepcopy(view)
retired['summary']['devices'].append(dict(devices[1], id='retired', name='USER-20241018IW', removed=True))
for window in retired['analytics']['windows'].values():
    window['rows'].append(dict(device='retired', model='gpt-6-astra', bucket=0, tokens=99999999, weight=999, unknown=0))
before = list(app.line_chart.target)
app.render(retired)
assert 'USER-20241018IW' not in app.pie_chart.labels
assert 'retired' not in app.device_choices.values()
assert app.line_chart.target == before
app.render(view)
app.select_quota_display('个人额度 = 100%')
assert app.cards['local'].get() == '等待确认'
assert app.table.rows['b'][1][3].cget('text') == '66.48%'
app.select_quota_display('账号总额度')
app.select_chart_device(next(k for k, v in app.device_choices.items() if v == 'b'))
assert sum(app.line_chart.target) < sum(app.last_view['analytics']['windows']['day']['rows'][i]['tokens']
    for i in range(len(app.last_view['analytics']['windows']['day']['rows'])))
app.select_chart_device('全部用户')
assert app.table.winfo_rooty()+app.table.winfo_height() <= root.winfo_rooty()+root.winfo_height()
app.model_choice.set('gpt-6-astra')
app.window_choice.set('一周')
app.render_charts()
capture('model-week.png')
app.window_choice.set('一月')
app.render_charts()
settle()
assert len(app.line_chart.target)==30
app.line_chart.motion(SimpleNamespace(x=160, y=70))
capture('model-month-hover.png')
previous_hover = app.line_chart.hover_position
app.line_chart.motion(SimpleNamespace(x=240, y=70))
assert previous_hover < app.line_chart.hover_position < app.line_chart.hover_target
until = time.monotonic()+2
while abs(app.line_chart.hover_position-app.line_chart.hover_target) >= .01 and time.monotonic() < until:
    root.update()
    time.sleep(.015)
assert abs(app.line_chart.hover_position-app.line_chart.hover_target) < .01

def check_button_centers(widget):
    if isinstance(widget, ctk.CTkButton) and widget.winfo_ismapped() and widget._image_label is None:
        label = widget._text_label
        if label is not None:
            assert abs(label.winfo_x()+label.winfo_width()/2-widget.winfo_width()/2) <= 1.5
            assert abs(label.winfo_y()+label.winfo_height()/2-widget.winfo_height()/2) <= 1.5
    for child in widget.winfo_children():
        check_button_centers(child)

check_button_centers(root)
app.model_menu._open_dropdown_menu()
settle()
popup = app.model_menu.popup
assert popup is not None and popup.winfo_exists()
for _ in range(20):
    app.render(view)
assert app.model_menu.popup is popup and popup.winfo_exists()
capture('dropdown-stable.png')
popup.destroy()
app.model_menu.popup = None
before = list(app.line_chart.target)
app.chart_choice.set('柱状')
app.render_charts()
settle()
assert app.line_chart.kind == 'bar' and app.line_chart.target == before
app.line_chart.motion(SimpleNamespace(x=160, y=70))
capture('model-month-bars.png')
before = list(app.line_chart.target)
for period in ['本周期', '历史累计', '一天使用', '七天使用', '一个月使用']:
    app.pie_window.set(period)
    app.render_charts()
    settle()
    assert sum(app.pie_chart.target) > 0
    assert app.line_chart.target == before
capture('pie-total-and-bars.png')
app.save_device_color('demo', 'b', '#ed8299')
app.select_chart_device(next(k for k, v in app.device_choices.items() if v == 'b'))
app.render(view)
assert app.line_chart.colors == ('#ed8299',)
assert app.pie_chart.colors[app.pie_chart.labels.index('渲染工作站')] == '#ed8299'
assert app.table.rows['b'][1][3].cget('text_color') == '#ed8299'
capture('custom-user-color.png')
app.select_chart_device('全部用户')
app.pie_window.set('最近一小时')
app.chart_choice.set('曲线')
app.render_charts()
app.tabs.select(app.settings)
assert app.tabs.job is not None
capture('settings.png')
assert app.tabs.job is None
assert not errors, errors
for tab, name in [(app.accounts_tab, 'accounts.png'), (app.pair_tab, 'sync.png')]:
    app.tabs.select(tab)
    capture(name)
app.tabs.select(app.overview)
app.table._select('b')
app.edit_device_note()
settle()
for child in root.winfo_children():
    if isinstance(child, ctk.CTkToplevel):
        child.destroy()
app.save_device_note('demo','b','副机')
app.render(view)
assert app.table.rows['b'][1][0].cget('text')=='副机'
app.render(dict(identity=dict(mode='api'), summary=None, auto_block=False))
settle()
assert not app.pie_chart.target and not any(app.line_chart.target)
capture('empty.png')
app.minimize()
assert float(root.attributes('-alpha')) <= 1
settle()
assert root.state() == 'iconic'
root.deiconify()
settle()
app.tray = SimpleNamespace()
app.close()
assert app.window_job is not None
settle()
assert root.state() == 'withdrawn'
app.show()
assert app.window_job is not None
settle()
assert root.state() == 'normal' and float(root.attributes('-alpha')) == 1
assert not user.GetWindowLongW(hwnd,-16) & 0x00C00000
root.destroy()
assert not errors, errors
print('COMPACT_REAL_WIDGETS_FILTERS_NOTES_EMPTY_AND_ANIMATIONS_OK', out)
