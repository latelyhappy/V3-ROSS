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
from utils import safe_float, calc_hma

INFO_CACHE = {}

def format_volume(v):
    if v >= 1000000: return f"{v/1000000:.2f}M"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v))

def update_scanner_loop():
    print("🚀 [SCANNER] 啟動 TV 索敵 + yFinance 鑑定的終極複合引擎...")
    
    url = "https://scanner.tradingview.com/america/scan"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json"
    }

    while True:
        try:
            now_ny = datetime.now(TZ_NY)
            is_premarket = now_ny.hour < 9 or (now_ny.hour == 9 and now_ny.minute < 30)
            sort_col = "premarket_change" if is_premarket else "change"

            # 🚀 第一波：利用 TV 官方 API 取得「真正漲幅最高」的名單 (最簡 Payload，絕不報錯)
            payload = {
                "filter": [
                    {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
                    {"left": "close", "operation": "in_range", "right": [0.5, 50.0]}
                ],
                "options": {"lang": "en"},
                "markets": ["america"],
                "symbols": {"query": {"types": []}, "tickers": []},
                "columns": ["name"], # 只要名字，不索求複雜欄位，保證 100% 成功
                "sort": {"sortBy": sort_col, "sortOrder": "desc"},
                "range": [0, 20] # 抓取市場前 20 名強勢股
            }

            response = requests.post(url, json=payload, headers=headers, timeout=10)
            tickers_to_scan = []
            
            if response.status_code == 200:
                data = response.json()
                for item in data.get("data", []):
                    sym = item["d"][0]
                    # TV 回傳格式如 "NASDAQ:ATPC", 這裡切出純代碼 "ATPC"
                    if ":" in sym: sym = sym.split(":")[1]
                    tickers_to_scan.append(sym)
            else:
                print(f"⚠️ TV 掃描器無回應，啟用備援名單。")
                tickers_to_scan = ["ATPC", "ILLR", "STFS", "LIMN", "NCPL", "TSLA", "NVDA", "AAPL"]

            if not tickers_to_scan:
                tickers_to_scan = ["TSLA", "NVDA", "AAPL"]

            # 🚀 第二波：將名單交由 yFinance 取得「真實量能」與「真實籌碼」
            def scan_single_ticker(ticker):
                try:
                    stock = yf.Ticker(ticker)
                    df = stock.history(period="1d", interval="1m", prepost=True)
                    if df is None or df.empty: return None

                    df.columns = [c.lower() for c in df.columns]
                    last_row = df.iloc[-1]
                    close_price = safe_float(last_row['close'])
                    
                    # 盤前真實累積成交量
                    volume = safe_float(df['volume'].sum())

                    if ticker not in INFO_CACHE:
                        try:
                            info = stock.info
                            out = safe_float(info.get('sharesOutstanding', 0))
                            flt = safe_float(info.get('floatShares', 0))
                            avg_vol = safe_float(info.get('averageVolume', 0))
                            INFO_CACHE[ticker] = {'out': out, 'flt': flt, 'avg_vol': avg_vol}
                        except:
                            INFO_CACHE[ticker] = {'out': 0, 'flt': 0, 'avg_vol': 0}
                    
                    cache = INFO_CACHE[ticker]
                    out_str = format_volume(cache['out']) if cache['out'] > 0 else "-"
                    flt_str = format_volume(cache['flt']) if cache['flt'] > 0 else "-"
                    
                    rel_vol = (volume / (cache['avg_vol'] / 390)) if cache['avg_vol'] > 0 else 0.0
                    
                    hma_series = calc_hma(df['close'], 9)
                    last_hma = safe_float(hma_series.iloc[-1]) if len(hma_series) > 0 else close_price
                    ema9_dev = ((close_price - last_hma) / last_hma * 100) if last_hma > 0 else 0.0
                    
                    first_price = safe_float(df['close'].iloc[0])
                    pct_val = ((close_price - first_price) / first_price * 100) if first_price > 0 else 0.0

                    return {
                        "Code": ticker,
                        "ticker": ticker,
                        "Price": f"${close_price:.2f}",
                        "PriceVal": close_price,
                        "Pct": f"+{pct_val:.2f}%" if pct_val >= 0 else f"{pct_val:.2f}%",
                        "PctVal": pct_val,
                        "Vol": format_volume(volume),
                        "Daily_Vol_Raw": volume,
                        "Outstanding": out_str,
                        "Float": flt_str,
                        "Float_Color": "cyan-bg" if (0 < cache['flt'] <= 20000000) else "gray-bg",
                        "RelVol": f"{rel_vol:.1f}x" if rel_vol > 0 else "-",
                        "EMA9_Dev": ema9_dev,
                        "VWAP_Dev": 0.0,
                        "StopLoss": close_price * 0.95,
                        "NewsList": []
                    }
                except:
                    return None

            # 多執行緒啟動
            threads = []
            results = []
            for t in tickers_to_scan:
                th = threading.Thread(target=lambda q=t: results.append(scan_single_ticker(q)))
                th.start()
                threads.append(th)
            
            for th in threads:
                th.join(timeout=8.0)

            valid_results = [r for r in results if r is not None]
            valid_results.sort(key=lambda x: x["PctVal"], reverse=True)

            # 寫入大腦記憶體
            with shared_state.brain_lock:
                shared_state.MASTER_BRAIN["leaderboard"] = valid_results
                for item in valid_results:
                    sym = item["Code"]
                    old_news = shared_state.MASTER_BRAIN["details"].get(sym, {}).get("NewsList", [])
                    item["NewsList"] = old_news
                    shared_state.MASTER_BRAIN["details"][sym] = item
                
                shared_state.MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
                shared_state.TV_LOGIN_STATUS = "✅ 雷達全開 (TV 索敵 + yF 鑑定)"

        except Exception as e:
            print(f"⚠️ [SCANNER] 迴圈發生例外錯誤: {e}")
            
        time.sleep(10)

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()