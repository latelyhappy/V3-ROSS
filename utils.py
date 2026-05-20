import math
import numpy as np
from datetime import datetime
from email.utils import parsedate_to_datetime
import pytz
from config import TZ_NY, TZ_TW

def safe_float(v):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f): return 0.0
        return f
    except: return 0.0

def format_shares_k_m(n):
    if n <= 0 or math.isnan(n): return "未知"
    if n < 1_000_000: return f"{n/1000:.2f}K"
    return f"{n/1_000_000:.2f}M"

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.2f}K"
    return str(int(n))

def convert_to_taiwan_time(raw_time, source="yahoo"):
    try:
        if source == "yahoo":
            dt_obj = parsedate_to_datetime(raw_time)
            if dt_obj.tzinfo is None: dt_obj = TZ_NY.localize(dt_obj)
            return dt_obj.astimezone(TZ_TW)
        elif source == "finnhub":
            utc_dt = datetime.fromtimestamp(raw_time, pytz.UTC)
            return utc_dt.astimezone(TZ_TW)
    except: return datetime.now(TZ_TW)

def calc_wma(s, length):
    weights = np.arange(1, length + 1)
    return s.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def calc_hma(s, length):
    if len(s) < length: return s.copy() 
    half_length = int(length / 2)
    sqrt_length = int(np.sqrt(length))
    return calc_wma(calc_wma(s, half_length) * 2 - calc_wma(s, length), sqrt_length)