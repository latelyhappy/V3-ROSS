import time
import threading
import requests
from datetime import datetime
import pytz
import yfinance as yf
import warnings

# 🚀 強制消音 Pandas 警告
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', message='.*Timestamp.utcnow.*')

import shared_state
from config import TZ_NY, TZ_TW
from utils import safe_float

# 🧠 全域靜態快取與防阻塞鎖
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
    print("🚀 [SCANNER] 啟動動態階梯濾網 + Ross狙擊標靶判定...")
    
    url = "https://scanner.tradingview.com/america/scan"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json"
    }

    while True:
        try:
            now_ny = datetime.now(TZ_NY)
            is_real_premarket = (4 <= now_ny.hour < 9) or (now_ny.hour == 9 and now_ny.minute < 30)
            
            if is_real_premarket:
                sort_col = "premarket_change"
                vol_target_col = "premarket_volume"
            else:
                sort_col = "change"
                vol_target_col = "volume"

            # 🚀 第一層：TV API 初步放寬到 5 萬股，以防漏掉微型妖股
            payload = {
                "filter": [
                    {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
                    {"left": "close", "operation": "in_range", "right": [0.5, 50.0]},
                    {"left": vol_target_col, "operation": "egreater", "right": 50000} 
                ],
                "options": {"lang": "en"},
                "markets": ["america"],
                "symbols": {"query": {"types": []}, "tickers": []},
                "columns": ["name", "close", "change", "volume", "premarket_close", "premarket_change", "premarket_volume"], 
                "sort": {"sortBy": sort_col, "sortOrder": "desc"},
                "range": [0, 50] # 抓取多一點，讓後台動態洗牌
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

                if sym not in INFO_CACHE and sym not in FETCHING_CACHE:
                    FETCHING_CACHE.add(sym)
                    threading.Thread(target=fetch_yf_info, args=(sym,), daemon=True).start()
                
                cache = INFO_CACHE.get(sym, {'out': 0, 'flt': 0, 'avg_vol': 0, 'prev_c': 0})
                out_val, flt_val, avg_vol, prev_c = cache['out'], cache['flt'], cache['avg_vol'], cache['prev_c']

                # 🚀 第二層（動能核心）：動態流動性階梯過濾
                vol_threshold = 100000 # 預設門檻 10 萬股
                if flt_val > 0:
                    if flt_val <= 5000000:       # 流通 < 5M，門檻放寬為 5 萬股
                        vol_threshold = 50000
                    elif flt_val > 50000000:     # 流通 > 50M，門檻嚴格為 50 萬股
                        vol_threshold = 500000
                
                # 若未達動態門檻，視為雜訊，原地抹殺
                if vol < vol_threshold: continue

                tickers_to_scan.append(sym)
                
                out_str = format_volume(out_val) if out_val > 0 else "載入中"
                flt_str = format_volume(flt_val) if flt_val > 0 else "載入中"
                
                rel_vol = (vol / avg_vol) if avg_vol > 0 else 0.0
                gap_pct = ((price - prev_c) / prev_c * 100) if prev_c > 0 else 0.0
                
                # 🚀 第三層（戰術標靶）：Ross Cameron 第一層訊號鎖定！
                # 只要符合 跳空大於 5% 且 流通量低於 20M，就直接打上狙擊印記。
                is_sniper_target = (gap_pct >= 5.0 and 0 < flt_val <= 20000000)

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
                    "NewsList": [],
                    "IsSniperTarget": is_sniper_target # 寫入大腦，傳給火控雷達！
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
                shared_state.TV_LOGIN_STATUS = "✅ 階梯濾網 + Ross狙擊標靶"

        except Exception as e:
            print(f"⚠️ [SCANNER] 迴圈錯誤: {e}")
            
        time.sleep(2) 

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()