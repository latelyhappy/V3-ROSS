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
    """背景非同步抓取籌碼與昨日收盤價，絕不阻塞 2 秒報價迴圈！"""
    try:
        info = yf.Ticker(sym).info
        INFO_CACHE[sym] = {
            'out': safe_float(info.get('sharesOutstanding', 0)),
            'flt': safe_float(info.get('floatShares', 0)),
            'avg_vol': safe_float(info.get('averageVolume', 0)),
            'prev_c': safe_float(info.get('previousClose', 0)) # 🚀 抓取昨收價用來算跳空
        }
    except:
        if sym not in INFO_CACHE:
            INFO_CACHE[sym] = {'out': 0, 'flt': 0, 'avg_vol': 0, 'prev_c': 0}
    finally:
        if sym in FETCHING_CACHE:
            FETCHING_CACHE.remove(sym)

def update_scanner_loop():
    print("🚀 [SCANNER] 啟動 TV極速報價 + yF背景籌碼跳空 (修復白畫面穩定版)...")
    
    url = "https://scanner.tradingview.com/america/scan"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json"
    }

    while True:
        try:
            now_ny = datetime.now(TZ_NY)
            is_premarket = now_ny.hour < 9 or (now_ny.hour == 9 and now_ny.minute < 30)
            
            sort_col = "premarket_change" if is_premarket else "change"
            vol_target_col = "premarket_volume" if is_premarket else "volume"

            # 🚀 退回 100% 安全的 7 大基礎欄位，保證 TV 絕對不會擋人！
            payload = {
                "filter": [
                    {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
                    {"left": "close", "operation": "in_range", "right": [0.5, 50.0]},
                    {"left": vol_target_col, "operation": "egreater", "right": 100000} # > 100K 股
                ],
                "options": {"lang": "en"},
                "markets": ["america"],
                "symbols": {"query": {"types": []}, "tickers": []},
                "columns": ["name", "close", "change", "volume", "premarket_close", "premarket_change", "premarket_volume"], 
                "sort": {"sortBy": sort_col, "sortOrder": "desc"},
                "range": [0, 30] 
            }

            response = requests.post(url, json=payload, headers=headers, timeout=5)
            
            # 增加錯誤攔截印出，防範靜默當機
            if response.status_code != 200:
                print(f"⚠️ TV 伺服器拒絕請求: {response.status_code} - {response.text}")
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

                if is_premarket:
                    price = pre_close if pre_close > 0 else reg_close
                    pct = pre_change if pre_close > 0 else reg_change
                    vol = pre_vol
                else:
                    price = reg_close
                    pct = reg_change
                    vol = reg_vol

                # 確保只抓流動性 > 100K 的標的
                if vol < 100000: continue

                tickers_to_scan.append(sym)
                
                # 🚀 發現新股票時，發射背景執行緒去查籌碼 (完全不卡 2 秒報價)
                if sym not in INFO_CACHE and sym not in FETCHING_CACHE:
                    FETCHING_CACHE.add(sym)
                    threading.Thread(target=fetch_yf_info, args=(sym,), daemon=True).start()
                
                # 立刻使用目前快取
                cache = INFO_CACHE.get(sym, {'out': 0, 'flt': 0, 'avg_vol': 0, 'prev_c': 0})
                out_val, flt_val, avg_vol, prev_c = cache['out'], cache['flt'], cache['avg_vol'], cache['prev_c']

                out_str = format_volume(out_val) if out_val > 0 else "載入中"
                flt_str = format_volume(flt_val) if flt_val > 0 else "載入中"
                
                # 🚀 極速動態運算：即時量比 & 動態跳空
                rel_vol = (vol / avg_vol) if avg_vol > 0 else 0.0
                gap_pct = ((price - prev_c) / prev_c * 100) if prev_c > 0 else 0.0
                
                valid_results.append({
                    "Code": sym,
                    "ticker": sym,
                    "PriceVal": price,
                    "PctVal": pct,
                    "Daily_Vol_Raw": vol,
                    "GapPct": gap_pct, # 傳送跳空數值給前端
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
                # 繼承舊有新聞
                for item in valid_results:
                    sym = item["Code"]
                    old_news = shared_state.MASTER_BRAIN["details"].get(sym, {}).get("NewsList", [])
                    item["NewsList"] = old_news
                    shared_state.MASTER_BRAIN["details"][sym] = item
                
                shared_state.MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
                shared_state.TV_LOGIN_STATUS = "✅ 雷達全開 (TV極速報價 + yF背景籌碼)"

        except Exception as e:
            print(f"⚠️ [SCANNER] 迴圈錯誤: {e}")
            
        time.sleep(2) 

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()