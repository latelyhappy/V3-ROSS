import time
import threading
import requests
from datetime import datetime
import pytz
import yfinance as yf
import warnings

# 🚀 強制消音 yfinance 底層的 Pandas 煩人警告
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', message='.*Timestamp.utcnow.*')

# 🚀 匯入中央狀態與工具
import shared_state
from config import TZ_NY, TZ_TW
from utils import safe_float

INFO_CACHE = {}

def format_volume(v):
    if v >= 1000000: return f"{v/1000000:.2f}M"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v))

def update_scanner_loop():
    print("🚀 [SCANNER] 啟動 TV 量能 + yF 籌碼完美複合引擎...")
    
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

            # 🚀 第一波：利用 TV 官方 API 取得「代碼、現價、真實成交量」
            payload = {
                "filter": [
                    {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
                    {"left": "close", "operation": "in_range", "right": [0.5, 50.0]}
                ],
                "options": {"lang": "en"},
                "markets": ["america"],
                "symbols": {"query": {"types": []}, "tickers": []},
                # 精確鎖定這 7 個基礎欄位，TV 絕對不會拒絕！
                "columns": ["name", "close", "change", "volume", "premarket_close", "premarket_change", "premarket_volume"], 
                "sort": {"sortBy": sort_col, "sortOrder": "desc"},
                "range": [0, 25] # 抓取市場前 25 名強勢股
            }

            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            valid_results = []
            tickers_to_scan = []

            if response.status_code == 200:
                data = response.json()
                for item in data.get("data", []):
                    d = item.get("d", [])
                    if len(d) < 7: continue

                    sym = d[0].split(":")[1] if ":" in d[0] else d[0]
                    reg_close, reg_change, reg_vol = safe_float(d[1]), safe_float(d[2]), safe_float(d[3])
                    pre_close, pre_change, pre_vol = safe_float(d[4]), safe_float(d[5]), safe_float(d[6])

                    # 根據時段切換價格與量能來源
                    if is_premarket:
                        price = pre_close if pre_close > 0 else reg_close
                        pct = pre_change if pre_close > 0 else reg_change
                        vol = pre_vol
                    else:
                        price = reg_close
                        pct = reg_change
                        vol = reg_vol

                    # 過濾殭屍股 (無量剔除)
                    if vol < 100: continue

                    tickers_to_scan.append(sym)
                    valid_results.append({
                        "Code": sym,
                        "ticker": sym,
                        "PriceVal": price,
                        "PctVal": pct,
                        "Daily_Vol_Raw": vol,
                        "Price": f"${price:.2f}",
                        "Pct": f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%",
                        "Vol": format_volume(vol)
                    })

            # 🚀 致命修復 1：強制更新全域雷達名單，喚醒 Alpaca 與 新聞情報引擎！
            with shared_state.brain_lock:
                if tickers_to_scan:
                    shared_state.DYNAMIC_WATCHLIST = tickers_to_scan.copy()

            # 🚀 第二波：讓 yFinance 只負責查「發行量、流通股、10日均量」，絕對不碰價格跟成交量！
            def fetch_yf_info(stock_item):
                sym = stock_item["Code"]
                if sym not in INFO_CACHE:
                    try:
                        info = yf.Ticker(sym).info
                        INFO_CACHE[sym] = {
                            'out': safe_float(info.get('sharesOutstanding', 0)),
                            'flt': safe_float(info.get('floatShares', 0)),
                            'avg_vol': safe_float(info.get('averageVolume', 0))
                        }
                    except:
                        INFO_CACHE[sym] = {'out': 0, 'flt': 0, 'avg_vol': 0}
                
                cache = INFO_CACHE[sym]
                out_val = cache['out']
                flt_val = cache['flt']
                avg_vol = cache['avg_vol']

                # 組合剩餘靜態籌碼數據
                stock_item["Outstanding"] = format_volume(out_val) if out_val > 0 else "-"
                stock_item["Float"] = format_volume(flt_val) if flt_val > 0 else "-"
                stock_item["Float_Color"] = "cyan-bg" if (0 < flt_val <= 20000000) else "gray-bg"
                
                # 計算即時量比 (TV 真實今日量 / yF 平均日量換算的每分鐘基準)
                vol = stock_item["Daily_Vol_Raw"]
                rel_vol = (vol / (avg_vol / 390)) if avg_vol > 0 else 0.0
                stock_item["RelVol"] = f"{rel_vol:.1f}x" if rel_vol > 0 else "-"
                
                # 基礎防呆參數 (Alpaca 會覆寫真實動態運算)
                stock_item["EMA9_Dev"] = 0.0 
                stock_item["VWAP_Dev"] = 0.0
                stock_item["StopLoss"] = stock_item["PriceVal"] * 0.95
                stock_item["NewsList"] = []

            # 多執行緒查籌碼 (極速 3 秒脫離防卡死)
            threads = []
            for item in valid_results:
                th = threading.Thread(target=fetch_yf_info, args=(item,))
                th.start()
                threads.append(th)
            for th in threads:
                th.join(timeout=3.0)

            # 寫入大腦記憶體
            with shared_state.brain_lock:
                shared_state.MASTER_BRAIN["leaderboard"] = valid_results
                for item in valid_results:
                    sym = item["Code"]
                    # 保留新聞不被洗掉
                    old_news = shared_state.MASTER_BRAIN["details"].get(sym, {}).get("NewsList", [])
                    item["NewsList"] = old_news
                    shared_state.MASTER_BRAIN["details"][sym] = item
                
                shared_state.MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
                shared_state.TV_LOGIN_STATUS = "✅ 雷達全開 (TV 量能 + yF 籌碼)"

        except Exception as e:
            print(f"⚠️ [SCANNER] 迴圈發生錯誤: {e}")
            
        time.sleep(10)

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()