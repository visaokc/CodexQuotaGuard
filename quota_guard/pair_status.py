"""Pairing UI presents user action, authenticated peers, and ledger receipts separately."""
import time
from datetime import datetime

from . import skin as ctk


def presentation(flow, view, now=None):
    now = time.time() if now is None else now
    stage = flow.get('stage', 'idle')
    shared = view.get('shared_group_enabled') is True
    peers, receipts = view.get('peers', {}), view.get('sync_receipts', {})
    connection = view.get('connection', {})
    recent = {p: receipts[p] for p in peers if p in receipts and now-receipts[p] < 90}
    steps = [stage in ('ready', 'saved') or bool(peers), bool(connection.get('ready') or connection.get('relay_ready') or peers), bool(peers), bool(recent)]
    color = '#69d9bd'
    if stage == 'authorizing':
        title = '等待内嵌 Tailscale 就绪'
        detail = '首次请点击“授权登录 Tailscale”，在浏览器将'+('各端' if shared else '两端')+'加入同一私人网络；无需另装客户端。授权完成后自动生成匹配码。'
    elif stage == 'preparing':
        title = '正在生成匹配码 · '+str(flow.get('elapsed', 0))+' 秒'
        detail = '正在准备本机身份和公共连接。完成后匹配码会显示在下方；现在不需要对方输入。'
    elif stage == 'failed':
        title, detail, color = '匹配码尚未生成', flow.get('error', '准备失败，请重试。'), '#f2b46f'
    elif not shared and (view.get('identity', {}).get('mode') in ('api', 'none', 'logged_out') or view.get('pair_scope') is False):
        title, detail, color = '当前未同步 · 请登录已添加的订阅账号', '配对信息仍保留。API 或未添加账号不会启动订阅用量同步。', '#f2b46f'
        steps[1:] = [False]*3
    elif recent:
        title = ('共享组 · ' if shared else '')+f'已连接 {len(peers)} 台设备 · 已收到同步数据'
        detail = f'已收到并处理 {len(recent)}/{len(peers)} 台设备的近期同步消息；不表示全部历史记录已补传完成。'
    elif peers and any(p in receipts for p in peers):
        title, detail, color = ('共享组 · ' if shared else '')+f'已连接 {len(peers)} 台设备 · 同步消息暂未更新', '之前已收到同步，但近期没有新回执；正在等待下一次同步，尚不能确认数据是否最新。无需重复粘贴。', '#f2b46f'
    elif peers:
        title = ('共享组 · ' if shared else '')+f'已连接 {len(peers)} 台设备 · 等待首次用量同步'
        detail = ('共享组设备' if shared else '同账号设备')+'已通过加密校验，尚未收到可确认的用量同步。无需重复粘贴。'
    elif receipts:
        title, detail, color = ('共享组设备连接已中断 · 正在自动重连' if shared else '设备连接已中断 · 正在自动重连'), '保留已保存的配对信息，不需要重新粘贴。'+('尚不能确定是对方退出还是网络中断。' if shared else '尚不能确定是对方退出、账号切换还是网络中断。'), '#f2b46f'
    elif stage == 'ready':
        title = '匹配码已生成 · 请复制给对方'
        detail = '对方粘贴并确认后自动连接。尚未发现'+('共享组设备' if shared else '同账号设备')+'，无法判断对方是否已粘贴或是否在线。'
    elif stage == 'saved':
        title = '共享组信息已保存 · 正在加入' if shared else '配对信息已保存 · 正在连接'
        detail = ('确认后配置立即保存，无需再次粘贴。各成员须运行新版软件，并将内嵌 Tailscale 授权到同一私人网络；Codex 登录不同账号也能连接共享组。'
                  if shared else '确认后配置立即保存，连接与同步仍需等待，无需再次粘贴。双方须运行本工具并登录同一个已添加账号。')
    else:
        title, detail = '尚未开始配对', '发起方生成并复制匹配码；接收方粘贴确认。只有显示已连接并收到同步数据，才说明同步链路已工作。'
    if stage not in ('preparing', 'failed') and not peers:
        if connection.get('transport') == 'tailscale':
            state = connection.get('state')
            if connection.get('phase') == 'retrying':
                detail += '\n本地节点通信异常，正在恢复；这不等于账号未授权，具体原因见连接诊断。'
            elif state == 'NeedsLogin':
                detail += '\n需要浏览器授权：点击“授权登录 Tailscale”。'
            elif state == 'NeedsMachineAuth':
                detail += '\n等待 Tailscale 网络管理员批准此设备。'
            elif connection.get('ready'):
                detail += '\nTailscale 节点就绪，尚未收到'+('共享组' if shared else '同账号')+'对端握手；请确认'+('各端' if shared else '双方')+'已授权、升级并交换 CQG4 匹配码。'
            else:
                detail += '\n正在连接 Tailscale 控制服务；不会退回旧公共中转。'
        elif connection.get('phase') == 'retrying':
            error = connection.get('error', '连接失败')
            explanations = {'TimeoutError': '连接请求超时，请检查网络／代理',
                'URLError': '连接组件接口暂时不可达', 'OSError': '连接组件或网络访问异常',
                'RuntimeError': '连接组件未能正常运行', 'ValueError': '连接组件返回的数据无效'}
            detail += '\n连接组件异常，正在自动重试：'+explanations.get(error, '连接准备未完成')+'（'+error+'）'
            color = '#f2b46f'
        elif connection.get('relay_ready'):
            detail += '\n公共中转已就绪；尚未发现同账号对端。可能对方未运行、账号不同或网络尚未连通，当前无法确定原因。'
        elif connection.get('phase') in ('starting', 'waiting'):
            detail += '\n正在建立公共连接；如等待较久，请检查两端联网及代理状态，可点击重新连接。'
    if peers:
        detail += '\n连接方式：'+' / '.join(sorted({p.get('route', '加密连接') for p in peers.values()}))
        names = {d['id']: d['name'] for d in (view.get('summary') or {}).get('devices', [])}
        for peer in peers:
            stamp = receipts.get(peer)
            detail += '\n'+names.get(peer, '对端 '+peer[:8])+'：'+(
                '上次收到同步 '+datetime.fromtimestamp(stamp).strftime('%H:%M:%S') if stamp else '尚未收到首次同步')
    if receipts:
        stamp = max(receipts.values())
        detail += '\n上次收到并处理同步：'+datetime.fromtimestamp(stamp).strftime('%m-%d %H:%M:%S')+f'（{max(0, int(now-stamp))} 秒前）'
    return dict(title=title, detail=detail, steps=steps, color=color)


