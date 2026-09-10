"""Account-wide token estimates from synchronized, time-aligned usage samples."""


def estimate_budget(epoch, events, devices, segments, now, previous=None, reset_pending=False, checkpoints=None):
    previous = previous or {}
    sampled = sum(e['tokens'] for e in events if e['ts'] <= now)
    aligned = sum(e['tokens'] for e in events if e['ts'] <= min(now, epoch['observed_at']))
    # A scan watermark is only evidence for known devices, not proof that the
    # account has no unpaired users. All results deliberately remain estimates.
    contributors = {e['device'] for e in events if e['tokens'] > 0 and e['ts'] <= now}
    # Empty legacy profiles must not veto every synchronized sample. Keep
    # contributors in the gate even when offline, since their logs may be late.
    watermark = min((d['scan_at'] for device, d in devices.items()
                     if device in contributors), default=0)
    joint = checkpoints is not None
    participants = contributors | {device for device, sample in (checkpoints or {}).items()
                                   if sample['declared_through'] > epoch['started']}
    scans = {device: sample['through'] for device, sample in (checkpoints or {}).items()}
    if joint:
        watermark = min((scans.get(device, 0) for device in participants), default=0)
    tokens, percent, index, sample_count = 0, 0.0, 0, 0
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
                and (joint or segment['end'] <= now-120)):
            tokens += count
            percent += segment['delta']
            sample_count += 1
    calibration = previous
    source = '历史比例' if previous else '等待样本'
    if joint:
        # Never mix per-machine rough estimates into a common sample pool.
        calibration = {}
        source = '等待联合样本'
    if not reset_pending and tokens and percent >= 2:
        calibration = dict(total_tokens=tokens*100/percent, sample_tokens=tokens,
                           sample_percent=percent, cycle=epoch['cycle'])
        source = '联合样本' if joint else '同步样本'
    elif not joint and not previous and not reset_pending:
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
    result = dict(sampled_tokens=sampled, used_tokens=used, total_tokens=total, source=source)
    if joint:
        latest = max((s['end'] for s in segments), default=epoch['started'])
        result.update(sample_devices=len(participants),
                      sample_ready=sum(scans.get(d, 0) >= latest for d in participants),
                      sample_segments=sample_count, sample_until=watermark,
                      sample_tokens=tokens, sample_percent=percent)
    return result, calibration


def compact_tokens(value):
    # Promote values that would round to 1000 k, instead of displaying 1000.00 k.
    return f'{value/1e6:.2f}M' if value >= 999995 else f'{value/1000:.2f}k'


def budget_text(budget):
    budget = budget or {}
    sampled = budget.get('sampled_tokens')
    return '本周期已同步 Token：'+('—' if sampled is None else compact_tokens(sampled))
