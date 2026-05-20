import os
import json
import requests
import yfinance as yf
from config import SHARES_CACHE_FILE, FINNHUB_TOKEN
import shared_state

def load_shares_cache():
    if os.path.exists(SHARES_CACHE_FILE):
        try:
            with open(SHARES_CACHE_FILE, 'r', encoding='utf-8') as f:
                shared_state.SHARES_CACHE = json.load(f)
        except: pass

def save_shares_cache():
    try:
        with open(SHARES_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(shared_state.SHARES_CACHE, f)
    except: pass

def get_shares_data(symbol):
    if symbol in shared_state.SHARES_CACHE and isinstance(shared_state.SHARES_CACHE[symbol], dict):
        return shared_state.SHARES_CACHE[symbol].get('float', 0.0), shared_state.SHARES_CACHE[symbol].get('outstanding', 0.0)
    
    f_sh, o_sh = 0.0, 0.0
    
    if FINNHUB_TOKEN:
        try:
            res = requests.get(f"[https://finnhub.io/api/v1/stock/metric?symbol=](https://finnhub.io/api/v1/stock/metric?symbol=){symbol}&metric=all&token={FINNHUB_TOKEN}", timeout=3)
            if res.status_code == 200:
                m = res.json().get('metric', {})
                if m.get('floatShares', 0) > 0: f_sh = m['floatShares'] * (1e6 if m['floatShares'] < 50000 else 1)
                if m.get('sharesOutstanding', 0) > 0: o_sh = m['sharesOutstanding'] * (1e6 if m['sharesOutstanding'] < 50000 else 1)
        except: pass

    if f_sh == 0.0 or o_sh == 0.0:
        try:
            tkr = yf.Ticker(symbol.replace('-', '.'))
            info = tkr.info
            if info.get('floatShares'): f_sh = float(info.get('floatShares', 0.0))
            if info.get('sharesOutstanding'): o_sh = float(info.get('sharesOutstanding', 0.0))
        except: pass
        
    if f_sh > 0 or o_sh > 0:
        shared_state.SHARES_CACHE[symbol] = {'float': f_sh, 'outstanding': o_sh}
        save_shares_cache()
        
    return f_sh, o_sh

def fetch_yfinance_prev_close(ticker):
    try:
        tkr = yf.Ticker(ticker.replace('-', '.'))
        hist = tkr.history(period="5d")
        if not hist.empty and len(hist) >= 2: return float(hist['Close'].iloc[-2])
        elif len(hist) == 1: return float(hist['Close'].iloc[0])
    except: pass
    return 0.0