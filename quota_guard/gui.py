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
import customtkinter as ctk

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

BG, PANEL, FG, MUTED, ACCENT = '#101620', '#1a2432', '#e8eef8', '#8c9eb6', '#69d9bd'


def button(parent, text, command=None, style=None, **kwargs):
    primary = style == 'Accent.TButton'
    return ctk.CTkButton(parent, text=text, command=command, height=38, corner_radius=9,
                         fg_color=ACCENT if primary else '#263549',
                         hover_color='#8be6cf' if primary else '#344963',
                         text_color='#102a26' if primary else FG,
                         font=('Microsoft YaHei UI', 13, 'bold' if primary else 'normal'), **kwargs)


class Navigation:
    def __init__(self, sidebar, title):
        self.sidebar, self.title = sidebar, title
        self.pages = []
        self.buttons = []

    def add(self, page, text):
        index = len(self.pages)
        self.pages.append(page)
        icons = ['◫', '◎', '⇄', '⚙', 'ⓘ']
        b = ctk.CTkButton(self.sidebar, text=icons[index]+'   '+text, anchor='w', height=45,
                         corner_radius=9, fg_color='transparent', hover_color='#26384b',
                         text_color=MUTED, font=('Microsoft YaHei UI', 14),
                         command=lambda: self.select(page))
        b.pack(fill='x', padx=14, pady=4)
        self.buttons.append((b, text))

    def select(self, page):
        for p, (b, name) in zip(self.pages, self.buttons):
            if p is page:
                p.pack(fill='both', expand=True)
                b.configure(fg_color='#253b40', text_color=ACCENT)
                self.title.set(name)
            else:
                p.pack_forget()
                b.configure(fg_color='transparent', text_color=MUTED)


class Meter(ctk.CTkProgressBar):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, height=7, corner_radius=4, fg_color='#293749', progress_color=ACCENT)
        self.set(0)

    def __setitem__(self, key, value):
        if key == 'value':
            self.set(float(value)/100)


