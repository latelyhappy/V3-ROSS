import time
import threading
import requests
from datetime import datetime
import pytz

# 🚀 匯入中央狀態與工具
import shared_state
from config import TZ_NY, TZ_TW
from utils import safe_float

def format_volume(v):
    """將大數字格式化為 K 或 M"""
    if v >= 1000000: return f"{v/1000000:.2f}M"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v))

def update_scanner_loop():
    print("🚀 [SCANNER] 啟動 TradingView 官方直連篩選引擎 (極速無阻擋版)...")
    
    # TV 官方掃描器 API 接口
    url = "https://scanner.tradingview.com/america/scan"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    while True:
        try:
            # 1. 動態判斷是否為盤前 (美東 04:00 - 09:30)
            now_ny = datetime.now(TZ_NY)
            is_premarket = now_ny.hour < 9 or (now_ny.hour == 9 and now_ny.minute < 30)
            
            # 決定排序依據
            sort_col = "premarket_change" if is_premarket else "change"

            # 2. 構建 TV 官方篩選器封包
            payload = {
                "filter": [
                    {"left": "type", "operation": "in_range", "right": ["stock", "dr", "fund"]},
                    {"left": "close", "operation": "egreater", "right": 0.5},
                    {"left": "close", "operation": "eless", "right": 50.0}
                ],
                "options": {"lang": "en"},
                "markets": ["america"],
                "symbols": {"query": {"types": []}, "tickers": []},
                "columns": [
                    "name", "close", "change", "volume",
                    "premarket_close", "premarket_change", "premarket_volume",
                    "average_volume_10d_calc", "float_shares_outstanding", "total_shares_outstanding",
                    "VWAP", "EMA10"
                ],
                "sort": {"sortBy": sort_col, "sortOrder": "desc"},
                "range": [0, 50] # 直接抓取全市場前 50 名
            }

            # 3. 發送 API 請求
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                valid_results = []
                
                # 4. 解析 TV 回傳的真實數據
                for item in data.get("data", []):
                    d = item.get("d", [])
                    if len(d) < 12: continue
                    
                    ticker = d[0]
                    reg_close, reg_change, reg_vol = safe_float(d[1]), safe_float(d[2]), safe_float(d[3])
                    pre_close, pre_change, pre_vol = safe_float(d[4]), safe_float(d[5]), safe_float(d[6])
                    
                    avg_vol = safe_float(d[7])
                    float_shares = safe_float(d[8])
                    total_shares = safe_float(d[9])
                    vwap = safe_float(d[10])
                    ema10 = safe_float(d[11])

                    # 根據盤前或盤中，採用對應數值
                    if is_premarket:
                        price = pre_close if pre_close > 0 else reg_close
                        pct = pre_change if pre_close > 0 else reg_change
                        vol = pre_vol
                    else:
                        price = reg_close
                        pct = reg_change
                        vol = reg_vol

                    # 過濾掉完全沒量的殭屍股
                    if vol < 100: continue

                    rel_vol = (vol / (avg_vol / 390)) if avg_vol > 0 else 0.0
                    ema_dev = ((price - ema10) / ema10 * 100) if ema10 > 0 else 0.0
                    vwap_dev = ((price - vwap) / vwap * 100) if vwap > 0 else 0.0

                    stock_info = {
                        "Code": ticker,
                        "ticker": ticker,
                        "Price": f"${price:.2f}",
                        "PriceVal": price,
                        "Pct": f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%",
                        "PctVal": pct,
                        "Vol": format_volume(vol),
                        "Daily_Vol_Raw": vol,
                        "Outstanding": format_volume(total_shares) if total_shares > 0 else "-",
                        "Float": format_volume(float_shares) if float_shares > 0 else "-",
                        "Float_Color": "cyan-bg" if (0 < float_shares <= 20000000) else "gray-bg",
                        "RelVol": f"{rel_vol:.1f}x" if rel_vol > 0 else "-",
                        "EMA9_Dev": ema_dev,
                        "VWAP_Dev": vwap_dev,
                        "StopLoss": price * 0.95,
                        "NewsList": []
                    }
                    valid_results.append(stock_info)

                # 5. 寫入全域中央大腦
                with shared_state.brain_lock:
                    shared_state.MASTER_BRAIN["leaderboard"] = valid_results
                    for item in valid_results:
                        sym = item["Code"]
                        old_news = shared_state.MASTER_BRAIN["details"].get(sym, {}).get("NewsList", [])
                        item["NewsList"] = old_news
                        shared_state.MASTER_BRAIN["details"][sym] = item
                    
                    shared_state.MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
                    shared_state.TV_LOGIN_STATUS = "✅ 雷達全開 (TV 直連版)"

            else:
                print(f"⚠️ [SCANNER] TV API 回應異常: {response.status_code}")

        except Exception as e:
            print(f"⚠️ [SCANNER] 迴圈異常: {e}")
            
        time.sleep(10) # 10 秒刷新一次，保護 IP 不被封鎖

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()