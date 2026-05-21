import math
import numpy as np
from datetime import datetime
from email.utils import parsedate_to_datetime
import pytz

# 🚀 匯入 V59.0 中央時區設定（防範時區遺漏）
try:
    from config import TZ_NY, TZ_TW
except:
    TZ_NY = pytz.timezone('America/New_York')
    TZ_TW = pytz.timezone('Asia/Taipei')

def safe_float(v):
    """安全浮點數轉換，防範 NaN 或 Inf 搞崩前端"""
    try:
        f = float(v)
        return f if not (math.isnan(f) or math.isinf(f)) else 0.0
    except: 
        return 0.0

def convert_to_taiwan_time(raw_time, source="yahoo"):
    """🚀 補齊火控雷達缺口：跨平台動態時區校準引擎 (新聞模組專用)"""
    try:
        if source == "yahoo":
            dt = parsedate_to_datetime(raw_time)
            return dt.astimezone(TZ_TW) if dt.tzinfo else TZ_NY.localize(dt).astimezone(TZ_TW)
        elif source == "finnhub":
            return datetime.fromtimestamp(raw_time, pytz.UTC).astimezone(TZ_TW)
    except: 
        pass
    return datetime.now(TZ_TW)

def calc_wma(s, length):
    """計算加權移動平均 (WMA)"""
    weights = np.arange(1, length + 1)
    return s.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def calc_hma(s, length):
    """核心數學引擎：計算赫爾移動平均 (HMA) 以追蹤主力動能"""
    if len(s) < length: 
        return s.copy()
    half = int(length / 2)
    sqrt_l = int(np.sqrt(length))
    wmaf = calc_wma(s, half) * 2
    wmas = calc_wma(s, length)
    diff = wmaf - wmas
    return calc_wma(diff, sqrt_l)