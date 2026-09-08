"""Numeric-only sync diagnostics; never export configuration or journal payloads."""


def rejection_reason(error):
    # Only known validation messages are safe to export, never arbitrary payload text.
    reasons = {
        '同步版本向量无效', '同步记录无效或时钟超前', '事件批次过大', 'Token 事件无效',
        '额度快照无效', '每个设备只能设置自己的配额', '配额必须大于 0 且不超过 100',
        '设备描述无效', '同步批次过大',
        '设备日志发生分叉；不要复制整个客户端数据目录到另一台设备',
    }
    return str(error) if str(error) in reasons else type(error).__name__


def progress(local, peers, vectors, receipts, errors, now):
    result = {}
    for peer in peers:
        remote = vectors.get(peer)
        state = 'waiting'
        send = receive = 0
        if remote is not None:
            origins = local.keys() | remote.keys()
            send = sum(max(0, local.get(k, 0)-remote.get(k, 0)) for k in origins)
            receive = sum(max(0, remote.get(k, 0)-local.get(k, 0)) for k in origins)
            state = 'syncing' if send or receive else 'caught_up'
            if now-receipts.get(peer, 0) > 30:
                state = 'stale'
        if peer in errors:
            state = 'error'
        result[peer] = dict(state=state, send=send, receive=receive)
    return result


def progress_text(values):
    if not values:
        return '账本同步：尚无在线对端'
    if any(v['state'] == 'error' for v in values.values()):
        return '账本同步异常：部分记录被拒绝，请导出同步诊断'
    if any(v['state'] in ('waiting', 'stale') for v in values.values()):
        return '账本同步：等待对端确认进度（网络连通不代表账本已齐）'
    send = sum(v['send'] for v in values.values())
    receive = sum(v['receive'] for v in values.values())
    if send or receive:
        return f'账本同步中：待发送 {send} 条 · 待接收 {receive} 条（按对端累计）'
    return '账本进度已对齐 · 用量分摊自动重算（不代表旧日志已全部采集）'


def report(view, device):
    summary = view.get('summary') or {}
    recovery = view.get('recovery') or {}
    return dict(format=2, device=device, account=summary.get('account'),
        allocation=summary.get('allocation'),
        at=summary.get('server_time'), tracked_since=view.get('tracked_since'),
        epoch={k: (summary.get('epoch') or {}).get(k) for k in
               ('started', 'ended', 'baseline', 'used', 'reset_at', 'observed_at', 'reason')},
        sync=view.get('sync_progress', {}), vectors=view.get('sync_vectors', {}),
        local_vector=view.get('local_vector', {}), sync_errors=view.get('sync_errors', {}),
        devices=[{k: d.get(k) for k in ('id', 'tokens', 'weight', 'unknown_tokens', 'estimated', 'removed')}
                 for d in summary.get('devices', [])],
        recovery={k: recovery.get(k) for k in ('scanning', 'recovered_events', 'recovered_tokens',
            'inferred_tokens', 'runtime_tokens', 'unresolved_events', 'unresolved_tokens', 'unresolved_reasons')})
