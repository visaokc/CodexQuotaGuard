"""Render the real Windows GUI with explicitly labelled synthetic preview data."""
import ctypes
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import customtkinter as ctk
from PIL import ImageGrab

from quota_guard.gui import App
from quota_guard.storage import Database, defaults
from quota_guard.pairing import create_code, load_config


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('work/ui-preview')
    out.mkdir(parents=True, exist_ok=True)
    ctk.set_appearance_mode('dark')
    ctk.set_widget_scaling(1.0)
    ctk.set_window_scaling(1.0)
    root = ctk.CTk()
    cfg = defaults()
    cfg.update(name='工作站 A', device_id='a', rendezvous_url='wss://relay.example.com', relay_token='demo-service-key-not-real-'+'x'*20)
    cfg['tracked_accounts'] = {'a'*64: dict(label='demo@example.com', added_at=0, cap=33),
                               'b'*64: dict(label='second@example.com', added_at=0, cap=40)}
    app = App(root, out, cfg, Database(out/'preview.sqlite'), demo=True)
    now = time.time()
    view = dict(identity=dict(mode='account', account='a'*64, label='演示账号 · demo@example.com', plan='pro'), status='演示数据 · 非真实使用量',
                error='', mesh='发现服务已连接 · 演示状态', blocked=False, auto_block=True, active=3, uncertain=0,
                unbound_active=1, unbound_uncertain=0,
                history=dict(current=dict(day=18400000, week=32100000, month=145200000)),
                peers={'b': {'route': 'P2P 直连'}, 'c': {'route': '中转'}},
                summary=dict(epoch=dict(used=62, baseline=15, reset_at=now+3*86400), unassigned=2.0, provisional=1.2,
                    reset_pending=False, calibration={'samples': 6}, devices=[
                        dict(id='a', name='工作站 A', online=True, active=3, uncertain=0, unbound_active=1, tokens=18400000, estimated=24.35, cap=33),
                        dict(id='b', name='笔记本 B', online=True, active=1, uncertain=0, tokens=12600000, estimated=16.80, cap=33),
                        dict(id='c', name='家用电脑 C', online=False, active=0, uncertain=0, tokens=3200000, estimated=3.85, cap=33)]))
    app.render(view)
    root.update()
    assert len(app.table.get_children()) == 3
    assert app.cards['local'].get() == '24.35% / 33%'
    def capture(name):
        root.update()
        time.sleep(.35)
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ImageGrab.grab(window=hwnd).save(out/name)
    capture('overview.png')
    # Test real modal route and widget events, not only the pure number parser.
    submitted = []
    app.engine = SimpleNamespace(snapshot=lambda: view,
        set_cap=lambda value, expected_account: submitted.append((expected_account, value)))
    app.change_cap()
    root.update()
    dialog = app.cap_dialog
    assert dialog.winfo_exists()
    assert app.limit_panel.card.cget('border_color') == '#69d9bd'
    # Pending settings must not be overwritten by an old worker snapshot.
    app.busy, app.limit_pending = True, False
    app.config['auto_block'] = False
    app.auto_block.set(False)
    app.render_limit(view)
    assert not app.auto_block.get() and not app.config['auto_block']
    app.busy, app.limit_pending = False, None
    app.render(view)
    dialog.entry.delete(0, 'end')
    dialog.entry.insert(0, '42.75')
    root.update()
    assert abs(dialog.slider.get()-42.75) < .001
    dialog.entry.delete(0, 'end')
    dialog.entry.insert(0, '101')
    root.update()
    assert dialog.save_button.cget('state') == 'disabled'
    canvas = dialog.slider._canvas
    canvas.event_generate('<Button-1>', x=int(canvas.winfo_width()*.60), y=12)
    root.update()
    assert 55 < float(dialog.value.get()) < 65
    canvas.event_generate('<B1-Motion>', x=int(canvas.winfo_width()*.33), y=12)
    root.update()
    assert 28 < float(dialog.value.get()) < 38
    dialog.value.set('33.5')
    root.update()
    time.sleep(.2)
    root.update()
    assert dialog.save_button.winfo_rooty()+dialog.save_button.winfo_height() <= dialog.winfo_rooty()+dialog.winfo_height()
    ImageGrab.grab(window=ctypes.windll.user32.GetParent(dialog.winfo_id())).save(out/'cap-dialog.png')
    dialog.save_button.invoke()
    root.update()
    assert submitted == [('a'*64, 33.5)]
    app.change_cap()
    root.update()
    app.cap_dialog.cancel()
    root.update()
    assert len(submitted) == 1
    app.engine = None
    for key, sample in [('limit-off', dict(view, auto_block=False)), ('limit-blocked', dict(view, blocked=True)),
                        ('limit-api', dict(view, identity=dict(mode='api', account='', label='API'))),
                        ('activity-no-quota', dict(view, summary=None))]:
        app.render(sample)
        capture(key+'.png')
    assert '归属待确认活动 1' in app.observed.get()
    app.render(view)
    app.overview._parent_canvas.yview_moveto(1)
    capture('token-history.png')
    app.overview._parent_canvas.yview_moveto(0)
    app.tabs.select(app.accounts_tab)
    capture('accounts.png')
    assert app.account_list.size() == 2
    with patch('quota_guard.gui.identity', return_value=dict(mode='account', account='c'*64, label='third@example.com')):
        app.add_account()
    assert app.account_list.size() == 3
    assert load_config(out/'settings.json')['tracked_accounts']['c'*64]['label'] == 'third@example.com'
    app.account_list.selection_set(2)
    with patch('quota_guard.gui.messagebox.askokcancel', return_value=True):
        app.remove_account()
    assert app.account_list.size() == 2
    with patch('quota_guard.gui.identity', return_value=dict(mode='api', account='', label='API')):
        with patch('quota_guard.gui.messagebox.showerror') as error:
            app.add_account()
            error.assert_called_once()
    assert app.account_list.size() == 2
    app.tabs.select(app.pair_tab)
    capture('pairing.png')
    app.config['rendezvous_url'] = ''
    with patch('quota_guard.gui.messagebox.showinfo') as technical_error:
        app.send_pair()
        root.update()
        technical_error.assert_not_called()
    dialogs = [w for w in root.winfo_children() if w.winfo_toplevel() is w and w is not root]
    deadline = time.monotonic()+20
    while app.busy and time.monotonic()<deadline:
        root.update();time.sleep(.1)
    root.update()
    dialogs = [w for w in root.winfo_children() if w.winfo_toplevel() is w and w is not root]
    assert dialogs and dialogs[0].title() == '发送匹配码'
    assert app.pair_panel.code.startswith('CQG2.')
    app.pair_panel.copy.invoke()
    assert root.clipboard_get() == app.pair_panel.code
    assert app.config['link_enabled'] and not app.advanced.winfo_manager()
    assert create_code(app.config).startswith('CQG2.')
    ImageGrab.grab(window=ctypes.windll.user32.GetParent(dialogs[0].winfo_id())).save(out/'pairing-setup.png')
    dialogs[0].destroy()
    # Real refresh route must preserve operation state and never imply a pasted code is connected.
    app.pair_flow = dict(stage='preparing', started=time.monotonic()-15)
    waiting = dict(view, peers={}, sync_receipts={}, connection=dict(phase='waiting', relay_ready=False))
    app.render(waiting)
    assert '正在生成匹配码' in app.pair_panel.title.cget('text')
    app.fail_pair('测试：公共连接准备超时')
    app.render(waiting)
    assert app.pair_panel.title.cget('text') == '匹配码尚未生成'
    capture('pairing-failed.png')
    app.pair_flow = dict(stage='saved')
    app.pair_panel.set_code('')
    app.render(dict(waiting, connection=dict(phase='waiting', relay_ready=True)))
    assert '已保存' in app.pair_panel.title.cget('text')
    capture('pairing-saved.png')
    connected = dict(view, peers={'b': {'route': '公共加密中转'}}, sync_receipts={})
    app.render(connected)
    assert '等待首次用量同步' in app.pair_panel.title.cget('text')
    capture('pairing-connected.png')
    app.render(dict(connected, sync_receipts={'b': time.time()}))
    assert '已收到同步数据' in app.pair_panel.title.cget('text')
    capture('pairing-synced.png')
    # Busy work cannot extend the invitation deadline forever; obsolete callbacks are ignored.
    app.busy = True
    app.wait_pair_ready(time.monotonic()-1, app.pair_request)
    assert app.pair_flow['stage'] == 'failed'
    app.pair_request += 1
    app.pair_flow = dict(stage='saved')
    app.wait_pair_ready(time.monotonic()-1, app.pair_request-1)
    assert app.pair_flow['stage'] == 'saved'
    app.busy = False
    app.tabs.select(app.settings)
    capture('settings.png')
    assert app.auto_update.get()
    app.update_button.invoke()
    assert '不安装更新' in app.update_status.get()
    # Exercise the real sender flow, no network connection is made in demo mode.
    app.send_pair()
    deadline = time.monotonic()+20
    while app.busy and time.monotonic()<deadline:
        root.update();time.sleep(.1)
    root.update()
    dialogs = [w for w in root.winfo_children() if w.winfo_toplevel() is w and w is not root]
    assert dialogs, 'Sender dialog missing'
    for w in dialogs:
        w.destroy()
    app.receive_pair()
    root.update()
    dialogs = [w for w in root.winfo_children() if w.winfo_toplevel() is w and w is not root]
    assert dialogs, 'Receiver dialog missing'
    peer = defaults()
    peer.update(rendezvous_url='wss://relay.example.com', relay_token='test-'+'y'*32)
    with patch('quota_guard.gui.messagebox.askokcancel', return_value=True):
        app.accept_pair(create_code(peer), dialogs[0])
    saved = load_config(out/'settings.json')
    assert saved['group_secret'] == peer['group_secret']
    assert saved['device_id'] == 'a', 'Pairing must not copy another device identity'
    assert app.pair_flow['stage'] == 'saved'
    app.tabs.select(app.overview)
    root.geometry('1100x720')
    capture('overview-compact.png')
    app.overview._parent_canvas.yview_moveto(1)
    capture('overview-compact-bottom.png')
    app.tabs.select(app.pair_tab)
    app.toggle_advanced()
    root.update()
    app.pair_tab._parent_canvas.yview_moveto(1)
    capture('pairing-compact-bottom.png')
    root.destroy()
    print('GUI_PREVIEW_OK')


if __name__ == '__main__':
    main()
