import math
import numpy as np

def safe_float(v):
    try:
        f = float(v)
        return f if not (math.isnan(f) or math.isinf(f)) else 0.0
    except: return 0.0

def calc_wma(s, length):
    weights = np.arange(1, length + 1)
    return s.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def calc_hma(s, length):
    if len(s) < length: return s.copy()
    half = int(length / 2)
    sqrt_l = int(np.sqrt(length))
    wmaf = calc_wma(s, half) * 2
    wmas = calc_wma(s, length)
    diff = wmaf - wmas
    return calc_wma(diff, sqrt_l)