class PairPanel(ctk.CTkFrame):
    def __init__(self, parent, retry):
        super().__init__(parent, fg_color='#1a2432', corner_radius=14, border_width=1, border_color='#295447')
        self.code = ''
        self.title = ctk.CTkLabel(self, text='', anchor='w', font=('Microsoft YaHei UI', 17, 'bold'))
        self.title.pack(fill='x', padx=20, pady=(17, 8))
        row = ctk.CTkFrame(self, fg_color='transparent')
        row.pack(fill='x', padx=20, pady=(0, 10))
        self.steps = []
        for i, text in enumerate(('匹配信息', '连接通道', '同账号设备', '收到同步')):
            row.grid_columnconfigure(i, weight=1)
            label = ctk.CTkLabel(row, text=f'{i+1:02}  {text}', corner_radius=8, height=32, font=('Microsoft YaHei UI', 12))
            label.grid(row=0, column=i, sticky='ew', padx=(0, 6))
            self.steps.append(label)
        self.detail = ctk.CTkLabel(self, text='', text_color='#8c9eb6', justify='left', anchor='w',
                                  wraplength=820, font=('Microsoft YaHei UI', 12))
        self.detail.pack(fill='x', padx=20, pady=(0, 10))
        self.last_width = None
        self.bind('<Configure>', self.resize_detail)
        self.text = ctk.CTkTextbox(self, height=92, font=('Consolas', 12), wrap='char', fg_color='#101620')
        self.text.pack(fill='x', padx=20)
        self.text.insert('1.0', '匹配码尚未生成。生成完成后会保留在这里，可随时复制。')
        self.text.configure(state='disabled')
        actions = ctk.CTkFrame(self, fg_color='transparent')
        self.actions = actions
        actions.pack(fill='x', padx=20, pady=(10, 16))
        self.copy = ctk.CTkButton(actions, text='复制完整匹配码', command=self.copy_code, state='disabled', height=36,
                                  fg_color='#69d9bd', text_color='#102a26', hover_color='#8be6cf')
        self.copy.pack(side='left')
        self.retry = ctk.CTkButton(actions, text='重新连接', command=retry, height=36, fg_color='#263549', hover_color='#344963')
        self.retry.pack(side='right')

    def resize_detail(self, event):
        # Updating wraplength itself triggers layout events. Do not create a Configure feedback loop.
        if event.width != self.last_width:
            self.last_width = event.width
            self.detail.configure(wraplength=max(250, self._reverse_widget_scaling(event.width)-45))

    def set_code(self, code, placeholder=None):
        self.code = code
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.insert('1.0', code or placeholder or '接收端已保存配对信息，无需重复输入。添加第三台设备时可点击生成匹配码。')
        self.text.configure(state='disabled')
        self.copy.configure(state='normal' if code else 'disabled', text='复制完整匹配码')

    def copy_code(self):
        if self.code:
            self.clipboard_clear()
            self.clipboard_append(self.code)
            self.copy.configure(text='已复制匹配码')

    def render(self, flow, view):
        value = presentation(flow, view)
        self.title.configure(text=value['title'], text_color=value['color'])
        self.detail.configure(text=value['detail'])
        self.configure(border_color=value['color'])
        if flow.get('stage') == 'saved' and not self.code:
            self.text.pack_forget()
            self.copy.pack_forget()
        elif not self.text.winfo_manager():
            self.text.pack(fill='x', padx=20, before=self.actions)
            self.copy.pack(side='left')
        names = ('匹配信息', '连接通道', '共享组设备' if view.get('shared_group_enabled') is True else '同账号设备', '收到同步')
        for i, (label, complete) in enumerate(zip(self.steps, value['steps'])):
            label.configure(text=f'{i+1:02}  {names[i]}', fg_color='#203e35' if complete else '#263549', text_color='#69d9bd' if complete else '#8c9eb6')
        self.retry.configure(text='重试生成匹配码' if flow.get('stage') == 'failed' else '刷新匹配码' if flow.get('stage') == 'ready' else '重新连接',
                             state='disabled' if flow.get('stage') == 'preparing' else 'normal')
