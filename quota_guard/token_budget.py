"""Account-wide token estimates from synchronized, time-aligned usage samples."""


def estimate_budget(epoch, events, devices, segments, now, previous=None, reset_pending=False):
    previous = previous or {}
    sampled = sum(e['tokens'] for e in events if e['ts'] <= now)
    aligned = sum(e['tokens'] for e in events if e['ts'] <= min(now, epoch['observed_at']))
    # A scan watermark is only evidence for known devices, not proof that the
    # account has no unpaired users. All results deliberately remain estimates.
    watermark = min((d['scan_at'] for d in devices.values()), default=0)
    tokens, percent, index = 0, 0.0, 0
    for segment in segments:
        count = 0
        profiled = True
        while index < len(events) and events[index]['ts'] <= segment['end']:
            event = events[index]
            if event['ts'] > segment['start']:
                count += event['tokens']
                profiled &= event['device'] in devices
            index += 1
        if (count and profiled and segment['end'] <= watermark
                and segment['end'] <= now-120):
            tokens += count
            percent += segment['delta']
    calibration = previous
    source = '历史比例' if previous else '等待样本'
    if not reset_pending and tokens and percent >= 2:
        calibration = dict(total_tokens=tokens*100/percent, sample_tokens=tokens,
                           sample_percent=percent, cycle=epoch['cycle'])
        source = '同步样本'
    elif not previous and not reset_pending:
        delta = epoch['used']-epoch['baseline']
        if delta > 0 and aligned > 0:
            calibration = dict(total_tokens=aligned*100/delta, sample_tokens=aligned,
                               sample_percent=delta, cycle=epoch['cycle'], rough=True)
            source = '粗估'
    if calibration.get('rough'):
        source = '粗估'
    total = calibration.get('total_tokens')
    # Never substitute the local/paired token sum for account-wide consumption.
    # Unreported devices still consume the official percentage of this estimate.
    used = total*epoch['used']/100 if total is not None and not reset_pending else None
    return dict(sampled_tokens=sampled, used_tokens=used, total_tokens=total,
                source=source), calibration


def compact_tokens(value):
    # Promote values that would round to 1000 k, instead of displaying 1000.00 k.
    return f'{value/1e6:.2f}M' if value >= 999995 else f'{value/1000:.2f}k'


def budget_text(budget):
    budget = budget or {}
    used, total = budget.get('used_tokens'), budget.get('total_tokens')
    left = '—' if used is None else '≈'+compact_tokens(used)
    right = '待校准' if total is None else '≈'+compact_tokens(total)
    return f'本周期 Token：{left} / {right}'
