import time
import threading
import requests
from datetime import datetime
import pytz

import shared_state
from config import TZ_NY, TZ_TW
from utils import safe_float

def format_volume(v):
    if v >= 1000000: return f"{v/1000000:.2f}M"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v))

def update_scanner_loop():
    print("🚀 [SCANNER] 啟動 100% TV 官方純化直連引擎 (量能下限 > 1000)...")
    
    url = "https://scanner.tradingview.com/america/scan"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json"
    }

    while True:
        try:
            now_ny = datetime.now(TZ_NY)
            is_premarket = now_ny.hour < 9 or (now_ny.hour == 9 and now_ny.minute < 30)
            
            # 根據盤前或盤中決定排序與量能過濾欄位
            sort_col = "premarket_change" if is_premarket else "change"
            vol_target_col = "premarket_volume" if is_premarket else "volume"
            gap_col_name = "premarket_gap" if is_premarket else "gap"

            # 🚀 構建 TV 官方最精準的全量能、全籌碼篩選封包 (成交量 > 1000)
            payload = {
                "filter": [
                    {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
                    {"left": "close", "operation": "in_range", "right": [0.5, 50.0]},
                    {"left": vol_target_col, "operation": "egreater", "right": 1000} # 🚀 指令 4：成交量硬性大於 1000
                ],
                "options": {"lang": "en"},
                "markets": ["america"],
                "symbols": {"query": {"types": []}, "tickers": []},
                "columns": [
                    "name", "close", "change", "volume", 
                    "premarket_close", "premarket_change", "premarket_volume",
                    "total_shares_outstanding", "float_shares_outstanding", "average_volume_10d_calc",
                    gap_col_name
                ], 
                "sort": {"sortBy": sort_col, "sortOrder": "desc"},
                "range": [0, 30] 
            }

            response = requests.post(url, json=payload, headers=headers, timeout=5)
            
            valid_results = []
            tickers_to_scan = []

            if response.status_code == 200:
                data = response.json()
                for item in data.get("data", []):
                    d = item.get("d", [])
                    if len(d) < 11: continue

                    sym = d[0].split(":")[1] if ":" in d[0] else d[0]
                    reg_close, reg_change, reg_vol = safe_float(d[1]), safe_float(d[2]), safe_float(d[3])
                    pre_close, pre_change, pre_vol = safe_float(d[4]), safe_float(d[5]), safe_float(d[6])
                    
                    # 🚀 指令 5 修復：直接讀取交易所申報的即時真實籌碼，100% 零偏差！
                    total_shares = safe_float(d[7])
                    float_shares = safe_float(d[8])
                    avg_vol = safe_float(d[9])
                    raw_gap = safe_float(d[10]) # 🚀 指令 3 & 4：抓取官方跳空

                    if is_premarket:
                        price = pre_close if pre_close > 0 else reg_close
                        pct = pre_change if pre_close > 0 else reg_change
                        vol = pre_vol
                    else:
                        price = reg_close
                        pct = reg_change
                        vol = reg_vol

                    # 後台二次硬性攔截過濾
                    if vol < 1000: continue

                    tickers_to_scan.append(sym)
                    
                    # 量比公式精準修正
                    rel_vol = (vol / avg_vol) if avg_vol > 0 else 0.0
                    
                    valid_results.append({
                        "Code": sym,
                        "ticker": sym,
                        "PriceVal": price,
                        "PctVal": pct,
                        "Daily_Vol_Raw": vol,
                        "GapPct": raw_gap, # 🚀 丟給前端渲染
                        "Price": f"${price:.2f}",
                        "Pct": f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%",
                        "Vol": format_volume(vol),
                        "Outstanding": format_volume(total_shares) if total_shares > 0 else "-",
                        "Float": format_volume(float_shares) if float_shares > 0 else "-",
                        "Float_Color": "cyan-bg" if (0 < float_shares <= 20000000) else "gray-bg",
                        "RelVol": f"{rel_vol:.2f}x" if rel_vol > 0 else "-",
                        "EMA9_Dev": 0.0,
                        "VWAP_Dev": 0.0,
                        "StopLoss": price * 0.95,
                        "NewsList": []
                    })

            # 強制更新全域雷達名單 (不再卡死延遲)
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
                shared_state.TV_LOGIN_STATUS = "✅ 雷達全開 (TV 直連純化版)"

        except Exception as e:
            print(f"⚠️ [SCANNER] 迴圈錯誤: {e}")
            
        time.sleep(2) # 🚀 2秒極速無阻塞空轉

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()