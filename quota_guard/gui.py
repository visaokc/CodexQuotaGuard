import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from . import skin as ctk

from . import __version__
from .accounts import enroll
from .engine import Engine
from .firewall import discover_programs, is_admin
from .pairing import create_code, read_code, save_config
from .quota import identity
from . import startup
from .tray import Tray
from .limit_controls import CapDialog, LimitPanel, limit_presentation
from .pair_status import PairPanel
from .token_budget import budget_text
from .analytics import chart_data, quota_display
from .charts import Chart, compact, COLORS, blend
from .app_icon import icon_image
from .sync_diagnostics import progress_text, report as sync_report

BG, PANEL, FG, MUTED, ACCENT = '#0c0d0f', '#181a1d', '#eef0f4', '#89909c', '#669cff'
USER_COLORS = dict(zip(('蓝色', '琥珀', '薄荷', '薰衣草', '玫瑰', '青柠'), COLORS))
USER_COLORS.update(珊瑚='#f39777', 晴青='#62cde2')


def sync_caption(view):
    states = [p.get('state') for p in view.get('sync_progress', {}).values()]
    if 'error' in states:
        return '同步异常'
    if 'syncing' in states:
        return '同步中'
    if any(state in ('waiting', 'stale') for state in states):
        return '等待确认'
    if states and all(state == 'caught_up' for state in states):
        return '已同步'
    return '等待确认' if view.get('peers') else '未连接'


def sync_confirmed_at(view):
    peers = view.get('sync_progress', {})
    receipts = view.get('sync_receipts', {})
    if not peers or any(p.get('state') != 'caught_up' or not receipts.get(peer)
                        for peer, p in peers.items()):
        return None
    return min(receipts[peer] for peer in peers)


def button(parent, text, command=None, style=None, **kwargs):
    primary = style == 'Accent.TButton'
    options = dict(height=30, width=96, corner_radius=7,
                   fg_color=ACCENT if primary else '#25282e',
                   hover_color='#86b1ff' if primary else '#343942',
                   text_color='#0d1629' if primary else FG,
                   font=('Microsoft YaHei UI', 11, 'bold' if primary else 'normal'))
    options.update(kwargs)
    return ctk.CTkButton(parent, text=text, command=command, **options)


class Navigation:
    def __init__(self, sidebar, title):
        self.sidebar, self.title = sidebar, title
        self.pages = []
        self.buttons = []
        self.active, self.job = None, None

    def add(self, page, text):
        index = len(self.pages)
        self.pages.append(page)
        icons = ['◫', '◎', '⇄', '⚙', 'ⓘ']
        b = ctk.CTkButton(self.sidebar, text=icons[index]+'  '+text, width=88, height=32,
                         corner_radius=9, fg_color='transparent', hover_color='#25282e',
                         text_color=MUTED, font=('Microsoft YaHei UI', 12),
                         command=lambda: self.select(page))
        b.pack(side='left', padx=3, pady=4)
        self.buttons.append((b, text))

    def select(self, page):
        if self.active is page:
            return
        if self.job:
            self.active.after_cancel(self.job)
            self.job = None
        for p, (b, name) in zip(self.pages, self.buttons):
            if p is page:
                b.configure(fg_color='#1c2a40', text_color=ACCENT)
                self.title.set(name)
            else:
                p.pack_forget()
                p.place_forget()
                b.configure(fg_color='transparent', text_color=MUTED)
        initial = self.active is None
        self.active = page
        if initial:
            page.pack(fill='both', expand=True)
            return
        def tick(frame=0):
            self.job = None
            page.place(x=round(16*(1-frame/8)**3), y=0, relwidth=1, relheight=1)
            if frame < 8:
                self.job = page.after(16, lambda: tick(frame+1))
            else:
                page.place_forget()
                page.pack(fill='both', expand=True)
        tick()


class Meter(ctk.CTkProgressBar):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, height=7, corner_radius=4, fg_color='#293749', progress_color=ACCENT)
        self.set(0)

    def __setitem__(self, key, value):
        if key == 'value':
            if getattr(self, '_last_value', None) != value:
                self._last_value = value
                self.set(float(value)/100)


class DeviceTable(ctk.CTkFrame):
    """A native, keyboard-selectable device list without legacy table borders."""
    def __init__(self, parent, columns, **kwargs):
        super().__init__(parent, fg_color='transparent', corner_radius=0)
        self.columns, self.rows, self.selected = columns, {}, None
        self.subtitles, self.avatars = {}, {}
        self.header_labels = {}
        self.headers = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10, height=32)
        self.headers.pack(fill='x', pady=(0, 3))
        self.empty = ctk.CTkLabel(self, text='等待设备数据\n开始监测或匹配另一台设备后，会在这里显示。',
                                  text_color=MUTED, font=('Microsoft YaHei UI', 12))
        self.empty.pack(expand=True)
        self.bind('<Up>', lambda _: self._move(-1))
        self.bind('<Down>', lambda _: self._move(1))

    def heading(self, col, text):
        if col in self.header_labels:
            self.header_labels[col].configure(text=text)
            return
        i = self.columns.index(col)
        self.headers.grid_columnconfigure(i, weight=2 if i == 0 else 1, uniform='cols')
        label = ctk.CTkLabel(self.headers, text=text, text_color=MUTED, anchor='w' if i == 0 else 'center',
                              font=('Microsoft YaHei UI', 11))
        label.grid(row=0, column=i, sticky='ew', padx=(10, 4), pady=3)
        self.header_labels[col] = label

    def column(self, *_args, **_kwargs):
        pass

    def tag_configure(self, *_args, **_kwargs):
        pass

    def get_children(self):
        return tuple(self.rows)

    def selection(self):
        return (self.selected,) if self.selected in self.rows else ()

    def _move(self, step):
        keys = list(self.rows)
        if keys:
            index = keys.index(self.selected) if self.selected in keys else 0
            self._select(keys[max(0, min(len(keys)-1, index+step))])

    def _select(self, iid):
        self.selected = iid
        self.focus_set()
        for key, (row, labels) in self.rows.items():
            row.configure(fg_color='#1d2b40' if key == iid else '#181c22',
                          border_color='#547db7' if key == iid else '#252b34')

    def insert(self, parent, index, iid, values, tags=()):
        self.empty.pack_forget()
        row = ctk.CTkFrame(self, fg_color='#181c22', corner_radius=10, height=44,
                          border_width=1, border_color='#252b34')
        row.pack(fill='x', pady=3)
        row.bind('<Button-1>', lambda _: self._select(iid))
        labels = []
        for i, value in enumerate(values):
            row.grid_columnconfigure(i, weight=2 if i == 0 else 1, uniform='cols')
            if i == 0:
                identity = ctk.CTkFrame(row, fg_color='transparent', corner_radius=0)
                identity.grid(row=0, column=0, sticky='ew', padx=10, pady=5)
                avatar = ctk.CTkLabel(identity, text=str(value)[:1], width=34, height=34,
                    corner_radius=8, fg_color='#24354d', text_color='#8bb8ff',
                    font=('Microsoft YaHei UI', 14, 'bold'))
                avatar.pack(side='left', padx=(0, 9))
                text = ctk.CTkFrame(identity, fg_color='transparent', corner_radius=0)
                text.pack(side='left', fill='x', expand=True)
                label = ctk.CTkLabel(text, text=value, anchor='w', height=22,
                                     font=('Microsoft YaHei UI', 13, 'bold'))
                label.pack(fill='x')
                subtitle = ctk.CTkLabel(text, text='', anchor='w', height=18, text_color=MUTED,
                                        font=('Microsoft YaHei UI', 11))
                subtitle.pack(fill='x')
                for widget in (identity, avatar, text, label, subtitle):
                    widget.bind('<Button-1>', lambda _, iid=iid: self._select(iid))
                self.subtitles[iid], self.avatars[iid] = subtitle, avatar
                labels.append(label)
                continue
            label = ctk.CTkLabel(row, text=value, anchor='w' if i == 0 else 'center',
                                font=('Microsoft YaHei UI', 13, 'bold' if i in (0, 3) else 'normal'),
                                corner_radius=6, height=28, wraplength=135 if i == 0 else 100 if i == 1 else 0)
            label.grid(row=0, column=i, sticky='ew', padx=(10, 4), pady=6)
            label.bind('<Button-1>', lambda _, iid=iid: self._select(iid))
            labels.append(label)
        self.rows[iid] = (row, labels)
        self.item(iid, values=values, tags=tags)

    def detail(self, iid, text):
        self.subtitles[iid].configure(text=text)

    def color(self, iid, color):
        self.rows[iid][1][3].configure(text_color=color)
        self.avatars[iid].configure(text_color=color, fg_color=blend('#181c22', color, .18))

    def item(self, iid, values=None, tags=()):
        if values is None:
            return {}
        row, labels = self.rows[iid]
        row.configure(fg_color='#1d2b40' if iid == self.selected else '#181c22',
                      border_color='#547db7' if iid == self.selected else '#252b34')
        offline = 'offline' in tags
        self.avatars[iid].configure(text=str(values[0])[:1])
        for i, (label, value) in enumerate(zip(labels, values)):
            label.configure(text=value)
            if i in (0, 2):
                label.configure(text_color='#73869d' if offline else ACCENT if i == 0 and 'local' in tags else FG)
        labels[1].configure(fg_color='#203656' if '使用中' in values[1] and not offline else 'transparent',
                            text_color=ACCENT if '使用中' in values[1] and not offline else MUTED)

    def delete(self, *ids):
        for iid in ids:
            if iid in self.rows:
                self.rows.pop(iid)[0].destroy()
                self.subtitles.pop(iid, None)
                self.avatars.pop(iid, None)
        if not self.rows:
            self.empty.pack(expand=True)


def number(value):
    return f'{value/1e6:.2f} M' if value >= 1e6 else f'{value/1000:.2f} k'


