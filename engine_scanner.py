import time
import threading
import requests
from datetime import datetime
import pytz
import yfinance as yf
import warnings

# 強制消音 Pandas 警告
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', message='.*Timestamp.utcnow.*')

import shared_state
from config import TZ_NY, TZ_TW
from utils import safe_float

# 全域靜態快取與防阻塞鎖
INFO_CACHE = {}
FETCHING_CACHE = set()

def format_volume(v):
    if v >= 1000000: return f"{v/1000000:.2f}M"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v))

def fetch_yf_info(sym):
    """背景非同步抓取籌碼與昨收價，絕不阻塞 2 秒極速報價迴圈"""
    try:
        info = yf.Ticker(sym).info
        INFO_CACHE[sym] = {
            'out': safe_float(info.get('sharesOutstanding', 0)),
            'flt': safe_float(info.get('floatShares', 0)),
            'avg_vol': safe_float(info.get('averageVolume', 0)),
            'prev_c': safe_float(info.get('previousClose', 0))
        }
    except:
        if sym not in INFO_CACHE:
            INFO_CACHE[sym] = {'out': 0, 'flt': 0, 'avg_vol': 0, 'prev_c': 0}
    finally:
        if sym in FETCHING_CACHE:
            FETCHING_CACHE.remove(sym)

def update_scanner_loop():
    print("🚀 [SCANNER] 啟動 TV 極速報價 + 24小時無盲區時區對齊引擎...")
    
    url = "https://scanner.tradingview.com/america/scan"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json"
    }

    while True:
        try:
            now_ny = datetime.now(TZ_NY)
            
            # 🚀 核心修復：精確鎖定真正的美東盤前（04:00 - 09:30）
            is_real_premarket = (4 <= now_ny.hour < 9) or (now_ny.hour == 9 and now_ny.minute < 30)
            
            if is_real_premarket:
                sort_col = "premarket_change"
                vol_target_col = "premarket_volume"
            else:
                # 常規盤與深夜時段全部對齊 Regular 欄位，徹底終結深夜白畫面！
                sort_col = "change"
                vol_target_col = "volume"

            # 🚀 100% 安全封包，設定硬性門檻：成交量 > 100,000 股 (100K)
            payload = {
                "filter": [
                    {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
                    {"left": "close", "operation": "in_range", "right": [0.5, 50.0]},
                    {"left": vol_target_col, "operation": "egreater", "right": 100000} 
                ],
                "options": {"lang": "en"},
                "markets": ["america"],
                "symbols": {"query": {"types": []}, "tickers": []},
                "columns": ["name", "close", "change", "volume", "premarket_close", "premarket_change", "premarket_volume"], 
                "sort": {"sortBy": sort_col, "sortOrder": "desc"},
                "range": [0, 30] 
            }

            response = requests.post(url, json=payload, headers=headers, timeout=5)
            
            if response.status_code != 200:
                time.sleep(2)
                continue
            
            data = response.json()
            valid_results = []
            tickers_to_scan = []

            for item in data.get("data", []):
                d = item.get("d", [])
                if len(d) < 7: continue

                sym = d[0].split(":")[1] if ":" in d[0] else d[0]
                reg_close, reg_change, reg_vol = safe_float(d[1]), safe_float(d[2]), safe_float(d[3])
                pre_close, pre_change, pre_vol = safe_float(d[4]), safe_float(d[5]), safe_float(d[6])

                if is_real_premarket:
                    price = pre_close if pre_close > 0 else reg_close
                    pct = pre_change if pre_close > 0 else reg_change
                    vol = pre_vol
                else:
                    price = reg_close
                    pct = reg_change
                    vol = reg_vol

                # 二次保險：未滿 100K 股直接剔除
                if vol < 100000: continue

                tickers_to_scan.append(sym)
                
                if sym not in INFO_CACHE and sym not in FETCHING_CACHE:
                    FETCHING_CACHE.add(sym)
                    threading.Thread(target=fetch_yf_info, args=(sym,), daemon=True).start()
                
                cache = INFO_CACHE.get(sym, {'out': 0, 'flt': 0, 'avg_vol': 0, 'prev_c': 0})
                out_val, flt_val, avg_vol, prev_c = cache['out'], cache['flt'], cache['avg_vol'], cache['prev_c']

                out_str = format_volume(out_val) if out_val > 0 else "載入中"
                flt_str = format_volume(flt_val) if flt_val > 0 else "載入中"
                
                rel_vol = (vol / avg_vol) if avg_vol > 0 else 0.0
                gap_pct = ((price - prev_c) / prev_c * 100) if prev_c > 0 else 0.0
                
                valid_results.append({
                    "Code": sym,
                    "ticker": sym,
                    "PriceVal": price,
                    "PctVal": pct,
                    "Daily_Vol_Raw": vol,
                    "GapPct": gap_pct, 
                    "Price": f"${price:.2f}",
                    "Pct": f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%",
                    "Vol": format_volume(vol),
                    "Outstanding": out_str,
                    "Float": flt_str,
                    "Float_Color": "cyan-bg" if (0 < flt_val <= 20000000) else "gray-bg",
                    "RelVol": f"{rel_vol:.2f}x" if rel_vol > 0 else "-",
                    "EMA9_Dev": 0.0,
                    "VWAP_Dev": 0.0,
                    "StopLoss": price * 0.95,
                    "NewsList": []
                })

            with shared_state.brain_lock:
                if tickers_to_scan:
                    shared_state.DYNAMIC_WATCHLIST = tickers_to_scan.copy()
                
                shared_state.MASTER_BRAIN["leaderboard"] = valid_results
                for item in valid_results:
                    sym = item["Code"]
                    old_news = shared_state.MASTER_BRAIN["details"].get(sym, {}).get("NewsList", [])
                    item["NewsList"] = old_news
                    shared_state.MASTER_BRAIN["details"][sym] = item
                
                shared_state.MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
                shared_state.TV_LOGIN_STATUS = "✅ 雷達全開 (24H 鋼鐵防線版)"

        except Exception as e:
            print(f"⚠️ [SCANNER] 迴圈錯誤: {e}")
            
        time.sleep(2) 

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()