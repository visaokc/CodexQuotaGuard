"""State-led limit controls; no quota or network side effects live in these widgets."""
from decimal import Decimal, InvalidOperation
import tkinter as tk

from . import skin as ctk

BG, PANEL, FG, MUTED, ACCENT = '#101620', '#1a2432', '#e8eef8', '#8c9eb6', '#69d9bd'


def limit_presentation(view, tracked, pending=None):
    if pending is not None:
        return dict(key='pending', title='正在开启自动限额…' if pending else '正在关闭自动限额并恢复运行…',
                    body='等待后台确认完成；不会清空 Token 记录或重置官方额度。', action='请稍候', command='wait', tone='neutral')
    if view.get('blocked'):
        return dict(key='blocked', title='Codex 已触发限额 · 运行已受限',
                    body='已暂停选定 Codex 并限制联网；确认官方周刷新后自动恢复。',
                    action='恢复运行并关闭自动限额', command='disable', tone='warning')
    enabled = view.get('auto_block')
    if enabled is None:
        return dict(key='unknown', title='正在确认 Codex 自动限额状态…',
                    body='以后台实际状态为准，不把尚未确认的设置显示为已启用。', action='查看设置', command='settings', tone='neutral')
    if not enabled:
        return dict(key='off', title='Codex 自动限额已关闭', body='当前仅统计与提醒，不会自动暂停 Codex 或限制联网。',
                    action='开启自动限额', command='enable', tone='neutral')
    ident = view.get('identity') or {}
    if ident.get('mode') != 'account' or ident.get('account') not in tracked:
        return dict(key='inactive', title='Codex 自动限额已开启 · 当前账号不适用',
                    body='当前是 API、未登录或未添加账号，不执行订阅限额。',
                    action='关闭自动限额', command='disable', tone='warning')
    summary = view.get('summary') or {}
    if view.get('error') or not summary.get('epoch') or summary.get('reset_pending'):
        return dict(key='waiting', title='Codex 自动限额已启用 · 等待额度确认',
                    body='取得有效官方额度后执行限额；数据未知时不会冒充已刷新。',
                    action='关闭自动限额', command='disable', tone='warning')
    return dict(key='on', title='Codex 自动限额已启用',
                body='达到本机账号限额后自动暂停 Codex；确认官方周刷新后恢复。',
                action='关闭自动限额', command='disable', tone='active')


class LimitPanel(ctk.CTkFrame):
    def __init__(self, parent, command):
        super().__init__(parent, fg_color='#233041', corner_radius=16)
        self.last = None
        self.card = ctk.CTkFrame(self, corner_radius=13, border_width=2, fg_color=PANEL)
        self.card.pack(fill='both', expand=True, padx=3, pady=3)
        self.card.grid_columnconfigure(0, weight=1)
        self.title = ctk.CTkLabel(self.card, text='', anchor='w', font=('Microsoft YaHei UI', 14, 'bold'), wraplength=400)
        self.title.grid(row=0, column=0, sticky='w', padx=18, pady=(14, 2))
        self.description = ctk.CTkLabel(self.card, text='', text_color=MUTED, justify='left', anchor='w',
                                       font=('Microsoft YaHei UI', 11), wraplength=400)
        self.description.grid(row=1, column=0, sticky='w', padx=18, pady=(0, 15))
        self.action = ctk.CTkButton(self.card, text='', command=command, width=170, height=40, corner_radius=9,
            font=('Microsoft YaHei UI', 13, 'bold'))
        self.action.grid(row=0, column=1, rowspan=2, padx=(12, 18), pady=18)

    def render(self, presentation, busy=False):
        if self.last == (presentation, busy):
            return
        self.last = (dict(presentation), busy)
        tone = presentation['tone']
        color = ACCENT if tone == 'active' else '#f2b46f' if tone == 'warning' else MUTED
        self.configure(fg_color='#204c40' if tone == 'active' else '#4b3926' if tone == 'warning' else '#233041')
        self.card.configure(border_color=color if tone != 'neutral' else '#354359',
                            fg_color='#152f29' if tone == 'active' else '#30281f' if tone == 'warning' else PANEL)
        self.title.configure(text='●  '+presentation['title'], text_color=color if tone != 'neutral' else FG)
        self.description.configure(text=presentation['body'])
        primary = presentation['command'] == 'enable'
        self.action.configure(text=presentation['action'], fg_color=ACCENT if primary else '#344134' if tone == 'active' else '#344155',
                              text_color='#102a26' if primary else FG, hover_color='#8be6cf' if primary else '#43516a',
                              state='disabled' if busy or presentation['command'] == 'wait' else 'normal')


def parse_cap(text):
    try:
        value = Decimal(text.strip().removesuffix('%').strip())
        if not value.is_finite() or not Decimal('.01') <= value <= 100:
            raise ValueError('请输入 0.01–100 之间的限额。')
        if value != value.quantize(Decimal('.01')):
            raise ValueError('限额最多保留两位小数。')
        return float(value)
    except (InvalidOperation, TypeError):
        raise ValueError('请输入有效数字，例如 33 或 33.5。') from None


