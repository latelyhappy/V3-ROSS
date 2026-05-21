import time
import threading
from datetime import datetime
import pytz
import yfinance as yf

# 🚀 匯入中央狀態與工具
import shared_state
from config import TZ_NY, TZ_TW
from utils import safe_float, calc_hma

# 🧠 靜態快取：避免每幾秒就去戳 yfinance 導致 API 限流被 Ban
INFO_CACHE = {}

def format_volume(v):
    """將大數字格式化為 K 或 M"""
    if v >= 1000000: return f"{v/1000000:.2f}M"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v))

def update_scanner_loop():
    print("🚀 [SCANNER] 啟動全域盤前選股引擎 (yFinance 真實籌碼版)...")
    tickers_to_scan = ["ATPC", "ILLR", "STFS", "LIMN", "NCPL", "CXAI", "VIDA", "JUNS", "TSLA", "NVDA", "AAPL", "AMD"]

    while True:
        try:
            def scan_single_ticker(ticker):
                try:
                    # 1. 取得歷史 K 線 (1分鐘，包含盤前)
                    stock = yf.Ticker(ticker)
                    df = stock.history(period="1d", interval="1m", prepost=True)
                    if df is None or df.empty: return None

                    df.columns = [c.lower() for c in df.columns]
                    last_row = df.iloc[-1]
                    close_price = safe_float(last_row['close'])
                    
                    # 2. 真實成交量計算
                    volume = safe_float(df['volume'].sum())

                    # 3. 取得真實基本面與籌碼 (加入快取機制)
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
                    
                    # 4. 運算即時量比 (相對平均日量)
                    rel_vol = (volume / (cache['avg_vol'] / 390)) if cache['avg_vol'] > 0 else 0.0
                    rel_vol_str = f"{rel_vol:.1f}x" if rel_vol > 0 else "-"

                    # 5. 指標與漲幅運算
                    hma_series = calc_hma(df['close'], 9)
                    last_hma = safe_float(hma_series.iloc[-1]) if len(hma_series) > 0 else close_price
                    ema9_dev = ((close_price - last_hma) / last_hma * 100) if last_hma > 0 else 0.0
                    
                    first_price = safe_float(df['close'].iloc[0])
                    pct_val = ((close_price - first_price) / first_price * 100) if first_price > 0 else 0.0

                    stock_info = {
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
                        "RelVol": rel_vol_str,
                        "EMA9_Dev": ema9_dev,
                        "VWAP_Dev": 0.0,
                        "StopLoss": close_price * 0.95,
                        "NewsList": []
                    }
                    return stock_info
                except Exception as e:
                    return None

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

            with shared_state.brain_lock:
                shared_state.MASTER_BRAIN["leaderboard"] = valid_results
                for item in valid_results:
                    sym = item["Code"]
                    old_news = shared_state.MASTER_BRAIN["details"].get(sym, {}).get("NewsList", [])
                    item["NewsList"] = old_news
                    shared_state.MASTER_BRAIN["details"][sym] = item
                
                shared_state.MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
                shared_state.TV_LOGIN_STATUS = "✅ 雷達全開 (yFinance 真實籌碼版)"

        except Exception as e:
            pass
        time.sleep(10) # 🚀 放寬至 10 秒刷新一次，保護 IP 不被 Yahoo 封鎖

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()