class DeviceTable(ctk.CTkFrame):
    """A native, keyboard-selectable device list without legacy table borders."""
    def __init__(self, parent, columns, **kwargs):
        super().__init__(parent, fg_color=PANEL, corner_radius=0)
        self.columns, self.rows, self.selected = columns, {}, None
        self.headers = ctk.CTkFrame(self, fg_color='transparent', height=38)
        self.headers.pack(fill='x', pady=(0, 8))
        self.empty = ctk.CTkLabel(self, text='等待设备数据\n开始监测或匹配另一台设备后，会在这里显示。',
                                  text_color=MUTED, font=('Microsoft YaHei UI', 12))
        self.empty.pack(expand=True)
        self.bind('<Up>', lambda _: self._move(-1))
        self.bind('<Down>', lambda _: self._move(1))

    def heading(self, col, text):
        i = self.columns.index(col)
        self.headers.grid_columnconfigure(i, weight=2 if i == 0 else 1, uniform='cols')
        ctk.CTkLabel(self.headers, text=text, text_color=MUTED, anchor='w' if i == 0 else 'center',
                     font=('Microsoft YaHei UI', 11)).grid(row=0, column=i, sticky='ew', padx=(10, 4))

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
            row.configure(fg_color='#263c43' if key == iid else '#1c2939')

    def insert(self, parent, index, iid, values, tags=()):
        self.empty.pack_forget()
        row = ctk.CTkFrame(self, fg_color='#1c2939', corner_radius=10, height=66)
        row.pack(fill='x', pady=5)
        row.bind('<Button-1>', lambda _: self._select(iid))
        labels = []
        for i, value in enumerate(values):
            row.grid_columnconfigure(i, weight=2 if i == 0 else 1, uniform='cols')
            label = ctk.CTkLabel(row, text=value, anchor='w' if i == 0 else 'center',
                                font=('Microsoft YaHei UI', 12, 'bold' if i in (0, 5) else 'normal'),
                                corner_radius=6, height=28)
            label.grid(row=0, column=i, sticky='ew', padx=(10, 4), pady=17)
            label.bind('<Button-1>', lambda _, iid=iid: self._select(iid))
            labels.append(label)
        self.rows[iid] = (row, labels)
        self.item(iid, values=values, tags=tags)

    def item(self, iid, values=None, tags=()):
        if values is None:
            return {}
        row, labels = self.rows[iid]
        offline = 'offline' in tags
        for i, (label, value) in enumerate(zip(labels, values)):
            label.configure(text=value, text_color='#73869d' if offline else ACCENT if i == 5 or (i == 0 and 'local' in tags) else FG)
        labels[1].configure(fg_color='#203e35' if '使用中' in values[1] and not offline else 'transparent',
                            text_color=ACCENT if '使用中' in values[1] and not offline else MUTED)

    def delete(self, *ids):
        for iid in ids:
            if iid in self.rows:
                self.rows.pop(iid)[0].destroy()
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
        self.root.geometry(f"{min(1240, self.root.winfo_screenwidth()-60)}x{min(840, self.root.winfo_screenheight()-80)}")
        self.root.minsize(1100, 720)
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
        sidebar = ctk.CTkFrame(shell, width=190, corner_radius=0, fg_color='#151e2b')
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        brand = ctk.CTkFrame(sidebar, fg_color='transparent')
        brand.pack(fill='x', padx=18, pady=(28, 30))
        logo = ctk.CTkLabel(brand, text='C', width=42, height=44, corner_radius=12,
                           fg_color=ACCENT, text_color='#142c29', font=('Segoe UI', 28, 'bold'))
        logo.pack(side='left')
        ctk.CTkLabel(brand, text='  Codex\n  配额管家', text_color=FG, justify='left',
                     font=('Microsoft YaHei UI', 14, 'bold')).pack(side='left')
        ctk.CTkLabel(sidebar, text='WORKSPACE', text_color='#5d718e', font=('Segoe UI', 10, 'bold')).pack(anchor='w', padx=23, pady=(0, 8))
        body = ttk.Frame(shell, padding=(28, 0, 28, 0))
        body.pack(side='left', fill='both', expand=True)
        head = ttk.Frame(body, padding=(0, 28, 0, 8))
        head.pack(fill='x')
        self.page_title = tk.StringVar(value='设备总览')
        ttk.Label(head, textvariable=self.page_title, font=('Microsoft YaHei UI', 22, 'bold')).pack(side='left')
        ctk.CTkLabel(head, text='  估算计量  ·  v'+__version__+'  ', text_color=ACCENT, fg_color='#213934',
                     corner_radius=8, font=('Microsoft YaHei UI', 11), height=28).pack(side='right', pady=5)
        self.account = tk.StringVar(value='正在识别本机当前登录账号…')
        ttk.Label(body, textvariable=self.account, style='Muted.TLabel', padding=(0, 0, 0, 16)).pack(anchor='w')
        self.status = tk.StringVar(value='启动中…')
        footer = ttk.Frame(body, padding=(0, 12, 0, 16))
        footer.pack(side='bottom', fill='x')
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel', wraplength=930).pack(anchor='w')
        pages = ttk.Frame(body)
        pages.pack(fill='both', expand=True)
        self.tabs = Navigation(sidebar, self.page_title)
        self.overview = ctk.CTkScrollableFrame(pages, fg_color=BG, corner_radius=0)
        self.accounts_tab = ctk.CTkScrollableFrame(pages, fg_color=BG, corner_radius=0)
        self.pair_tab = ctk.CTkScrollableFrame(pages, fg_color=BG, corner_radius=0)
        self.settings = ctk.CTkScrollableFrame(pages, fg_color=BG, corner_radius=0)
        self.help_tab = ttk.Frame(pages, padding=(0, 6))
        for tab, title in ((self.overview, '设备总览'), (self.accounts_tab, '账号管理'), (self.pair_tab, '匹配与同步'), (self.settings, '监测与限额'), (self.help_tab, '计量说明')):
            self.tabs.add(tab, text=title)
        self.tabs.select(self.overview)
        badge = ctk.CTkFrame(sidebar, fg_color='#1d2b3c', corner_radius=12)
        badge.pack(side='bottom', fill='x', padx=14, pady=20)
        ctk.CTkLabel(badge, text='设备配对 · 端到端加密', font=('Microsoft YaHei UI', 11), text_color=FG).pack(padx=10, pady=(10, 0))
        ctk.CTkLabel(badge, text='P2P 优先  /  中转保底', font=('Microsoft YaHei UI', 10), text_color=MUTED).pack(padx=10, pady=(0, 10))
        self._overview()
        self._accounts()
        self._pairing()
        self._settings()
        self._help()
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

    def _overview(self):
        cards = ttk.Frame(self.overview)
        cards.pack(fill='x')
        self.cards = {}
        for i, (key, title) in enumerate((('global', '账号周额度已用 · 官方快照'), ('local', '本机周额度已用 · 估算'), ('reset', '下一次官方刷新时间'))):
            cards.columnconfigure(i, weight=1)
            card = ctk.CTkFrame(cards, fg_color='#18332f' if key == 'local' else PANEL, corner_radius=14,
                                border_width=1, border_color='#295447' if key == 'local' else '#243244')
            card.grid(row=0, column=i, sticky='nsew', padx=(0, 12 if i < 2 else 0))
            ctk.CTkLabel(card, text=title, text_color=MUTED, font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=18, pady=(17, 2))
            value = tk.StringVar(value='—')
            ctk.CTkLabel(card, textvariable=value, text_color=ACCENT if key == 'local' else FG,
                     font=('Segoe UI', 31 if key != 'reset' else 23, 'bold')).pack(anchor='w', padx=18, pady=(4, 8))
            if key == 'global':
                self.account_tokens = tk.StringVar(value=budget_text(None))
                self.account_tokens_label = ctk.CTkLabel(card, textvariable=self.account_tokens,
                    text_color=MUTED, font=('Microsoft YaHei UI', 10))
                self.account_tokens_label.pack(anchor='w', padx=18, pady=(0, 4))
            hints = {'global': '账号共享估算 · 已用 / 总 Token', 'local': '按本机加权 Token 分摊', 'reset': '以官方最新快照为准'}
            ctk.CTkLabel(card, text=hints[key], text_color=MUTED, font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=18, pady=(0, 14))
            self.cards[key] = value
        self.meter = Meter(self.overview)
        self.meter.pack(fill='x', pady=(22, 10))
        self.detail = tk.StringVar(value='正在核对本机日志与账号证据，自动补记漏采历史。')
        ttk.Label(self.overview, textvariable=self.detail, style='Muted.TLabel', wraplength=900, justify='left').pack(anchor='w', pady=(0, 17))
        self.limit_panel = LimitPanel(self.overview, self.limit_action)
        self.limit_panel.pack(fill='x', pady=(0, 18))
        self.limit_panel.render(limit_presentation({}, self.config.get('tracked_accounts', {})))
        row = ttk.Frame(self.overview)
        row.pack(fill='x', pady=(0, 10))
        ttk.Label(row, text='同账号设备', font=('Microsoft YaHei UI', 13, 'bold')).pack(side='left')
        button(row, text='设置本机账号配额', command=self.change_cap).pack(side='right')
        columns = ('name', 'status', 'route', 'active', 'tokens', 'used', 'cap')
        table_card = ctk.CTkFrame(self.overview, fg_color=PANEL, corner_radius=14, border_width=1, border_color='#243244')
        table_card.pack(fill='both', expand=True)
        self.table = DeviceTable(table_card, columns=columns)
        for col, title, width in zip(columns, ('设备', '状态', '连接', '活动会话*', '本周期 Token', '估算已用', '设备配额'), (164, 135, 95, 85, 110, 90, 82)):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, minwidth=70, anchor='w' if col == 'name' else 'center')
        self.table.pack(fill='both', expand=True, padx=12, pady=10)
        self.table.tag_configure('local', foreground=ACCENT)
        self.table.tag_configure('online', foreground=FG)
        self.table.tag_configure('offline', foreground='#72849c')
        self.note = tk.StringVar(value='只显示已配对并运行本工具的设备，不是 OpenAI 官方登录设备列表。')
        ttk.Label(self.overview, textvariable=self.note, style='Muted.TLabel', wraplength=1030).pack(anchor='w', pady=12)
        self.observed = tk.StringVar(value='正在读取本机会话活动…')
        ctk.CTkLabel(self.overview, textvariable=self.observed, text_color=ACCENT, justify='left',
                     wraplength=900, font=('Microsoft YaHei UI', 12)).pack(anchor='w', pady=(0, 10))
        buttons = ttk.Frame(self.overview)
        buttons.pack(fill='x')
        button(buttons, text='刷新', command=lambda: self.engine and self.engine.wakeup.set()).pack(side='left')
        button(buttons, text='匹配另一台设备', style='Accent.TButton', command=lambda: self.tabs.select(self.pair_tab)).pack(side='left', padx=8)
        self.cycle_tokens = tk.StringVar(value='本额度周期 Token：—')
        ttk.Label(self.overview, textvariable=self.cycle_tokens, font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w', pady=(18, 10))
        self.history_values = {}
        history = ttk.Frame(self.overview)
        history.pack(fill='x', pady=(0, 10))
        for i, (unit, title) in enumerate([('day', '本机今日 Token'), ('week', '本机自然周 Token'), ('month', '本机本月 Token')]):
            history.columnconfigure(i, weight=1, uniform='history')
            card = ctk.CTkFrame(history, fg_color=PANEL, corner_radius=12)
            card.grid(row=0, column=i, sticky='ew', padx=(0, 10) if i < 2 else 0)
            ctk.CTkLabel(card, text=title, text_color=MUTED, font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=16, pady=(10, 0))
            value = tk.StringVar(value='—')
            ctk.CTkLabel(card, textvariable=value, font=('Segoe UI', 24, 'bold')).pack(anchor='w', padx=16, pady=(0, 10))
            self.history_values[unit] = value
        ttk.Label(self.overview, text='日／自然周／月历史不随额度刷新清零；完整记录见“账号管理 → 查看选中账号账本”。',
                  style='Muted.TLabel', wraplength=850).pack(anchor='w')

    def _pairing(self):
        ttk.Label(self.pair_tab, text='让设备彼此连接', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(self.pair_tab, text='一个匹配码，连接你的工作站、笔记本和家用电脑。配对信息持久保存，重启自动重连。',
                  style='Muted.TLabel', wraplength=920).pack(anchor='w', pady=(8, 20))
        cards = ttk.Frame(self.pair_tab)
        cards.pack(fill='x')
        for i, (num, title, desc, action, command) in enumerate([
            ('01', '从这台设备发起', '生成设备组匹配码，发送给你自己的另一台设备。', '生成匹配码（发送）', self.send_pair),
            ('02', '加入已有设备组', '粘贴另一台设备的匹配码，保存配对并开始同步。', '输入匹配码（接收）', self.receive_pair)]):
            cards.columnconfigure(i, weight=1, uniform='pair')
            card = ctk.CTkFrame(cards, fg_color=PANEL, corner_radius=14, border_color='#2a3b4f', border_width=1)
            card.grid(row=0, column=i, sticky='nsew', padx=(0, 14) if i == 0 else 0)
            ctk.CTkLabel(card, text=num, text_color=ACCENT, font=('Segoe UI', 24, 'bold')).pack(anchor='w', padx=22, pady=(18, 2))
            ctk.CTkLabel(card, text=title, text_color=FG, font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w', padx=22)
            ctk.CTkLabel(card, text=desc, text_color=MUTED, font=('Microsoft YaHei UI', 11), wraplength=390,
                         justify='left').pack(anchor='w', padx=22, pady=(8, 18))
            button(card, text=action, style='Accent.TButton' if i == 0 else None, command=command).pack(fill='x', padx=22, pady=(0, 20))
        self.pair_panel = PairPanel(self.pair_tab, self.retry_pair)
        self.pair_panel.pack(fill='x', pady=(18, 6))
        self.pair_panel.render(self.pair_flow, {})
        self.mesh_label = tk.StringVar(value='连接诊断：尚未启动')
        ctk.CTkLabel(self.pair_tab, textvariable=self.mesh_label, text_color=ACCENT, font=('Microsoft YaHei UI', 12)).pack(anchor='w', pady=(16, 6))
        button(self.pair_tab, text='高级：自建服务（可选）  ▾', command=self.toggle_advanced).pack(anchor='w', pady=8)
        self.advanced = ttk.Frame(self.pair_tab)
        self.url = self.entry(self.advanced, 'WSS 服务地址', self.config['rendezvous_url'])
        self.token = self.entry(self.advanced, '服务访问密钥', self.config['relay_token'], show='•')
        self.stun = self.entry(self.advanced, 'STUN 地址（可留空）', self.config['stun_url'])
        self.force_relay = tk.BooleanVar(value=self.config['force_relay'])
        ctk.CTkCheckBox(self.advanced, text='仅使用加密中转（代理兼容性排查）', variable=self.force_relay,
                       fg_color='#309d82', hover_color='#24745f', font=('Microsoft YaHei UI', 12),
                       checkbox_width=18, checkbox_height=18).pack(anchor='w', pady=8)
        button(self.advanced, text='保存连接设置', command=self.save_connection).pack(anchor='w', pady=8)
        ttk.Label(self.pair_tab, text='无需账号注册或填写网络参数。内置 Syncthing 自动发现设备，直连不可用时使用公共加密中转。\n'
                  '匹配码只交给自己的设备；不上传 Codex 登录凭证或对话内容。双方需运行本工具并添加同一账号。',
                  style='Muted.TLabel', wraplength=920, justify='left').pack(side='bottom', anchor='w', pady=12)

    def _accounts(self):
        card = ctk.CTkFrame(self.accounts_tab, fg_color=PANEL, corner_radius=14)
        card.pack(fill='x', pady=(0, 20))
        ctk.CTkLabel(card, text='扫描登录账号 · 独立计量', text_color=FG,
                     font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w', padx=22, pady=(20, 12))
        self.detected_account = tk.StringVar(value='等待读取当前登录账号…')
        ctk.CTkLabel(card, textvariable=self.detected_account, text_color=ACCENT,
                     font=('Microsoft YaHei UI', 13), wraplength=770, justify='left').pack(anchor='w', padx=22, pady=(0, 15))
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
                  style='Muted.TLabel', justify='left', wraplength=850).pack(anchor='w', pady=20)
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
                lines += ['', '未归属额度逐段核对（不会按设备比例强行补齐）：']
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
        row.pack(fill='x', pady=5)
        ttk.Label(row, text=title, width=23).pack(side='left')
        var = tk.StringVar(value=str(value))
        widget = ctk.CTkEntry(row, textvariable=var, show=show or '', height=36, corner_radius=8, fg_color=PANEL, border_color='#2b3d54', text_color=FG, font=('Microsoft YaHei UI', 12))
        widget.pack(side='left', fill='x', expand=True)
        return var

    def _settings(self):
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
        ctk.CTkLabel(update_row, textvariable=self.update_status, text_color=MUTED, wraplength=580,
                     justify='left', font=('Microsoft YaHei UI', 11)).pack(side='left')
        self.update_button = button(update_row, text='检测更新', command=lambda: self.check_update(True))
        self.update_button.pack(side='right')
        self.limit_setup_note = tk.StringVar(value='')
        ctk.CTkLabel(self.settings, textvariable=self.limit_setup_note, text_color='#f2b46f', wraplength=850,
                     justify='left', font=('Microsoft YaHei UI', 12)).pack(anchor='w', pady=(0, 6))
        self.autostart = tk.BooleanVar(value=self.config.get('autostart', True))
        ctk.CTkCheckBox(self.settings, text='Windows 登录后自动启动（默认开启）', variable=self.autostart,
                       font=('Microsoft YaHei UI', 12)).pack(anchor='w', pady=(4, 12))
        ttk.Label(self.settings, text='点击 X 隐藏到托盘；后台约每 30 秒检查，受限时约每 2 秒检查账号切换。彻底退出请使用托盘菜单。',
                  style='Muted.TLabel', wraplength=850).pack(anchor='w', pady=(0, 10))
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
                  style='Muted.TLabel', wraplength=950).pack(anchor='w', pady=(0, 10))
        self.program_note = tk.StringVar(value='启动时自动查找 Codex 后台 EXE；不会选择 node.exe、Python 或桌面 GUI 外壳。')
        ttk.Label(self.settings, textvariable=self.program_note, style='Muted.TLabel', wraplength=900).pack(anchor='w')
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
                  style='Muted.TLabel', wraplength=950).pack(anchor='w', pady=16)

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
            '账号额度增量按同一时间段的设备权重分摊；这不是官方设备账单，无法承诺固定误差。\n\n'
            '哪些用量不强行归属\n'
            '首次监测前的消费单列为基线；没有对应 Token 或多设备混用未知权重模型的区间单列“未归属”。'
            '只有一台设备有 Token 时，未知模型不再阻止设备归属。账号账本提供未归属的时间段及原因。'
            'Spark 使用独立额度池，不混入主池。离线补传会修正历史估算。所有使用设备都应运行本工具。\n\n'
            '自动补记旧日志\n'
            '升级后自动重读本机已添加账号的历史时段，保留独立数值证据，不改动原始日志及旧采集游标。'
            '日志周额度快照与已知账号的历史快照相互匹配且没有账号冲突时，补记漏采 Token 并同步。'
            '同一会话的连续区间可继承已确认 Token 的账号，首尾均确认时标记区间推断；'
            'Provider、账号、额度指纹冲突或账号切换拒绝记录会截断继承。推断记录不再充当新的证据向外扩散。'
            '新版本还会只读索引本机 process_uuid、thread_id 与 turn_id，把连续运行区间绑定到已确认账号；'
            '重启和 account/updated 通知划分运行区间，旧请求尾部保留原归属。运行证据未索引完成或存在冲突时不扩散推断。'
            '这是额度快照相关性推断，不是逐请求账号证明；不会套用当前登录账号或今日快速模式回填旧记录。'
            '缺少证据的记录保留待核对；其他设备需升级后各自扫描，本机不会代造远端 Token。\n\n'
            '并发数的含义\n'
            '“活动会话”依据本机日志里的开始、完成与最近活动事件判断，并非服务端并发推理数。'
            '120 秒没有日志更新的未结束会话标记“待确认”；等待审批、断线或日志缺失会影响判断。\n\n'
            '限制和刷新\n'
            '默认只提醒。自动限制以至少 120 秒前的已分摊用量为依据，因此会延迟且可能超额。'
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
            self.send_pair()
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
            if not pair.get('link_enabled') and not messagebox.askokcancel('确认加入设备组', '将连接到：\n'+pair['rendezvous_url']+'\n\n同组、同 Codex 账号的设备可交换用量和状态。', parent=self.root):
                return
            self.config.update(pair)
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

    def render(self, view):
        self.last_view = view
        self.render_limit(view)
        ident = view.get('identity') or {}
        self.show_detected_account(ident)
        self.account.set(f"{ident.get('label', '未识别账号')}    {ident.get('plan', '').upper()}    ·    本机：{self.config['name']}")
        self.mesh_label.set('连接诊断：'+view.get('mesh', ''))
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
            self.cycle_tokens.set('本额度周期 Token：—')
            for value in self.history_values.values():
                value.set('—')
            for v in self.cards.values():
                v.set('—')
            self.table.delete(*self.table.get_children())
            self.meter['value'] = 0
            self.detail.set('等待可用的账号周额度快照；不把未知数据记为 0%。')
            return
        e = summary['epoch']
        local = next((d for d in summary['devices'] if d['id'] == self.config['device_id']), {})
        self.cycle_tokens.set(f"本额度周期 Token：本机 {number(local.get('tokens', 0))}   ·   已配对设备合计 {number(sum(d['tokens'] for d in summary['devices']))}")
        for unit, value in self.history_values.items():
            value.set(number((view.get('history') or {}).get('current', {}).get(unit, 0)))
        self.cards['global'].set(f"{e['used']:.0f}%")
        self.cards['local'].set(f"{local.get('estimated', 0):.2f}% / {local.get('cap', self.config['quota']):g}%")
        self.cards['reset'].set(datetime.fromtimestamp(e['reset_at']).strftime('%m-%d  %H:%M'))
        self.meter['value'] = e['used']
        self.detail.set(f"账号剩余 {100-e['used']:.0f}%   ·   监测前基线 {e['baseline']:.0f}%   ·   未归属 {summary['unassigned']:.2f}%   ·   待稳定分摊 {summary['provisional']:.2f}%"
                        + ('   ·   正在确认重置' if summary['reset_pending'] else '')
                        + ('\n本机旧日志扫描中…' if (view.get('recovery') or {}).get('scanning') else
                           f"\n本机自动补记 {number((view.get('recovery') or {}).get('recovered_tokens', 0))} Token"
                           f"（含推断 {number((view.get('recovery') or {}).get('inferred_tokens', 0))}）"
                           f"   ·   本机日志待核对 {number((view.get('recovery') or {}).get('unresolved_tokens', 0))} Token")
                        + ('\n运行身份索引中…' if ((view.get('recovery') or {}).get('runtime') or {}).get('scanning') else
                           f"\n按运行实例补记 {number((view.get('recovery') or {}).get('runtime_tokens', 0))} Token"))
        old = set(self.table.get_children())
        for d in summary['devices']:
            name = d['name']+('（本机）' if d['id'] == self.config['device_id'] else '')
            unknown = d.get('unbound_active', 0)
            state = ('Codex 使用中' if d['active'] else '活动·待归属' if unknown else '暂无近期活动') if d['online'] else '离线/已切换'
            if not d['online'] and d['id'] in view.get('peers', {}):
                state = '连接在线·监测状态过期'
            route = '本机' if d['id'] == self.config['device_id'] else view.get('peers', {}).get(d['id'], {}).get('route', '未连接')
            active = (str(d['active'])+(' + ?'+str(d['uncertain']) if d['uncertain'] else '')) if d['online'] else '—'
            if d['online'] and unknown:
                active += f' + {unknown}待归属'
            values = (name, state, route, active, number(d['tokens']), f"{d['estimated']:.2f}%", f"{d['cap']:g}%")
            if d['id'] in old:
                self.table.item(d['id'], values=values, tags=('local' if d['id'] == self.config['device_id'] else 'online' if d['online'] else 'offline',))
                old.remove(d['id'])
            else:
                self.table.insert('', 'end', iid=d['id'], values=values, tags=('local' if d['id'] == self.config['device_id'] else 'online' if d['online'] else 'offline',))
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
            if not self.hidden:
                self.render(view)
            for note in view.get('notifications', []):
                if self.tray and self.hidden:
                    self.tray.notify(note)
                else:
                    messagebox.showinfo('Codex 设备配额提醒', note, parent=self.root)
        self.root.after(2000 if self.hidden else 1000, self.refresh)

    def close(self):
        if self.tray:
            self.hidden = True
            if self.engine:
                self.engine.background_mode = True
            self.root.withdraw()
        else:
            self.root.iconify()

    def show(self):
        self.hidden = False
        if self.engine:
            self.engine.background_mode = False
            self.engine.wakeup.set()
        self.root.deiconify()
        self.root.lift()

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
