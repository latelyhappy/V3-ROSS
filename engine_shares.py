import os
import json
import requests
import yfinance as yf

# 🚀 匯入 V59.0 解耦模組
import shared_state
from config import SHARES_CACHE_FILE, FINNHUB_TOKEN

def load_shares_cache():
    """系統啟動時載入股數快取"""
    if os.path.exists(SHARES_CACHE_FILE):
        try:
            with open(SHARES_CACHE_FILE, 'r', encoding='utf-8') as f:
                shared_state.SHARES_CACHE = json.load(f)
        except Exception as e:
            print(f"⚠️ 讀取 {SHARES_CACHE_FILE} 失敗: {e}")

def save_shares_cache():
    """寫入股數快取以減少 API 請求"""
    try:
        with open(SHARES_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(shared_state.SHARES_CACHE, f, ensure_ascii=False, indent=4)
    except: pass

def get_shares_data(symbol):
    """雙通道股數查詢引擎 (Finnhub 優先，Yahoo 備援)"""
    # 1. 檢查快取
    if symbol in shared_state.SHARES_CACHE and isinstance(shared_state.SHARES_CACHE[symbol], dict):
        return shared_state.SHARES_CACHE[symbol].get('float', 0.0), shared_state.SHARES_CACHE[symbol].get('outstanding', 0.0)
    
    f_sh, o_sh = 0.0, 0.0
    
    # 2. Finnhub 優先查詢 (速度快、額度高)
    if FINNHUB_TOKEN:
        try:
            url = f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}&metric=all&token={FINNHUB_TOKEN}"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                metric = res.json().get('metric', {})
                fh_float = metric.get('floatShares', 0)
                fh_out = metric.get('sharesOutstanding', 0)
                if fh_float > 0: f_sh = fh_float * 1e6 if fh_float < 50000 else fh_float
                if fh_out > 0: o_sh = fh_out * 1e6 if fh_out < 50000 else fh_out
        except: pass

    # 3. 如果 Finnhub 失敗或沒資料，動用 Yahoo Finance (高風險被 Ban，作為備援)
    if f_sh == 0.0 or o_sh == 0.0:
        try:
            yf_symbol = symbol.replace('-', '.')
            tkr = yf.Ticker(yf_symbol)
            info = tkr.info
            yf_float = info.get('floatShares')
            yf_out = info.get('sharesOutstanding')
            
            if yf_float and yf_float > 0: f_sh = float(yf_float)
            if yf_out and yf_out > 0: o_sh = float(yf_out)
        except: pass
        
    # 4. 寫入快取
    if f_sh > 0 or o_sh > 0:
        shared_state.SHARES_CACHE[symbol] = {'float': f_sh, 'outstanding': o_sh}
        save_shares_cache()
        
    return f_sh, o_sh

def fetch_yfinance_prev_close(ticker):
    """拆股異常校準器 (YFinance Fallback)"""
    try:
        tkr = yf.Ticker(ticker.replace('-', '.'))
        hist = tkr.history(period="5d")
        if not hist.empty and len(hist) >= 2:
            return float(hist['Close'].iloc[-2])
        elif len(hist) == 1:
            return float(hist['Close'].iloc[0])
    except:
        pass
    return 0.0