class CapDialog(ctk.CTkToplevel):
    def __init__(self, parent, current, account_label, submit):
        super().__init__(parent)
        self.submit = submit
        self.title('Codex 配额管家 · 本机账号限额')
        self.configure(fg_color=BG)
        self.geometry('650x500')
        self.resizable(False, False)
        self.transient(parent)
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        self.bind('<Escape>', lambda _: self.cancel())
        self.bind('<Return>', lambda _: self.save())
        ctk.CTkLabel(self, text='本机账号限额', text_color=FG, font=('Microsoft YaHei UI', 24, 'bold')).pack(anchor='w', padx=28, pady=(22, 4))
        ctk.CTkLabel(self, text=account_label, text_color=MUTED, font=('Microsoft YaHei UI', 12), wraplength=580,
                     justify='left').pack(anchor='w', padx=28, pady=(0, 18))
        card = ctk.CTkFrame(self, fg_color='#152f29', corner_radius=15, border_width=1, border_color='#326b5b')
        card.pack(fill='x', padx=28)
        ctk.CTkLabel(card, text='占账号完整周额度的比例', text_color=MUTED, font=('Microsoft YaHei UI', 12)).pack(pady=(12, 0))
        row = ctk.CTkFrame(card, fg_color='transparent')
        row.pack(pady=6)
        self.value = tk.StringVar(value=f'{current:.2f}'.rstrip('0').rstrip('.'))
        self.entry = ctk.CTkEntry(row, textvariable=self.value, justify='center', width=164, height=60,
            fg_color='#183b31', border_color='#408c75', border_width=1, corner_radius=9,
            text_color=ACCENT, font=('Segoe UI', 38, 'bold'))
        self.entry.pack(side='left')
        ctk.CTkLabel(row, text=' %', text_color=ACCENT, font=('Segoe UI', 30, 'bold')).pack(side='left')
        self.entry.bind('<FocusIn>', lambda _: self.entry.select_range(0, 'end'))
        ctk.CTkLabel(card, text='点击数值直接输入，或拖动下方滑块', text_color=MUTED, font=('Microsoft YaHei UI', 11)).pack(pady=(0, 12))
        self.slider = ctk.CTkSlider(self, from_=.01, to=100, number_of_steps=9999, command=self.slide,
            progress_color=ACCENT, button_color=ACCENT, button_hover_color='#a2efd9', fg_color='#2b3b50',
            height=24, button_length=20, border_width=5)
        self.slider.pack(fill='x', padx=34, pady=(20, 0))
        self.slider.set(current)
        ticks = ctk.CTkFrame(self, fg_color='transparent')
        ticks.pack(fill='x', padx=34)
        ctk.CTkLabel(ticks, text='0.01%', text_color=MUTED, font=('Segoe UI', 11)).pack(side='left')
        ctk.CTkLabel(ticks, text='100%', text_color=MUTED, font=('Segoe UI', 11)).pack(side='right')
        self.error = tk.StringVar(value='只修改本机当前账号，不影响其他设备，也不会开关自动限额。')
        self.hint = ctk.CTkLabel(self, textvariable=self.error, text_color=MUTED, font=('Microsoft YaHei UI', 11), wraplength=590)
        self.hint.pack(padx=28, pady=(8, 12))
        actions = ctk.CTkFrame(self, fg_color='transparent')
        actions.pack(fill='x', padx=28, pady=(0, 20))
        ctk.CTkButton(actions, text='取消', fg_color='#263549', hover_color='#344963', height=40, corner_radius=9,
                      font=('Microsoft YaHei UI', 13), command=self.cancel).pack(side='left')
        self.save_button = ctk.CTkButton(actions, text='保存本机限额', fg_color=ACCENT, hover_color='#8be6cf',
            text_color='#102a26', height=40, corner_radius=9, font=('Microsoft YaHei UI', 13, 'bold'), command=self.save)
        self.save_button.pack(side='right')
        self.value.trace_add('write', self.changed)
        self.update_idletasks()
        x = parent.winfo_rootx()+(parent.winfo_width()-self.winfo_width())//2
        y = parent.winfo_rooty()+(parent.winfo_height()-self.winfo_height())//2
        self.geometry(f'+{max(0, x)}+{max(0, y)}')
        self.activation = self.after(100, self.activate)

    def activate(self):
        self.activation = None
        self.lift()
        self.grab_set()
        self.entry.focus_set()

    def changed(self, *_):
        try:
            value = parse_cap(self.value.get())
            self.slider.set(value)
            self.error.set('只修改本机当前账号，不影响其他设备，也不会开关自动限额。')
            self.hint.configure(text_color=MUTED)
            self.save_button.configure(state='normal')
        except ValueError as e:
            self.error.set(str(e))
            self.hint.configure(text_color='#f2b46f')
            self.save_button.configure(state='disabled')

    def slide(self, value):
        self.value.set(f'{value:.2f}'.rstrip('0').rstrip('.'))

    def save(self):
        try:
            value = parse_cap(self.value.get())
            self.submit(value)
        except Exception as e:
            self.error.set(str(e))
            self.hint.configure(text_color='#f2b46f')
            return
        self.cancel()

    def cancel(self):
        if self.activation is not None:
            self.after_cancel(self.activation)
            self.activation = None
        self.grab_release()
        self.destroy()