class App:
    def __init__(self, root, folder, config, database, demo=False, startup_enabled=True):
        self.root, self.folder, self.config, self.database = root, Path(folder), config, database
        self.engine = None
        self.demo = demo
        self.startup_enabled = startup_enabled and not demo
        self.hidden = False
        self.exited = False
        self.ui_actions = queue.Queue()
        self.tray = None
        self.results = queue.Queue()
        self.last_view = {}
        self.busy = False
        self.limit_pending = None
        self.last_runtime_auto = None
        self.cap_dialog = None
        self.update_running = False
        self.update_offer = None
        self.pair_request = 0
        self.pair_flow = dict(stage='saved' if config.get('link_enabled') or config.get('rendezvous_url') else 'idle')
        self.root.title('Codex 配额管家 · '+__version__)
        icon = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))/'assets'/'app.ico'
        if os.name == 'nt' and icon.exists():
            self.root.iconbitmap(str(icon))
        self.initial_size = (min(750, self.root.winfo_screenwidth()-60), min(570, self.root.winfo_screenheight()-80))
        self.root.geometry(f'{self.initial_size[0]}x{self.initial_size[1]}')
        self.root.minsize(730, 570)
        self.root.configure(fg_color=BG)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=FG, font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background=BG)
        style.configure('Card.TFrame', background=PANEL)
        style.configure('TLabel', background=BG, foreground=FG)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('TButton', background='#2b3b50', padding=(14, 9), borderwidth=0)
        style.map('TButton', background=[('active', '#3c526f')], foreground=[('disabled', '#62738a')])
        style.configure('Accent.TButton', background='#226e62', foreground='white')
        style.configure('TEntry', fieldbackground='#243247', foreground=FG, insertcolor=FG, padding=6)
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', background=PANEL, padding=(22, 12))
        style.map('TNotebook.Tab', background=[('selected', '#2b3b50')])
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=55, borderwidth=0, font=('Microsoft YaHei UI', 10))
        style.configure('Treeview.Heading', background=PANEL, foreground=MUTED, padding=(8, 14), font=('Microsoft YaHei UI', 9))
        style.map('Treeview', background=[('selected', '#345269')])
        style.configure('TCheckbutton', background=BG, foreground=FG)
        style.map('TCheckbutton', background=[('active', BG)])
        style.configure('Horizontal.TProgressbar', background=ACCENT, troughcolor='#2a3a4f', borderwidth=0)
        shell = ttk.Frame(root)
        shell.pack(fill='both', expand=True)
        top = ctk.CTkFrame(shell, fg_color=BG, height=40)
        top.pack(fill='x', padx=12, pady=(8, 8))
        self.brand_icon = ctk.CTkImage(light_image=icon_image(112), dark_image=icon_image(112), size=(28, 28))
        logo = ctk.CTkLabel(top, text='', width=28, height=28, image=self.brand_icon)
        logo.pack(side='left', padx=(3, 10))
        navigation = ctk.CTkFrame(top, fg_color='transparent')
        navigation.pack(side='left')
        button(top, text='×', width=28, height=27, command=self.close).pack(side='right', padx=(3, 0))
        button(top, text='−', width=28, height=27, command=self.minimize).pack(side='right')
        version = ctk.CTkLabel(top, text='v'+__version__, text_color=MUTED, font=('Segoe UI', 10))
        version.pack(side='right', padx=6)
        for surface in (top, logo, version):
            surface.bind('<Button-1>', self.drag_window)
            surface.bind('<B1-Motion>', self.move_window)
            surface.bind('<ButtonRelease-1>', self.end_move)
            surface.bind('<Double-Button-1>', lambda _: self.root.state(
                'normal' if self.root.state() == 'zoomed' else 'zoomed'))
        body = ttk.Frame(shell, padding=(14, 0, 14, 0))
        body.pack(fill='both', expand=True)
        self.page_title = tk.StringVar(value='概览')
        self.account = tk.StringVar(value='正在识别账号…')
        self.status = tk.StringVar(value='启动中…')
        footer = ttk.Frame(body, padding=(0, 3, 0, 5))
        footer.pack(side='bottom', fill='x')
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel', font=('Microsoft YaHei UI', 9),
                  wraplength=690).pack(anchor='w')
        pages = ttk.Frame(body)
        pages.pack(fill='both', expand=True)
        self.tabs = Navigation(navigation, self.page_title)
        self.overview = ctk.CTkFrame(pages, fg_color=BG, corner_radius=0)
        self.accounts_tab = ctk.CTkScrollableFrame(pages, fg_color=BG, corner_radius=0)
        self.pair_tab = ctk.CTkScrollableFrame(pages, fg_color=BG, corner_radius=0)
        self.settings = ctk.CTkScrollableFrame(pages, fg_color=BG, corner_radius=0)
        self.help_tab = ttk.Frame(pages, padding=(0, 6))
        for tab, title in ((self.overview, '概览'), (self.accounts_tab, '账号'), (self.pair_tab, '同步'),
                           (self.settings, '设置'), (self.help_tab, '说明')):
            self.tabs.add(tab, text=title)
        self.tabs.select(self.overview)
        self._overview()
        self._accounts()
        self._pairing()
        self._settings()
        self._help()
        grip = tk.Label(root, text='◢', bg=BG, fg='#3c424b', cursor='size_nw_se', font=('Segoe UI', 9))
        grip.place(relx=1, rely=1, anchor='se', width=14, height=14)
        grip.bind('<Button-1>', self.begin_resize)
        grip.bind('<B1-Motion>', self.resize_window)
        self.root.after(0, self.remove_titlebar)
        if not demo:
            try:
                self.tray = Tray(self.ui_actions.put, self.show, self.quit)
                self.tray.start()
            except Exception as e:
                self.tray = None
                messagebox.showerror('托盘启动失败', str(e), parent=self.root)
            self.restart()
        self.root.after(500, self.refresh)
        if self.startup_enabled and getattr(sys, 'frozen', False):
            self.root.after(15000, self.auto_update_tick)

    def remove_titlebar(self):
        if os.name == 'nt':
            width, height = (round(self.root._apply_window_scaling(n)) for n in self.initial_size)
            user = ctypes.windll.user32
            hwnd = user.GetParent(self.root.winfo_id())
            rect = (ctypes.c_long*4)()
            user.GetWindowRect(hwnd, ctypes.byref(rect))
            padding = rect[3]-rect[1]-self.root.winfo_height()
            # Tk retains the original nonclient height in its minimum tracking size.
            self.root.minsize(730, 570-round(self.root._reverse_window_scaling(padding)))
            style = user.GetWindowLongW(hwnd, -16)
            # Remove the nonclient strip while preserving taskbar/minimize behavior.
            user.SetWindowLongW(hwnd, -16, style & ~0x00C40000)
            user.SetWindowPos(hwnd, 0, 0, 0, width, height, 0x0036)
            corner, border = ctypes.c_int(2), ctypes.c_uint(0xfffffffe)
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)

    def drag_window(self, event):
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.moving = True
        self.move_job = None

    def move_window(self, event):
        x, y, left, top = self.drag_origin
        self.move_target = (left+event.x_root-x, top+event.y_root-y)
        if not self.move_job:
            self.move_job = self.root.after_idle(self.flush_move)

    def flush_move(self):
        self.move_job = None
        x, y = self.move_target
        if os.name == 'nt':
            user = ctypes.windll.user32
            user.SetWindowPos(user.GetParent(self.root.winfo_id()), 0, x, y, 0, 0, 0x0015)
        else:
            self.root.geometry(f'+{x}+{y}')

    def end_move(self, _=None):
        if self.move_job:
            self.root.after_cancel(self.move_job)
            self.flush_move()
        self.moving = False

    def begin_resize(self, event):
        self.resize_origin = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def resize_window(self, event):
        x, y, width, height = self.resize_origin
        minimum = tuple(round(self.root._apply_window_scaling(n)) for n in (730, 570))
        width, height = max(minimum[0], width+event.x_root-x), max(minimum[1], height+event.y_root-y)
        if os.name == 'nt':
            user = ctypes.windll.user32
            user.SetWindowPos(user.GetParent(self.root.winfo_id()), 0, 0, 0, width, height, 0x0016)
        else:
            self.root.geometry(f'{round(self.root._reverse_window_scaling(width))}x{round(self.root._reverse_window_scaling(height))}')

    def _overview(self):
        self.cards = {}
        self.quota_choice = tk.StringVar(value='个人额度 = 100%' if self.config.get('quota_display') == 'personal' else '账号总额度')
        cards = ctk.CTkFrame(self.overview, fg_color='transparent')
        cards.pack(fill='x', pady=(0, 7))
        for i, (key, title) in enumerate((('global', '官方周额度已用'), ('local', '同步状态'), ('reset', '下次官方刷新'))):
            cards.columnconfigure(i, weight=1, uniform='cards')
            card = ctk.CTkFrame(cards, fg_color=PANEL, corner_radius=12)
            card.grid(row=0, column=i, sticky='nsew', padx=(0, 8 if i < 2 else 0))
            ctk.CTkLabel(card, text=title, text_color=MUTED, height=20,
                         font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=12, pady=(4, 0))
            value = tk.StringVar(value='—')
            ctk.CTkLabel(card, textvariable=value, text_color=ACCENT if key == 'local' else FG,
                         height=27, font=('Microsoft YaHei UI', 20 if key == 'global' else 18, 'bold')).pack(anchor='w', padx=12, pady=(0, 3))
            self.cards[key] = value
            if key == 'local':
                self.sync_time = tk.StringVar(value='最近同步 —')
                self.last_sync = (None, None)
                ctk.CTkLabel(card, textvariable=self.sync_time, text_color=MUTED, height=12,
                             font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=12, pady=(0, 8))
            if key == 'global':
                self.meter = Meter(card)
                self.meter.configure(height=4)
                self.meter.pack(fill='x', padx=12, pady=(0, 7))
        self.account_tokens = tk.StringVar(value=budget_text(None))
        controls = ctk.CTkFrame(self.overview, fg_color='transparent')
        controls.pack(fill='x', pady=(0, 6))
        self.model_choice = tk.StringVar(value='全部模型')
        self.model_menu = ctk.CTkOptionMenu(controls, values=['全部模型'], variable=self.model_choice,
            command=lambda _: self.render_charts(), width=172, height=27, fg_color='#25282e',
            button_color='#303640', button_hover_color='#3e4755', font=('Segoe UI', 11))
        self.model_menu.pack(side='left')
        self.chart_choice = tk.StringVar(value='曲线')
        ctk.CTkSegmentedButton(controls, values=['曲线', '柱状'], variable=self.chart_choice,
            command=lambda _: self.render_charts(), height=27, selected_color='#294876',
            font=('Microsoft YaHei UI', 12)).pack(side='right', padx=(8, 0))
        self.device_choice = tk.StringVar(value='全部用户')
        self.device_filter = None
        self.device_choices = {'全部用户': None}
        self.device_menu = ctk.CTkOptionMenu(controls, values=['全部用户'], variable=self.device_choice,
            command=self.select_chart_device, width=144, height=27, fg_color='#25282e',
            button_color='#303640', button_hover_color='#3e4755', font=('Microsoft YaHei UI', 10))
        self.device_menu.pack(side='left', padx=8)
        self.window_choice = tk.StringVar(value='一天')
        ctk.CTkSegmentedButton(controls, values=['一天', '一周', '一月'], variable=self.window_choice,
            command=lambda _: self.render_charts(), height=27, selected_color='#294876',
            font=('Microsoft YaHei UI', 12)).pack(side='right')
        charts = ctk.CTkFrame(self.overview, fg_color='transparent')
        charts.pack(fill='x')
        charts.columnconfigure(0, weight=0, minsize=300)
        charts.columnconfigure(1, weight=1)
        pie_card = ctk.CTkFrame(charts, fg_color=PANEL, corner_radius=12)
        pie_card.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        line_card = ctk.CTkFrame(charts, fg_color=PANEL, corner_radius=12)
        line_card.grid(row=0, column=1, sticky='nsew')
        ctk.CTkLabel(pie_card, text='设备使用占比', text_color=FG, height=24,
                     font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w', padx=10, pady=(4, 0))
        self.trend_title = tk.StringVar(value='最近 24 小时')
        ctk.CTkLabel(line_card, textvariable=self.trend_title, height=24,
                     font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w', padx=10, pady=(4, 0))
        self.pie_chart = Chart(pie_card, 'pie', width=284, height=116)
        self.pie_chart.pack(fill='both', expand=True, padx=4, pady=(0, 5))
        self.pie_window = tk.StringVar(value='本周期')
        pie_footer = ctk.CTkFrame(pie_card, fg_color='transparent', height=30)
        pie_footer.place(in_=self.pie_chart, relx=0, rely=1, anchor='sw', relwidth=1)
        self.pie_detail = tk.StringVar()
        self.pie_chart.detail_callback = self.pie_detail.set
        ctk.CTkLabel(pie_footer, textvariable=self.pie_detail, text_color=MUTED,
                     font=('Segoe UI', 10), anchor='w', height=27).pack(side='left', padx=9)
        ctk.CTkOptionMenu(pie_footer,
            values=['本周期', '最近一小时', '历史累计', '一天使用', '七天使用', '一个月使用'],
            variable=self.pie_window, command=lambda _: self.render_charts(),
            width=130, height=27, fg_color='#25282e', button_color='#303640',
            button_hover_color='#3e4755', font=('Microsoft YaHei UI', 10)).pack(side='right', padx=4)
        self.line_chart = Chart(line_card, 'line', width=300, height=128)
        self.line_chart.pack(fill='both', expand=True, padx=4, pady=(0, 5))
        self.chart_note = tk.StringVar(value='占比基于同账号已同步日志；不代表官方一小时额度。')
        ctk.CTkLabel(line_card, textvariable=self.chart_note, text_color=MUTED, height=21,
                     font=('Microsoft YaHei UI', 9), anchor='w', wraplength=360).pack(fill='x', padx=10, pady=(0, 5))
        row = ctk.CTkFrame(self.overview, fg_color='transparent')
        row.pack(fill='x', pady=(1, 3))
        ctk.CTkLabel(row, text='同账号设备', font=('Microsoft YaHei UI', 12, 'bold')).pack(side='left')
        button(row, text='备注', width=53, height=26, command=self.edit_device_note).pack(side='right', padx=(5, 0))
        button(row, text='颜色', width=53, height=26, command=self.edit_device_color).pack(side='right', padx=(5, 0))
        button(row, text='移除', width=53, height=26, command=self.remove_device).pack(side='right', padx=(5, 0))
        columns = ('name', 'status', 'tokens', 'used')
        self.table = DeviceTable(self.overview, columns=columns)
        for col, title in zip(columns, ('用户 / 设备', '状态', '周期 Token', '估算已用')):
            self.table.heading(col, text=title)
        self.table.pack(fill='x')
        # Existing detailed diagnostics live on account/sync pages, not the dashboard.
        self.detail = tk.StringVar()
        self.note = tk.StringVar()
        self.observed = tk.StringVar()
        self.sync_progress_label = tk.StringVar(value='账本同步：等待进度')
        self.cycle_tokens = tk.StringVar()
        self.history_values = {}

    def render_charts(self):
        view = self.last_view
        ident = view.get('identity') or {}
        data = view.get('analytics') or {}
        if data.get('account') != ident.get('account'):
            data = {}
        choices = ['全部模型'] + data.get('models', [])
        self.model_menu.configure(values=choices)
        if self.model_choice.get() not in choices:
            self.model_choice.set('全部模型')
        model = None if self.model_choice.get() == '全部模型' else self.model_choice.get()
        metric = 'tokens'
        devices = {d['id']: d for d in (view.get('summary') or {}).get('devices', []) if not d.get('removed')}
        data = dict(data, windows={name: dict(window, rows=[r for r in window['rows'] if r['device'] in devices])
                                  for name, window in data.get('windows', {}).items()})
        pie_period = {'本周期': 'cycle', '最近一小时': 'hour', '历史累计': 'total', '一天使用': 'day',
                      '七天使用': 'week', '一个月使用': 'month'}[self.pie_window.get()]
        hour = chart_data(data, pie_period, model, metric)
        if pie_period == 'cycle' and model is None:
            # Use the same snapshot as the device rows, avoiding the chart cache delay.
            hour['devices'] = {key: d.get('tokens', 0) for key, d in devices.items()}
        window = {'一天': 'day', '一周': 'week', '一月': 'month'}[self.window_choice.get()]
        aliases = self.config.get('device_notes', {}).get(ident.get('account'), {})
        choices = {'全部用户': None}
        for device in sorted(devices):
            d = devices.get(device, {})
            label = aliases.get(device) or d.get('name', device[:10])
            if device == self.config['device_id']:
                label += ' · 本机'
            elif d.get('removed'):
                label += ' · 历史'
            if label in choices:
                label += ' · '+device[:6]
            choices[label] = device
        if self.device_filter not in choices.values():
            self.device_filter = None
        self.device_choices = choices
        selected = next(label for label, device in choices.items() if device == self.device_filter)
        if self.device_choice.get() != selected:
            self.device_choice.set(selected)
        self.device_menu.configure(values=list(choices))
        trend = chart_data(data, window, model, metric, self.device_filter)
        values, labels = [], []
        order = sorted({id for id, d in devices.items() if not d.get('removed')} | set(hour['devices']))
        pairs = [(device, hour['devices'].get(device, 0)) for device in order]
        for device, value in pairs[:3]:
            d = devices.get(device, {})
            labels.append(aliases.get(device) or d.get('name', device[:10]))
            values.append(value)
        if len(pairs) > 3:
            labels.append('其他设备')
            values.append(sum(value for _, value in pairs[3:]))
        unit = 'Token' if trend['metric'] == 'tokens' else '权重'
        colors = [self.device_color(device) for device, _ in pairs[:3]]
        if len(pairs) > 3:
            colors.append('#89909c')
        self.pie_chart.set_data(values, labels, colors=colors)
        kind = 'bar' if self.chart_choice.get() == '柱状' else 'line'
        if self.line_chart.kind != kind:
            self.line_chart.kind = kind
            self.line_chart.values = []
            self.line_chart.key = None
        self.line_chart.set_data(trend['points'], start=trend['start'], step=trend['step'], unit=unit,
                                 colors=[self.device_color(self.device_filter)] if self.device_filter else [ACCENT])
        period = {'day': '24 小时', 'week': '7 天', 'month': '30 天'}[window]
        self.trend_title.set(f"最近 {period} · {compact(trend['total'])} {unit}")
        notes = []
        if metric == 'weight':
            if hour['unknown']:
                notes.append('饼图时段有未知模型，按 Token')
            if trend['unknown']:
                notes.append('曲线含未知模型，按 Token')
        self.chart_note.set('；'.join(notes) if notes else
            '模型加权用量 · 悬停查看数值' if metric == 'weight' else '原始 Token 用量 · 悬停查看数值')

    def select_chart_device(self, label):
        self.device_filter = self.device_choices[label]
        self.render_charts()

    def select_quota_display(self, value):
        self.config['quota_display'] = 'personal' if value == '个人额度 = 100%' else 'account'
        if not self.demo:
            save_config(self.folder/'settings.json', self.config)
        self.render(self.last_view)

    def edit_device_note(self):
        selected = self.table.selection()
        if not selected:
            self.status.set('请先选择一台设备，再点击备注。')
            return
        account = (self.last_view.get('identity') or {}).get('account')
        if not account:
            return
        device = selected[0]
        win = ctk.CTkToplevel(self.root)
        win.title('设备备注')
        win.geometry('350x185')
        win.resizable(False, False)
        win.transient(self.root)
        win.configure(fg_color=BG)
        ctk.CTkLabel(win, text='仅在本机显示；留空恢复设备原名。', text_color=MUTED,
                     font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=18, pady=(18, 8))
        entry = ctk.CTkEntry(win, height=33)
        entry.pack(fill='x', padx=18)
        entry.insert(0, self.config.get('device_notes', {}).get(account, {}).get(device, ''))
        def save():
            self.save_device_note(account, device, entry.get())
            win.destroy()
            self.render(self.last_view)
        button(win, text='保存备注', style='Accent.TButton', command=save).pack(anchor='e', padx=18, pady=16)
        win.bind('<Return>', lambda _: save())
        win.bind('<Escape>', lambda _: win.destroy())

    def device_color(self, device):
        account = (self.last_view.get('identity') or {}).get('account')
        color = self.config.get('device_colors', {}).get(account, {}).get(device)
        if color in USER_COLORS.values():
            return color
        ids = sorted(set(self.device_choices.values())-{None})
        return COLORS[(ids.index(device) if device in ids else 0) % len(COLORS)]

    def edit_device_color(self):
        selected = self.table.selection()
        if not selected:
            self.status.set('请先选择一台设备，再点击颜色。')
            return
        account = (self.last_view.get('identity') or {}).get('account')
        if not account:
            return
        win = ctk.CTkToplevel(self.root)
        win.title('用户颜色')
        win.geometry('390x180')
        win.resizable(False, False)
        win.transient(self.root)
        win.configure(fg_color=BG)
        ctk.CTkLabel(win, text='选择预设颜色', font=('Microsoft YaHei UI', 14, 'bold')).pack(pady=(14, 8))
        palette = ctk.CTkFrame(win, fg_color='transparent')
        palette.pack(padx=12)
        def choose(color):
            self.save_device_color(account, selected[0], color)
            self.render(self.last_view)
            win.destroy()
        for i, (name, color) in enumerate(USER_COLORS.items()):
            button(palette, text='● '+name, width=80, height=36, fg_color=blend(PANEL, color, .18),
                   text_color=color, hover_color=blend(PANEL, color, .35),
                   command=lambda c=color: choose(c)).grid(row=i//4, column=i%4, padx=4, pady=4)
        win.bind('<Escape>', lambda _: win.destroy())

    def save_device_color(self, account, device, color):
        self.config.setdefault('device_colors', {}).setdefault(account, {})[device] = color
        if not self.demo:
            save_config(self.folder/'settings.json', self.config)

    def save_device_note(self, account, device, text):
        notes = self.config.setdefault('device_notes', {}).setdefault(account, {})
        value = text.strip()[:40]
        if value:
            notes[device] = value
        else:
            notes.pop(device, None)
        if not self.demo:
            save_config(self.folder/'settings.json', self.config)

    def _pairing(self):
        tools = ttk.Frame(self.pair_tab)
        tools.pack(fill='x', pady=(0, 10))
        button(tools, text='刷新连接', command=lambda: self.engine and self.engine.wakeup.set()).pack(side='left')
        button(tools, text='导出同步诊断', command=self.export_sync_diagnostics).pack(side='left', padx=8)
        ttk.Label(self.pair_tab, textvariable=self.sync_progress_label, style='Muted.TLabel',
                  wraplength=660).pack(anchor='w', pady=(0, 10))
        ttk.Label(self.pair_tab, text='让设备彼此连接', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(self.pair_tab, text='一个匹配码，连接你的工作站、笔记本和家用电脑。配对信息持久保存，重启自动重连。',
                  style='Muted.TLabel', wraplength=660).pack(anchor='w', pady=(8, 20))
        cards = ttk.Frame(self.pair_tab)
        cards.pack(fill='x')
        for i, (num, title, desc, action, command) in enumerate([
            ('01', '从这台设备发起', '完成内嵌 Tailscale 授权，再生成设备组匹配码。', '生成匹配码（发送）', self.send_tailscale),
            ('02', '加入已有设备组', '粘贴另一台设备的匹配码，保存配对并开始同步。', '输入匹配码（接收）', self.receive_pair)]):
            cards.columnconfigure(i, weight=1, uniform='pair')
            card = ctk.CTkFrame(cards, fg_color=PANEL, corner_radius=14, border_color='#2a3b4f', border_width=1)
            card.grid(row=0, column=i, sticky='nsew', padx=(0, 14) if i == 0 else 0)
            ctk.CTkLabel(card, text=num, text_color=ACCENT, font=('Segoe UI', 24, 'bold')).pack(anchor='w', padx=22, pady=(18, 2))
            ctk.CTkLabel(card, text=title, text_color=FG, font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w', padx=22)
            ctk.CTkLabel(card, text=desc, text_color=MUTED, font=('Microsoft YaHei UI', 11), wraplength=275,
                         justify='left').pack(anchor='w', padx=22, pady=(8, 18))
            button(card, text=action, style='Accent.TButton' if i == 0 else None, command=command).pack(fill='x', padx=22, pady=(0, 20))
        self.pair_panel = PairPanel(self.pair_tab, self.retry_pair)
        self.pair_panel.pack(fill='x', pady=(18, 6))
        self.pair_panel.render(self.pair_flow, {})
        self.mesh_label = tk.StringVar(value='连接诊断：尚未启动')
        self.tailnet_label = tk.StringVar(value='内嵌节点所属网络：尚未连接')
        ctk.CTkLabel(self.pair_tab, textvariable=self.tailnet_label, text_color=FG,
                     font=('Microsoft YaHei UI', 12), wraplength=660, justify='left').pack(anchor='w', pady=(12, 0))
        ctk.CTkLabel(self.pair_tab, textvariable=self.mesh_label, text_color=ACCENT, font=('Microsoft YaHei UI', 12)).pack(anchor='w', pady=(16, 6))
        button(self.pair_tab, text='授权登录 Tailscale（浏览器）', command=self.login_tailscale).pack(anchor='w', pady=8)
        button(self.pair_tab, text='换账号 / 切换 Tailscale 网络', command=self.switch_tailscale_network).pack(anchor='w', pady=4)
        self.advanced = ttk.Frame(self.pair_tab)
        self.url = self.entry(self.advanced, 'WSS 服务地址', self.config['rendezvous_url'])
        self.token = self.entry(self.advanced, '服务访问密钥', self.config['relay_token'], show='•')
        self.stun = self.entry(self.advanced, 'STUN 地址（可留空）', self.config['stun_url'])
        self.force_relay = tk.BooleanVar(value=self.config['force_relay'])
        ctk.CTkCheckBox(self.advanced, text='仅使用加密中转（代理兼容性排查）', variable=self.force_relay,
                       fg_color='#309d82', hover_color='#24745f', font=('Microsoft YaHei UI', 12),
                       checkbox_width=18, checkbox_height=18).pack(anchor='w', pady=8)
        button(self.advanced, text='保存连接设置', command=self.save_connection).pack(anchor='w', pady=8)
        ttk.Label(self.pair_tab, text='内嵌 Tailscale，无需另装客户端。两端首次需授权加入同一 Tailscale 网络，再交换新版匹配码。\n'
                  '优先直连，失败由 Tailscale DERP 中转；不再使用 Syncthing 公共中转。路径以近期探测为准。\n'
                  '旧设备组发起方刷新匹配码可保留账本；双方须升级并添加同一 Codex 账号。',
                  style='Muted.TLabel', wraplength=660, justify='left').pack(side='bottom', anchor='w', pady=12)

    def _accounts(self):
        for variable in (self.account_tokens, self.cycle_tokens, self.detail, self.observed, self.note):
            ttk.Label(self.accounts_tab, textvariable=variable, style='Muted.TLabel',
                      wraplength=660).pack(anchor='w', pady=(0, 6))
        card = ctk.CTkFrame(self.accounts_tab, fg_color=PANEL, corner_radius=14)
        card.pack(fill='x', pady=(0, 20))
        ctk.CTkLabel(card, text='扫描登录账号 · 独立计量', text_color=FG,
                     font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w', padx=22, pady=(20, 12))
        self.detected_account = tk.StringVar(value='等待读取当前登录账号…')
        ctk.CTkLabel(card, textvariable=self.detected_account, text_color=ACCENT,
                     font=('Microsoft YaHei UI', 13), wraplength=640, justify='left').pack(anchor='w', padx=22, pady=(0, 15))
        row = ctk.CTkFrame(card, fg_color='transparent')
        row.pack(fill='x', padx=22, pady=(0, 22))
        button(row, text='扫描当前登录', command=self.scan_account).pack(side='left')
        button(row, text='添加当前账号到统计', style='Accent.TButton', command=self.add_account).pack(side='left', padx=10)
        ttk.Label(self.accounts_tab, text='已添加的账号', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w', pady=8)
        self.account_list = tk.Listbox(self.accounts_tab, height=7, bg=PANEL, fg=FG,
            selectbackground='#345269', relief='flat', font=('Microsoft YaHei UI', 12),
            highlightthickness=0, exportselection=False)
        self.account_list.pack(fill='x', pady=(4, 12))
        actions = ttk.Frame(self.accounts_tab)
        actions.pack(fill='x')
        button(actions, text='查看选中账号账本', command=self.account_history).pack(side='left')
        button(actions, text='停止统计选中账号', command=self.remove_account).pack(side='left', padx=10)
        ttk.Label(self.accounts_tab, text='扫描只识别当前账号，点击“添加”后才开始统计。每个账号分别保存 Token、设备配额与周刷新周期。\n'
                  '中转 API、未添加账号不查询订阅额度，不参与账号组同步或触发订阅限额。\n'
                  '账号名单仅保存在本机；配对不会自动添加账号。切换边界和无法确认归属的会话不强行计入。',
                  style='Muted.TLabel', justify='left', wraplength=660).pack(anchor='w', pady=20)
        self.refresh_account_list()

    def refresh_account_list(self):
        self.account_ids = list(self.config.get('tracked_accounts', {}))
        self.account_list.delete(0, 'end')
        for account in self.account_ids:
            item = self.config['tracked_accounts'][account]
            self.account_list.insert('end', f"  {item['label']}   ·   {account[:12]}")

    def show_detected_account(self, ident):
        tracked = ident.get('account') in self.config.get('tracked_accounts', {}) and ident.get('mode') == 'account'
        self.detected_account.set(ident.get('label', '未识别')+'\n'+('已添加 · 独立统计' if tracked else '未纳入订阅统计'))

    def scan_account(self):
        self.background(lambda: identity(self.config['codex_home']), self.show_detected_account)

    def add_account(self):
        try:
            if self.busy:
                raise ValueError('后台操作尚未结束，请稍后添加。')
            ident = identity(self.config['codex_home'])
            enroll(self.config, ident)
            self.persist_restart()
            self.refresh_account_list()
            self.show_detected_account(ident)
        except Exception as e:
            messagebox.showerror('添加账号', str(e), parent=self.root)

    def remove_account(self):
        if self.busy or not self.account_list.curselection():
            return
        account = self.account_ids[self.account_list.curselection()[0]]
        if messagebox.askokcancel('停止统计', '停止采集和同步此账号；已有账本文件会保留。', parent=self.root):
            self.config['tracked_accounts'].pop(account)
            self.persist_restart()
            self.refresh_account_list()

    def account_history(self):
        if not self.engine or not self.account_list.curselection():
            return
        account = self.account_ids[self.account_list.curselection()[0]]
        label = self.config['tracked_accounts'][account]['label']
        def show(data):
            summary, history = data
            win = ctk.CTkToplevel(self.root)
            win.title('账号账本 · '+label)
            win.geometry('820x560')
            win.configure(fg_color=BG)
            controls = ctk.CTkFrame(win, fg_color='transparent')
            controls.pack(fill='x', padx=20, pady=(16, 0))
            text = ctk.CTkTextbox(win, fg_color=PANEL, corner_radius=12, font=('Microsoft YaHei UI', 13))
            text.pack(fill='both', expand=True, padx=20, pady=20)
            epoch = summary.get('epoch')
            lines = [label, '此为该账号最近保存的账本快照，不会查询其他账号的凭据。', '']
            if epoch:
                lines += [f"账号周额度已用：{epoch['used']:g}%   基线：{epoch['baseline']:g}%",
                          '刷新时间：'+datetime.fromtimestamp(epoch['reset_at']).strftime('%Y-%m-%d %H:%M'), '']
                for d in summary['devices']:
                    lines.append(f"{d['name']}   Token {number(d['tokens'])}   估算 {d['estimated']:.2f}% / 配额 {d['cap']:g}%")
                lines += ['', '未归属额度核对（周期权重或记录不完整）：']
                names = {d['id']: d['name'] for d in summary['devices']}
                for gap in summary.get('attribution_gaps', []):
                    span = ' → '.join(datetime.fromtimestamp(gap[k]).strftime('%m-%d %H:%M:%S') for k in ('start', 'end'))
                    reason = ('已采集设备：'+', '.join(names.get(d, d) for d in gap['devices'])+
                              '；未知模型权重：'+', '.join(gap['unknown_models'])
                              if gap['reason'] == 'unknown_weight' else '没有对应 Token；等待来源设备补记及同步')
                    lines.append(f"{span}   {gap['delta']:g}%   {reason}")
            else:
                lines.append('还没有此账号的有效周额度快照。')
            lines += ['', '本机今日 / 自然周 / 本月：'+' / '.join(number(history['current'][k]) for k in ('day', 'week', 'month'))]
            def render_history(unit):
                text.configure(state='normal')
                text.delete('1.0', 'end')
                text.insert('1.0', '\n'.join(lines+['', '本机历史 Token（不随额度重置）：']+
                    [f'{date}    {number(tokens)}' for date, tokens in sorted(history[unit].items(), reverse=True)]))
                text.configure(state='disabled')
            for unit, title in [('day', '按日'), ('week', '按自然周'), ('month', '按月')]:
                button(controls, text=title, command=lambda unit=unit: render_history(unit)).pack(side='left', padx=(0, 8))
            render_history('day')
        self.background(lambda: (self.engine.ledger.summary(account), self.engine.ledger.history(account, self.config['device_id'])), show)

    def toggle_advanced(self):
        if self.advanced.winfo_manager():
            self.advanced.pack_forget()
        else:
            self.advanced.pack(fill='x')

    def entry(self, parent, title, value, show=None):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=title, width=23).pack(side='left')
        var = tk.StringVar(value=str(value))
        widget = ctk.CTkEntry(row, textvariable=var, show=show or '', height=36, corner_radius=8, fg_color=PANEL, border_color='#2b3d54', text_color=FG, font=('Microsoft YaHei UI', 12))
        widget.pack(side='left', fill='x', expand=True)
        return var

    def _settings(self):
        quota_row = ctk.CTkFrame(self.settings, fg_color=PANEL, corner_radius=12)
        quota_row.pack(fill='x', pady=(0, 12))
        ctk.CTkLabel(quota_row, text='设备配额', font=('Microsoft YaHei UI', 13, 'bold')).pack(side='left', padx=14, pady=12)
        self.quota_settings = tk.StringVar(value='当前账号本机配额：—')
        ctk.CTkLabel(quota_row, textvariable=self.quota_settings, text_color=MUTED,
                     font=('Microsoft YaHei UI', 11)).pack(side='left', padx=8)
        button(quota_row, text='调整配额', command=self.change_cap).pack(side='right', padx=12, pady=10)
        ctk.CTkOptionMenu(self.settings, values=['账号总额度', '个人额度 = 100%'], variable=self.quota_choice,
            command=self.select_quota_display, width=195, height=30,
            font=('Microsoft YaHei UI', 12)).pack(anchor='w', pady=(0, 12))
        self.limit_panel = LimitPanel(self.settings, self.limit_action)
        self.limit_panel.pack(fill='x', pady=(0, 12))
        self.limit_panel.render(limit_presentation({}, self.config.get('tracked_accounts', {})))
        update_card = ctk.CTkFrame(self.settings, fg_color=PANEL, corner_radius=14,
                                  border_width=1, border_color='#295447')
        update_card.pack(fill='x', pady=(0, 16))
        ctk.CTkLabel(update_card, text='软件更新  ·  v'+__version__, font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w', padx=18, pady=(14, 8))
        self.auto_update = tk.BooleanVar(value=self.config.get('auto_update', True))
        ctk.CTkCheckBox(update_card, text='自动检测并安装正式版更新（默认开启）', variable=self.auto_update,
                       command=self.save_update_setting, font=('Microsoft YaHei UI', 12)).pack(anchor='w', padx=18, pady=(0, 10))
        update_row = ctk.CTkFrame(update_card, fg_color='transparent')
        update_row.pack(fill='x', padx=18, pady=(0, 12))
        self.update_status = tk.StringVar(value='GitHub 官方仓库 · 签名校验 · 原地更新后自动重启，保留账号与账本')
        ctk.CTkLabel(update_row, textvariable=self.update_status, text_color=MUTED, wraplength=440,
                     justify='left', font=('Microsoft YaHei UI', 11)).pack(side='left')
        self.update_button = button(update_row, text='检测更新', command=lambda: self.check_update(True))
        self.update_button.pack(side='right')
        self.limit_setup_note = tk.StringVar(value='')
        ctk.CTkLabel(self.settings, textvariable=self.limit_setup_note, text_color='#f2b46f', wraplength=660,
                     justify='left', font=('Microsoft YaHei UI', 12)).pack(anchor='w', pady=(0, 6))
        self.autostart = tk.BooleanVar(value=self.config.get('autostart', True))
        ctk.CTkCheckBox(self.settings, text='Windows 登录后自动启动（默认开启）', variable=self.autostart,
                       font=('Microsoft YaHei UI', 12)).pack(anchor='w', pady=(4, 12))
        ttk.Label(self.settings, text='点击 X 隐藏到托盘；后台约每 5 秒检查，前台及受限时约每 2 秒检查。彻底退出请使用托盘菜单。',
                  style='Muted.TLabel', wraplength=660).pack(anchor='w', pady=(0, 10))
        self.device_name = self.entry(self.settings, '本机设备名称', self.config['name'])
        self.home = self.entry(self.settings, 'Codex 数据目录', self.config['codex_home'])
        self.cap = self.entry(self.settings, '新账号默认配额（%）', self.config['quota'])
        self.multiplier = self.entry(self.settings, '本机权重校准系数', self.config['multiplier'])
        self.interval = self.entry(self.settings, '额度查询间隔（秒）', self.config['interval'])
        self.auto_block = tk.BooleanVar(value=self.config['auto_block'])
        ttk.Checkbutton(self.settings, text='达到设备配额后自动限制 Codex（需管理员权限）', variable=self.auto_block).pack(anchor='w', pady=12)
        ttk.Label(self.settings, text='限制方式：选定程序的出站防火墙规则 + 暂停选定 Codex 进程（兼容本机 sing-box 代理）。\n'
                  '检测到当前配置切换成 API 或其他账号后，先解除原账号的限制。不清空原账号账本。\n'
                  '防火墙按 EXE 生效，不能隔离同一 EXE 内同时运行的多账号／API 会话；不要用于混合并行模式。\n'
                  '不会阻断 sing-box 本身或整台电脑。远端已接收的请求可能仍继续计费。',
                  style='Muted.TLabel', wraplength=660).pack(anchor='w', pady=(0, 10))
        self.program_note = tk.StringVar(value='启动时自动查找 Codex 后台 EXE；不会选择 node.exe、Python 或桌面 GUI 外壳。')
        ttk.Label(self.settings, textvariable=self.program_note, style='Muted.TLabel', wraplength=660).pack(anchor='w')
        self.paths = tk.Listbox(self.settings, height=4, bg=PANEL, fg=FG, selectbackground='#345269', relief='flat',
                                font=('Consolas', 9), highlightthickness=0)
        self.paths.pack(fill='x', pady=8)
        for p in self.config['program_paths']:
            self.paths.insert('end', p)
        row = ttk.Frame(self.settings)
        row.pack(fill='x')
        button(row, text='自动查找 Codex', command=self.discover).pack(side='left')
        button(row, text='添加 EXE', command=self.add_path).pack(side='left', padx=8)
        button(row, text='移除选中', command=lambda: self.paths.delete('anchor')).pack(side='left')
        button(row, text='保存监测设置', style='Accent.TButton', command=self.save_settings).pack(side='right')
        ttk.Label(self.settings, text='权重系数只影响新采集记录。快速模式优先读取 Codex 配置；临时会话参数未必可识别。\n'
                  '当前权限：'+('管理员' if is_admin() else '普通用户；可右键 EXE 选择“以管理员身份运行”'),
                  style='Muted.TLabel', wraplength=660).pack(anchor='w', pady=16)

    def save_update_setting(self):
        self.config['auto_update'] = self.auto_update.get()
        save_config(self.folder/'settings.json', self.config)

    def auto_update_tick(self):
        if self.exited:
            return
        if self.config.get('auto_update', True):
            self.check_update(False)
        self.root.after(3600000, self.auto_update_tick)

    def check_update(self, manual=False):
        if self.update_running or self.busy:
            self.update_status.set('当前操作进行中，请稍后再试')
            return
        if self.demo or not getattr(sys, 'frozen', False):
            self.update_status.set('演示／源码模式不安装更新，请使用发布版 EXE')
            return
        self.update_running = True
        self.update_button.configure(state='disabled')
        self.update_status.set('正在连接 GitHub，检测正式版更新…')
        def work():
            from . import updater
            try:
                manifest = updater.check(__version__)
                if not manifest:
                    result = (None, None, '当前已是最新正式版 v'+__version__)
                else:
                    previous = self.folder/'updates'/'result.json'
                    failed = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else {}
                    if not manual and failed.get('ok') is False and failed.get('version') == manifest['payload']['version']:
                        result = (None, None, '上次更新未完成，当前版本仍可用；点击检测更新重试')
                    else:
                        staged = updater.download(manifest, self.folder/'updates')
                        result = (manifest, staged, '新版已下载并通过签名校验')
            except Exception as e:
                result = (None, None, '更新检测／下载失败，保留当前版本 · '+str(e)[:160])
            self.ui_actions.put(lambda: self.update_downloaded(result, manual))
        threading.Thread(target=work, daemon=True).start()

    def update_downloaded(self, result, manual):
        self.update_running = False
        self.update_button.configure(state='normal')
        manifest, staged, status = result
        self.update_status.set(status)
        if not manifest or (not manual and not self.config.get('auto_update', True)):
            return
        self.update_offer = (manifest, staged)
        self.install_update()

    def install_update(self):
        if self.busy or (self.engine and self.engine.blocked) or self.database.get('paused_processes'):
            self.update_status.set('新版已就绪 · 正在限额或处理操作，解除后自动安装，不中断限额保护')
            self.root.after(30000, self.retry_update)
            return
        from . import updater
        manifest, staged = self.update_offer
        self.update_status.set('正在安全退出后台并更新，随后自动重启…')
        def work():
            try:
                if self.engine:
                    self.engine.close()
                    if self.engine.blocked or self.database.get('paused_processes'):
                        self.engine.stop_event.clear()
                        self.engine.start()
                        return False
                updater.launch_helper(manifest, staged, self.folder, self.hidden)
                return True
            except Exception:
                if self.engine and self.engine.stop_event.is_set():
                    self.engine.stop_event.clear()
                    self.engine.start()
                raise
        def done(ready):
            if not ready:
                self.update_status.set('已延后更新，保持当前账号限额保护')
                self.root.after(30000, self.retry_update)
                return
            if self.tray:
                self.tray.stop()
            self.exited = True
            self.root.destroy()
        self.background(work, done)

    def retry_update(self):
        if self.update_offer and self.config.get('auto_update', True) and not self.exited:
            self.install_update()

    def _help(self):
        button(self.help_tab, text='查看第三方许可', command=lambda: os.startfile(str(
            Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))/'licenses'))).pack(anchor='w', pady=(0, 12))
        body = (
            '什么是“33%”\n'
            '指整个账号完整周额度的 33 个百分点，不是当前剩余额度的 33%。账号余额不足时，不能凭本机配额增加官方额度。\n\n'
            '怎样估算\n'
            '按模型区分非缓存输入、缓存输入与输出 Token，以公开计价比例作为初始相对权重。'
            '官方已用减去监测前基线，按整个额度周期的设备加权 Token 比例重新分摊。新增或补传 Token 会重算全部比例，无需等官方百分比再次上涨；这是一种组内分摊估算，不是官方设备账单。\n\n'
            '哪些用量不强行归属\n'
            '首次监测前的消费单列为基线；整个周期没有对应 Token 或多设备混用未知权重模型时，可分摊额度暂列“未归属”。'
            '只有一台设备有 Token 时，未知模型不再阻止设备归属。账号账本提供未归属周期及原因。'
            'Spark 使用独立额度池，不混入主池。离线补传会修正历史估算。所有使用设备都应运行本工具。\n\n'
            '自动补记旧日志\n'
            '升级后自动重读本机已添加账号的历史时段，保留独立数值证据，不改动原始日志及旧采集游标。'
            '日志周额度快照与已知账号的历史快照相互匹配且没有账号冲突时，补记漏采 Token 并同步。'
            '同一会话的连续区间可继承已确认 Token 的账号，首尾均确认时标记区间推断；'
            'Provider、账号、额度指纹冲突或账号切换拒绝记录会截断继承。推断记录不再充当新的证据向外扩散。'
            '新版本还会只读索引本机 process_uuid、thread_id 与 turn_id，把连续运行区间绑定到已确认账号；'
            '重启和 account/updated 通知划分运行区间，旧请求尾部保留原归属。运行证据未索引完成或存在冲突时不扩散推断。'
            '这是额度快照相关性推断，不是逐请求账号证明；不会套用当前登录账号或今日快速模式回填旧记录。'
            '缺少证据的记录保留待核对，不代表已确认是 API 用量；导出同步诊断可查看证据缺失或冲突原因。两端都需升级以使用相同分摊规则，本机不会代造远端 Token。\n\n'
            '并发数的含义\n'
            '“活动会话”依据本机日志里的开始、完成与最近活动事件判断，并非服务端并发推理数。'
            '120 秒没有日志更新的未结束会话标记“待确认”；等待审批、断线或日志缺失会影响判断。\n\n'
            '限制和刷新\n'
            '默认只提醒。自动限制将至少 120 秒前的额度增量按当前周期权重分摊，因此会延迟且可能超额；补传记录仍会修正比例。'
            '确认官方周期变化／提前重置后自动恢复；仅电脑时间到点不会清零。'
            '接口失败、登录过期或数据回退未确认时，不冒充刷新成功；可随时手动恢复。\n\n'
            '安全与恢复\n'
            '只读 Codex 登录文件，不修改登录、不兑换重置。匹配双方使用 AES-GCM 加密数据；'
            '中转仅转发密文，但能看到连接 IP、时序与流量大小。所有持有匹配码的设备彼此信任。'
            '程序异常退出后可运行“恢复 Codex 网络.exe”。退出主程序时会尝试恢复由本工具暂停的进程和规则。'
            '\n\n连接组件与源码\n自动连接使用 Syncthing v2.1.3-cqg1（MPL-2.0，代理兼容补丁）。'
            '完整修改文件、上游源码链接及构建方式：\nhttps://github.com/visaokc/CodexQuotaGuard/tree/main/vendor/syncthing'
        )
        text = tk.Text(self.help_tab, wrap='word', bg=BG, fg=FG, relief='flat', font=('Microsoft YaHei UI', 11),
                       padx=0, pady=0, spacing1=3, spacing3=5, highlightthickness=0)
        text.pack(fill='both', expand=True)
        text.insert('1.0', body)
        text.configure(state='disabled')

    def background(self, func, done=None):
        if self.busy:
            messagebox.showinfo('请稍候', '上一项操作尚未结束。', parent=self.root)
            return
        self.busy = True
        def worker():
            try:
                value = func()
                self.results.put((done, value, None))
            except Exception as e:
                self.results.put((None, None, str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def restart(self):
        def work():
            if self.engine:
                self.engine.close()
            found = []
            try:
                found = discover_programs()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass  # Detection failure must not stop account monitoring.
            if not self.config['program_paths']:
                self.config['program_paths'] = found
            self.engine = Engine(self.database, self.config)
            self.engine.background_mode = self.hidden
            self.engine.start()
            if self.startup_enabled:
                startup.apply(self.config.get('autostart', True), self.folder, elevated=self.config['auto_block'])
            return found
        def discovered(found):
            existing = set(self.paths.get(0, 'end'))
            for path in self.config['program_paths']:
                if path not in existing:
                    self.paths.insert('end', path)
            self.program_note.set(f'自动检测到 {len(found)} 个 Codex 后台 EXE；现有手动选择保持不变。' if found
                                  else '未发现 Codex 后台 EXE。请启动一个 Codex 会话后点击“自动查找 Codex”。')
        self.background(work, discovered)

    def persist_restart(self):
        save_config(self.folder/'settings.json', self.config)
        if not self.demo:
            self.restart()

    def save_connection(self):
        from .pairing import validate_url
        try:
            if self.busy:
                raise ValueError('上一项后台操作尚未结束，请稍后保存。')
            url = self.url.get().strip()
            if url:
                validate_url(url)
                if len(self.token.get().strip()) < 24:
                    raise ValueError('服务访问密钥至少 24 个字符')
            self.config.update(rendezvous_url=url, relay_token=self.token.get().strip(),
                               stun_url=self.stun.get().strip(), force_relay=self.force_relay.get(), fingerprint='',
                               link_enabled=not bool(url))
            self.persist_restart()
        except Exception as e:
            messagebox.showerror('连接设置', str(e), parent=self.root)

    def send_tailscale(self):
        if self.busy:
            return
        if not self.demo:
            ident = identity(self.config['codex_home'])
            if ident.get('mode') != 'account' or ident.get('account') not in self.config.get('tracked_accounts', {}):
                messagebox.showinfo('先添加账号', '请先添加当前 Codex 订阅账号，再启动内嵌 Tailscale。', parent=self.root)
                return
        self.pair_request += 1
        request = self.pair_request
        self.config.update(tailscale_enabled=True, link_enabled=True, rendezvous_url='', pair_role='sender')
        self.pair_flow = dict(stage='authorizing')
        self.pair_panel.set_code('', '首次点击“授权登录 Tailscale”；登录后匹配码自动生成。')
        if self.demo:
            self.config['tailscale_ip'] = '100.64.0.1'
            self.show_pair_code(create_code(self.config))
            return
        from .tsnet_mesh import TailscaleMesh
        if self.engine and isinstance(self.engine.mesh, TailscaleMesh):
            save_config(self.folder/'settings.json', self.config)
        else:
            self.persist_restart()
        self.root.after(500, lambda: self.wait_tailscale(request))

    def wait_tailscale(self, request):
        if self.exited or request != self.pair_request:
            return
        from .tsnet_node import tail_ip
        mesh = self.engine.mesh if self.engine else None
        state = mesh.connection_state() if mesh else {}
        ips = [ip for ip in state.get('ips', []) if tail_ip(ip)]
        if state.get('ready') and ips:
            self.config['tailscale_ip'] = ips[0]
            save_config(self.folder/'settings.json', self.config)
            self.show_pair_code(create_code(self.config))
        else:
            self.root.after(1000, lambda: self.wait_tailscale(request))

    def remove_device(self):
        selected = self.table.selection()
        engine = self.engine
        if not selected or not engine:
            return
        peer = selected[0]
        if peer == self.config['device_id']:
            messagebox.showinfo('移除设备', '不能在此移除本机，请选择其他设备。', parent=self.root)
            return
        with engine.view_lock:
            summary = engine.view.get('summary') or {}
            device = next((dict(d) for d in summary.get('devices', []) if d['id'] == peer), None)
            account = summary.get('account')
        if not device or device.get('removed'):
            return
        if messagebox.askokcancel('确认从设备组移除',
                f"确认将设备「{device['name']}」从整个设备组移除？\n设备 ID：{peer}\n\n"
                '其他成员会同步此移除记录，离线成员联网后生效。\n'
                '停止该设备后续账本同步，保留已有历史用量。\n'
                '该设备需重新输入匹配码才能加入；这不会删除 Tailscale 网络中的设备。',
                default='cancel', icon='warning', parent=self.root):
            engine.commands.put(('remove_device', dict(account=account, device=peer)))
            engine.wakeup.set()

    def switch_tailscale_network(self):
        if self.demo or self.busy:
            return
        mesh = self.engine.mesh if self.engine else None
        node = getattr(mesh, 'node', None)
        if not node:
            messagebox.showinfo('Tailscale', '请先启动匹配节点。', parent=self.root)
            return
        network = mesh.connection_state().get('tailnet') or '当前网络'
        if not messagebox.askokcancel('切换 Tailscale 网络',
                f'将退出本工具内嵌节点的 Tailscale 网络：{network}\n\n'
                '保留应用匹配关系与历史账本，暂时中断同步。\n'
                '随后在浏览器中选择双方要加入的同一网络。\n'
                '要换账号，请在授权页选择“使用其他账号登录”。\n'
                '切换后地址可能变化，需要重新生成并交换匹配码。',
                default='cancel', icon='warning', parent=self.root):
            return
        self.pair_request += 1
        self.pair_flow = dict(stage='authorizing')
        self.pair_panel.set_code('', '正在切换授权。完成后请重新生成匹配码，旧地址可能已失效。')
        def work():
            from .tsnet_node import auth_url
            node.request('logout')
            node.request('login')
            for _ in range(30):
                value = node.request('status').get('auth_url', '')
                if value and auth_url(value):
                    return value
                if self.exited:
                    return ''
                time.sleep(1)
            raise RuntimeError('尚未取得新授权链接，请稍后点击授权登录。')
        def done(value):
            self.config.pop('tailscale_ip', None)
            save_config(self.folder/'settings.json', self.config)
            if value:
                import webbrowser
                webbrowser.open(value)
        self.background(work, done)

    def login_tailscale(self):
        if self.demo:
            return
        from .tsnet_node import auth_url
        mesh = self.engine.mesh if self.engine else None
        state = mesh.connection_state() if mesh else {}
        url = state.get('auth_url', '')
        if url and auth_url(url):
            import webbrowser
            webbrowser.open(url)
        elif state.get('ready'):
            messagebox.showinfo('Tailscale', '本机节点已经授权并就绪。另一台也需授权加入同一 Tailscale 网络。', parent=self.root)
        elif mesh and state.get('state') == 'NeedsLogin' and getattr(mesh, 'node', None):
            if self.busy:
                return
            def request_login():
                mesh.node.request('login')
                for _ in range(15):
                    value = mesh.node.request('status').get('auth_url', '')
                    if value and auth_url(value):
                        return value
                    if self.exited:
                        return ''
                    time.sleep(1)
                return ''
            def open_login(value):
                if value:
                    import webbrowser
                    webbrowser.open(value)
                else:
                    messagebox.showinfo('Tailscale', '尚未取得授权链接，请检查网络后重试。', parent=self.root)
            self.background(request_login, open_login)
        elif mesh:
            messagebox.showinfo('Tailscale', '节点已启动，正在连接或恢复本地通信。无需重复登录，请等待状态更新。', parent=self.root)
        else:
            messagebox.showinfo('Tailscale', '请先生成或接收匹配码以启动节点，等待几秒后再点击授权登录。', parent=self.root)

    def send_pair(self):
        if self.busy or self.pair_flow.get('stage') == 'preparing':
            return
        if self.config.get('link_enabled') or not self.config['rendezvous_url']:
            if not self.demo:
                ident = identity(self.config['codex_home'])
                if ident.get('mode') != 'account' or ident.get('account') not in self.config.get('tracked_accounts', {}):
                    messagebox.showinfo('先添加账号', '请先在账号管理中添加当前登录账号。API 或未添加账号不会启动同步。', parent=self.root)
                    return
            from .autolink import prepare
            self.pair_request += 1
            request = self.pair_request
            self.pair_flow = dict(stage='preparing', started=time.monotonic(), elapsed=0)
            self.pair_panel.set_code('', '正在准备匹配码，生成后会保留在此处供复制。')
            self.pair_panel.render(self.pair_flow, self.last_view)
            self.root.after(100, self.focus_pair_status)
            def work():
                try:
                    return prepare(self.folder), None
                except Exception as e:
                    return None, '本机身份准备失败：'+type(e).__name__+'。请点击重试。'
            def done(result):
                if request != self.pair_request:
                    return
                device, error = result
                if error:
                    self.fail_pair(error)
                else:
                    self.finish_auto_pair(device)
            self.background(work, done)
            return
        try:
            code = create_code(self.config)
        except ValueError as e:
            messagebox.showinfo('先配置发现服务', str(e), parent=self.root)
            return
        self.show_pair_code(code)

    def finish_auto_pair(self, device):
        self.config.update(link_enabled=True, link_device=device, rendezvous_url='', relay_token='', fingerprint='', pair_role='sender')
        node = getattr(self.engine.mesh, 'node', None) if self.engine else None
        reusable = (node and node.device == device and node.process and node.process.poll() is None
                    and self.engine.config.get('link_enabled')
                    and self.engine.config['group_secret'] == self.config['group_secret'])
        if reusable:
            # Generating/retrying a code must not tear down an already live relay.
            save_config(self.folder/'settings.json', self.config)
        else:
            self.persist_restart()
        self.mesh_label.set('正在自动连接公共中转并生成匹配码…无需填写参数')
        if self.demo:
            self.show_pair_code(create_code(self.config))
        else:
            deadline, request = time.monotonic()+90, self.pair_request
            self.root.after(500, lambda: self.wait_pair_ready(deadline, request))

    def fail_pair(self, error):
        self.pair_flow = dict(stage='failed', error=error)
        self.pair_panel.set_code('', '匹配码未生成，请查看上方原因并点击重试。')
        self.pair_panel.render(self.pair_flow, self.last_view)

    def focus_pair_status(self):
        if not self.exited:
            self.pair_tab._parent_canvas.yview_moveto(max(0, self.pair_panel.winfo_y()-8)/max(1, self.pair_tab.winfo_height()))

    def retry_pair(self):
        if self.busy:
            return
        if self.pair_flow.get('stage') in ('failed', 'ready', 'idle') or self.config.get('pair_role') == 'sender':
            self.send_tailscale()
        else:
            self.pair_flow = dict(stage='saved')
            if not self.demo:
                self.restart()
            self.pair_panel.render(self.pair_flow, {})

    def wait_pair_ready(self, deadline, request=None):
        request = self.pair_request if request is None else request
        if request != self.pair_request or self.exited:
            return
        if time.monotonic() >= deadline:
            self.fail_pair('公共连接准备超时，尚未生成可复制的匹配码。请检查本机网络／代理后重试；不需要对方先输入。')
            return
        if self.busy:
            self.root.after(500, lambda: self.wait_pair_ready(deadline, request))
            return
        def probe():
            mesh = self.engine.mesh if self.engine else None
            node = getattr(mesh, 'node', None)
            if node and node.api:
                try:
                    return node.invitation_addresses()
                except (OSError, ValueError):
                    pass
            return []
        def done(addresses):
            if request != self.pair_request:
                return
            if addresses:
                self.config.setdefault('link_addresses', {})[self.config['link_device']] = addresses[:4]
                save_config(self.folder/'settings.json', self.config)
                self.mesh_label.set('匹配码已就绪 · 对方输入后自动连接')
                self.show_pair_code(create_code(self.config))
            elif time.monotonic() < deadline:
                self.root.after(2000, lambda: self.wait_pair_ready(deadline, request))
            else:
                self.fail_pair('公共连接准备超时，尚未生成匹配码。请检查网络后点击重试。')
        self.background(probe, done)

    def show_pair_code(self, code):
        self.pair_flow = dict(stage='ready')
        self.pair_panel.set_code(code)
        self.pair_panel.render(self.pair_flow, self.last_view)
        self.root.after(100, self.focus_pair_status)
        win = ctk.CTkToplevel(self.root)
        win.transient(self.root)
        win.after(200, win.lift)
        win.title('发送匹配码')
        win.geometry('760x350')
        win.configure(fg_color=BG)
        ctk.CTkLabel(win, text='发送匹配码', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w', padx=22, pady=(18, 5))
        ctk.CTkLabel(win, text='交给另一台设备，在那里选择“输入匹配码（接收）”。', text_color=MUTED,
                     font=('Microsoft YaHei UI', 12)).pack(anchor='w', padx=22, pady=(0, 12))
        text = ctk.CTkTextbox(win, wrap='char', font=('Consolas', 12), fg_color=PANEL, corner_radius=10)
        text.pack(fill='both', expand=True, padx=22)
        text.insert('1.0', code)
        text.configure(state='disabled')
        def copy_code():
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            copied.set('已复制；这是设备组访问凭证，请勿公开。')
        copied = tk.StringVar(value='重启后仍保留配对关系；第三台使用同一匹配码。')
        button(win, text='复制完整匹配码', command=copy_code).pack(pady=8)
        ctk.CTkLabel(win, textvariable=copied, text_color=MUTED, font=('Microsoft YaHei UI', 11)).pack(pady=(0, 10))

    def receive_pair(self):
        win = ctk.CTkToplevel(self.root)
        win.title('接收匹配码')
        win.geometry('760x360')
        win.configure(fg_color=BG)
        ctk.CTkLabel(win, text='加入设备组', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w', padx=22, pady=(18, 5))
        ctk.CTkLabel(win, text='确认后立即保存配对信息，再自动连接并同步。无需重复输入，进度会持续显示在配对页面。', text_color=MUTED,
                     font=('Microsoft YaHei UI', 12)).pack(anchor='w', padx=22, pady=(0, 12))
        entry = ctk.CTkTextbox(win, font=('Consolas', 12), fg_color=PANEL, corner_radius=10)
        entry.pack(fill='both', expand=True, padx=22)
        row = ctk.CTkFrame(win, fg_color='transparent')
        row.pack(fill='x', padx=22, pady=18)
        def paste():
            try:
                entry.delete('1.0', 'end')
                entry.insert('1.0', self.root.clipboard_get())
            except tk.TclError:
                pass
        button(row, text='从剪贴板粘贴', command=paste).pack(side='left')
        button(row, text='保存并开始连接', style='Accent.TButton',
               command=lambda: self.accept_pair(entry.get('1.0', 'end'), win)).pack(side='right')

    def accept_pair(self, code, dialog=None):
        try:
            if self.busy:
                raise ValueError('上一项后台操作尚未结束，请稍后匹配。')
            pair = read_code(code)
            if not self.demo and not pair.get('tailscale_enabled'):
                raise ValueError('旧版匹配码不再用于连接。请让原设备组发起方升级、授权 Tailscale 后生成 CQG4 匹配码；不要新建组或清空账本。')
            if not pair.get('link_enabled') and not messagebox.askokcancel('确认加入设备组', '将连接到：\n'+pair['rendezvous_url']+'\n\n同组、同 Codex 账号的设备可交换用量和状态。', parent=self.root):
                return
            self.config.update(pair)
            import uuid
            self.config['tailscale_join_request'] = uuid.uuid4().hex
            self.config['pair_role'] = 'receiver'
            self.url.set(pair['rendezvous_url'])
            self.token.set(pair['relay_token'])
            self.stun.set(pair.get('stun_url', self.config['stun_url']))
            self.persist_restart()
            self.pair_request += 1
            self.pair_flow = dict(stage='saved')
            self.pair_panel.set_code('')
            self.pair_panel.render(self.pair_flow, {})
            self.root.after(100, self.focus_pair_status)
            self.mesh_label.set('匹配信息已保存，等待连接与对端确认…')
            if dialog:
                dialog.destroy()
        except Exception as e:
            messagebox.showerror('匹配失败', str(e), parent=self.root)

    def save_settings(self):
        try:
            if self.busy:
                raise ValueError('上一项后台操作尚未结束，请稍后保存。')
            cap, multiplier, interval = float(self.cap.get()), float(self.multiplier.get()), int(self.interval.get())
            if not 0 < cap <= 100 or not .05 <= multiplier <= 20 or not 15 <= interval <= 300:
                raise ValueError('配额：(0,100]；权重系数：0.05–20；查询间隔：15–300 秒')
            paths = list(self.paths.get(0, 'end'))
            if self.auto_block.get():
                if not is_admin() or not paths:
                    raise ValueError('自动限制需要管理员权限，并至少选择一个 Codex EXE')
                if not messagebox.askokcancel('启用自动限制', '达到配额后会暂停选定 Codex 进程并限制其联网。\n当前任务可能暂停；确认后生效。', parent=self.root):
                    return
            if not Path(self.home.get()).is_dir():
                raise ValueError('Codex 数据目录不存在')
            self.config.update(name=self.device_name.get().strip()[:80] or 'Windows', codex_home=self.home.get(),
                               quota=cap, multiplier=multiplier, interval=interval,
                               auto_block=self.auto_block.get(), program_paths=paths, autostart=self.autostart.get())
            self.limit_pending = self.config['auto_block']
            self.persist_restart()
        except Exception as e:
            self.limit_pending = None
            messagebox.showerror('监测设置', str(e), parent=self.root)

    def change_cap(self):
        if not self.engine:
            return
        if self.cap_dialog and self.cap_dialog.winfo_exists():
            self.cap_dialog.lift()
            return
        ident = self.last_view.get('identity') or {}
        account = ident.get('account')
        if ident.get('mode') != 'account' or account not in self.config.get('tracked_accounts', {}):
            self.tabs.select(self.accounts_tab)
            return
        devices = (self.last_view.get('summary') or {}).get('devices', [])
        local = next((d for d in devices if d['id'] == self.config['device_id']), {})
        current = local.get('cap', self.config['tracked_accounts'][account].get('cap', self.config['quota']))
        self.cap_dialog = CapDialog(self.root, current, ident['label'],
            lambda value: self.engine.set_cap(value, expected_account=account))

    def render_limit(self, view):
        runtime = view.get('auto_block')
        if self.limit_pending is not None and not self.busy and runtime == self.limit_pending and not (self.limit_pending is False and view.get('blocked')):
            self.limit_pending = None
        if runtime is not None and not self.busy and self.limit_pending is None and runtime != self.last_runtime_auto:
            self.last_runtime_auto = runtime
            self.config['auto_block'] = runtime
            self.auto_block.set(runtime)
        state = limit_presentation(view, self.config.get('tracked_accounts', {}), self.limit_pending)
        self.limit_panel.render(state, self.busy)

    def limit_action(self):
        if self.busy or self.limit_pending is not None:
            return
        state = limit_presentation(self.last_view, self.config.get('tracked_accounts', {}))
        if state['command'] == 'disable':
            self.restore()
        elif state['command'] == 'enable':
            ident = self.last_view.get('identity') or {}
            if ident.get('mode') != 'account' or ident.get('account') not in self.config.get('tracked_accounts', {}):
                self.tabs.select(self.accounts_tab)
                return
            paths = self.config['program_paths']
            if not is_admin() or not paths or any(not Path(p).is_file() for p in paths):
                self.limit_setup_note.set('开启自动限额前，请以管理员身份运行，选择实际 Codex EXE，勾选下方自动限制并保存设置。')
                self.tabs.select(self.settings)
                self.settings._parent_canvas.yview_moveto(0)
                return
            self.config['auto_block'] = True
            self.auto_block.set(True)
            self.limit_pending = True
            self.persist_restart()
            self.render_limit(self.last_view)
        else:
            self.tabs.select(self.settings)

    def discover(self):
        def show(paths):
            existing = set(self.paths.get(0, 'end'))
            for p in paths:
                if p not in existing:
                    self.paths.insert('end', p)
        self.background(discover_programs, show)

    def add_path(self):
        p = filedialog.askopenfilename(title='选择需要限制的 Codex EXE', filetypes=[('Executable', '*.exe')], parent=self.root)
        if p:
            self.paths.insert('end', p)

    def restore(self):
        self.config['auto_block'] = False
        self.auto_block.set(False)
        save_config(self.folder/'settings.json', self.config)
        if self.engine:
            self.limit_pending = False
            self.engine.commands.put(('restore', None))
            self.engine.wakeup.set()
            self.render_limit(self.last_view)

    def export_sync_diagnostics(self):
        path = filedialog.asksaveasfilename(parent=self.root, title='导出同步诊断（不含密钥或对话内容）',
            initialfile='CodexQuotaGuard-sync-diagnostics.json', defaultextension='.json',
            filetypes=[('JSON', '*.json')])
        if path:
            try:
                Path(path).write_text(json.dumps(sync_report(self.last_view, self.config['device_id']),
                                                ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as e:
                messagebox.showerror('导出失败', str(e), parent=self.root)

    def render(self, view):
        self.last_view = view
        self.render_charts()
        self.cards['local'].set(sync_caption(view))
        sync_account = (view.get('identity') or {}).get('account')
        confirmed = sync_confirmed_at(view)
        if self.last_sync[0] != sync_account:
            self.last_sync = (sync_account, None)
        if confirmed:
            self.last_sync = (sync_account, confirmed)
        at = self.last_sync[1]
        stamp = datetime.fromtimestamp(at) if at else None
        self.sync_time.set('最近同步 '+(stamp.strftime('%H:%M:%S' if stamp.date() == datetime.now().date()
                                                     else '%m-%d %H:%M:%S') if stamp else '—'))
        self.render_limit(view)
        ident = view.get('identity') or {}
        self.show_detected_account(ident)
        self.account.set(f"{ident.get('label', '未识别账号')}    {ident.get('plan', '').upper()}    ·    本机：{self.config['name']}")
        self.mesh_label.set('连接诊断：'+view.get('mesh', ''))
        self.sync_progress_label.set(progress_text(view.get('sync_progress', {})))
        connection = view.get('connection') or {}
        tailnet = connection.get('tailnet')
        self.tailnet_label.set('内嵌节点所属网络：'+(
            str(tailnet)+'\n两边账号可以不同，但节点必须加入同一网络；接受邀请不会自动迁移已有节点。'
            if tailnet and connection.get('state') == 'Running' else '尚未确认（等待节点授权或连接）'))
        if self.pair_flow.get('stage') == 'preparing':
            self.pair_flow['elapsed'] = int(time.monotonic()-self.pair_flow['started'])
        self.pair_panel.render(self.pair_flow, view)
        error = view.get('error', '')
        if ident.get('mode') != 'account' or ident.get('account') not in self.config.get('tracked_accounts', {}):
            self.observed.set('当前是 API、未登录或未添加账号，不统计订阅会话与用量。')
        else:
            self.observed.set(f"本机日志观测：本账号活动 {view.get('active', 0)} · 归属待确认活动 {view.get('unbound_active', 0)}\n"
                              f"长时间无新日志：本账号 {view.get('uncertain', 0)} · 归属待确认 {view.get('unbound_uncertain', 0)}。待归属活动不计入本账号 Token 或限额。")
        self.status.set(('已限制 Codex · ' if view.get('blocked') else '')+view.get('status', '')+(' | '+error if error else ''))
        summary = view.get('summary')
        self.account_tokens.set(budget_text((summary or {}).get('token_budget')))
        if not summary or not summary.get('epoch'):
            self.quota_settings.set('当前账号本机配额：—')
            self.cycle_tokens.set('本额度周期 Token：—')
            for value in self.history_values.values():
                value.set('—')
            for key in ('global', 'reset'):
                self.cards[key].set('—')
            self.table.delete(*self.table.get_children())
            self.meter['value'] = 0
            self.detail.set('等待可用的账号周额度快照；不把未知数据记为 0%。')
            return
        e = summary['epoch']
        local = next((d for d in summary['devices'] if d['id'] == self.config['device_id']), {})
        self.cycle_tokens.set(f"本额度周期 Token：本机 {number(local.get('tokens', 0))}   ·   已配对设备合计 {number(sum(d['tokens'] for d in summary['devices'] if not d.get('removed')))}")
        for unit, value in self.history_values.items():
            value.set(number((view.get('history') or {}).get('current', {}).get(unit, 0)))
        self.cards['global'].set(f"{e['used']:.0f}%")
        personal = self.config.get('quota_display') == 'personal'
        self.quota_settings.set(f"当前账号本机配额：{local.get('cap', self.config['quota']):g}%")
        self.table.heading('used', text='个人已用' if personal else '估算已用')
        self.cards['reset'].set(datetime.fromtimestamp(e['reset_at']).strftime('%m-%d  %H:%M'))
        self.meter['value'] = e['used']
        self.detail.set(f"账号剩余 {100-e['used']:.0f}%   ·   监测前基线 {e['baseline']:.0f}%   ·   未归属 {summary['unassigned']:.2f}%   ·   待稳定分摊 {summary['provisional']:.2f}%"
                        + ('   ·   正在确认重置' if summary['reset_pending'] else '')
                        + ('\n本机旧日志扫描中…' if (view.get('recovery') or {}).get('scanning') else
                           f"\n本机历史累计补记 {number((view.get('recovery') or {}).get('recovered_tokens', 0))} Token"
                           f"（含推断 {number((view.get('recovery') or {}).get('inferred_tokens', 0))}）"
                           f"   ·   本机日志待核对 {number((view.get('recovery') or {}).get('unresolved_tokens', 0))} Token")
                        + ('\n运行身份索引中…' if ((view.get('recovery') or {}).get('runtime') or {}).get('scanning') else
                           f"\n按运行实例补记 {number((view.get('recovery') or {}).get('runtime_tokens', 0))} Token"))
        old = set(self.table.get_children())
        for d in summary['devices']:
            if d.get('removed'):
                continue
            alias = self.config.get('device_notes', {}).get(ident.get('account'), {}).get(d['id'])
            name = (alias or d['name'])+(' · 本机' if d['id'] == self.config['device_id'] else '')
            unknown = d.get('unbound_active', 0)
            state = ('Codex 使用中' if d['active'] else '活动·待归属' if unknown else '暂无近期活动') if d['online'] else '离线/已切换'
            if not d['online'] and d['id'] in view.get('peers', {}):
                state = '连接在线·监测状态过期'
            route = '本机' if d['id'] == self.config['device_id'] else view.get('peers', {}).get(d['id'], {}).get('route', '未连接')
            active = (str(d['active'])+(' + ?'+str(d['uncertain']) if d['uncertain'] else '')) if d['online'] else '—'
            if d['online'] and unknown:
                active += f' + {unknown}待归属'
            displayed, cap = quota_display(d['estimated'], d['cap'], personal)
            values = (name, state, number(d['tokens']),
                      f'{displayed:.2f}%' if displayed is not None else '—')
            if d['id'] in old:
                self.table.item(d['id'], values=values, tags=('local' if d['id'] == self.config['device_id'] else 'online' if d['online'] else 'offline',))
                old.remove(d['id'])
            else:
                self.table.insert('', 'end', iid=d['id'], values=values, tags=('local' if d['id'] == self.config['device_id'] else 'online' if d['online'] else 'offline',))
            detail = d['name'] if alias else ('本机设备' if d['id'] == self.config['device_id'] else route)
            self.table.detail(d['id'], detail)
            self.table.color(d['id'], self.device_color(d['id']))
        for iid in old:
            self.table.delete(iid)
        calibration = summary.get('calibration')
        extra = f"   独占校准样本：{calibration['samples']}（不是误差保证）" if calibration else ''
        self.note.set('*活动会话是日志观测，不是服务端并发数。“?” 表示长时间无新日志；“待归属”不计入本账号用量。'+extra)

    def refresh(self):
        while not self.ui_actions.empty():
            self.ui_actions.get_nowait()()
        while not self.results.empty():
            done, value, error = self.results.get_nowait()
            self.busy = False
            if error:
                self.limit_pending = None
                messagebox.showerror('操作失败', error, parent=self.root)
            elif done:
                done(value)
            if self.exited:
                return
        if self.engine:
            view = self.engine.snapshot()
            if not self.hidden and not getattr(self, 'moving', False):
                self.render(view)
            for note in view.get('notifications', []):
                if self.tray and self.hidden:
                    self.tray.notify(note)
                else:
                    messagebox.showinfo('Codex 设备配额提醒', note, parent=self.root)
        self.root.after(2000 if self.hidden else 1000, self.refresh)

    def window_transition(self, showing, done=None):
        if getattr(self, 'window_job', None):
            self.root.after_cancel(self.window_job)
        frames = 12 if showing else 8
        def tick(frame=0):
            self.window_job = None
            t = 1-(1-frame/frames)**3
            self.root.attributes('-alpha', t if showing else 1-.8*t)
            if frame < frames:
                self.window_job = self.root.after(16, lambda: tick(frame+1))
            else:
                if done:
                    done()
                self.root.attributes('-alpha', 1)
        tick()

    def minimize(self):
        self.window_transition(False, self.root.iconify)

    def close(self):
        if self.tray:
            self.hidden = True
            if self.engine:
                self.engine.background_mode = True
            self.window_transition(False, self.root.withdraw)
        else:
            self.minimize()

    def show(self):
        self.hidden = False
        if self.engine:
            self.engine.background_mode = False
            self.engine.wakeup.set()
        self.root.deiconify()
        self.root.lift()
        self.window_transition(True)

    def quit(self):
        if self.busy:
            messagebox.showinfo('请稍候', '后台操作尚未结束，请稍后关闭。', parent=self.root)
            return
        if self.engine and self.engine.blocked:
            if not messagebox.askokcancel('恢复并退出', '退出将恢复本工具暂停的 Codex 进程并撤销限制。\n要继续保持限制，请取消并最小化窗口。', parent=self.root):
                return
        def work():
            if self.engine:
                self.engine.close()
                if self.engine.blocked or self.database.get('paused_processes'):
                    self.engine.restore()
        def done(_):
            if self.tray:
                self.tray.stop()
            self.exited = True
            self.root.destroy()
        self.background(work, done)
