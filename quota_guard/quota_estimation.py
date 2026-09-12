"""Shared display-only quota estimation and quota/weight calibration."""
import math


def fit_rate(samples):
    """Fit one cumulative quota/weight rate from the supplied sample set."""
    valid = [(float(quota), float(weight)) for quota, weight in samples
             if quota is not None and weight is not None
             and math.isfinite(float(quota)) and math.isfinite(float(weight))
             and float(quota) >= 0 and float(weight) > 0]
    quota = sum(row[0] for row in valid)
    weight = sum(row[1] for row in valid)
    if not valid or weight <= 0:
        return None
    return dict(rate=quota/weight, samples=len(valid), quota=quota, weight=weight)


def project(rows, calibration, weight_for):
    """Project pending rows with one calibration; return estimates and unresolved rows."""
    estimates, missing = [], []
    for row in rows:
        weights = weight_for(row)
        if calibration is None or weights is None:
            missing.append(row)
            continue
        total, cached = weights
        if total is None or not math.isfinite(float(total)) or float(total) < 0:
            missing.append(row)
            continue
        quota = float(total)*calibration['rate']
        value = dict(row, quota=quota, estimated=True)
        value['cache_quota'] = (float(cached)*calibration['rate']
                                if cached is not None and math.isfinite(float(cached)) else None)
        estimates.append(value)
    return estimates, missing
