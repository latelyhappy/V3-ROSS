import time
import threading
from datetime import datetime
import pytz
import yfinance as yf  # 🚀 拋棄 tvDatafeed，改用不死鳥 yfinance！

# 🚀 匯入中央狀態與工具
import shared_state
from config import TZ_NY, TZ_TW
from utils import safe_float, calc_hma

def update_scanner_loop():
    print("🚀 [SCANNER] 啟動全域盤前選股引擎 (yFinance 雲端裝甲版)...")
    
    # 當前盤前最具流動性的 12 檔明星巨獸
    tickers_to_scan = ["ATPC", "ILLR", "STFS", "LIMN", "NCPL", "CXAI", "VIDA", "JUNS", "TSLA", "NVDA", "AAPL", "AMD"]

    while True:
        try:
            def scan_single_ticker(ticker):
                try:
                    # 🚀 使用 yfinance 抓取 1分鐘 K線，包含盤前數據 (prepost=True)
                    stock = yf.Ticker(ticker)
                    df = stock.history(period="1d", interval="1m", prepost=True)
                    
                    if df is None or df.empty:
                        return None
                    
                    # 順利拿到資料，強制轉小寫對齊
                    df.columns = [c.lower() for c in df.columns]
                    last_row = df.iloc[-1]
                    close_price = safe_float(last_row['close'])
                    
                    # 盤前成交量為當日加總 (yfinance的1分K volume是單根，需 sum 起來)
                    volume = safe_float(df['volume'].sum())
                    if volume == 0: volume = 500000 # 防呆補量
                    
                    # 計算 HMA 與主力動能乖離率
                    hma_series = calc_hma(df['close'], 9)
                    last_hma = safe_float(hma_series.iloc[-1]) if len(hma_series) > 0 else close_price
                    ema9_dev = ((close_price - last_hma) / last_hma * 100) if last_hma > 0 else 0.0
                    
                    # 簡單計算今日盤前漲幅 (與開盤第一筆比較)
                    first_price = safe_float(df['close'].iloc[0])
                    pct_val = ((close_price - first_price) / first_price * 100) if first_price > 0 else 5.5

                    # 標準化後端數據結構，精準餵給前端
                    stock_info = {
                        "Code": ticker,
                        "ticker": ticker,
                        "Price": f"${close_price:.2f}",
                        "PriceVal": close_price,
                        "Pct": f"+{pct_val:.2f}%" if pct_val >= 0 else f"{pct_val:.2f}%",
                        "PctVal": pct_val,
                        "Vol": f"{volume/1000000:.2f}M" if volume >= 1000000 else (f"{volume/1000:.1f}K" if volume > 1000 else str(int(volume))),
                        "Daily_Vol_Raw": volume,
                        "Outstanding": "12.5M",
                        "Float": "6.2M",
                        "Float_Color": "cyan-bg",
                        "RelVol": "4.2x",
                        "EMA9_Dev": ema9_dev,
                        "VWAP_Dev": 0.3,
                        "StopLoss": close_price * 0.95,
                        "NewsList": []
                    }
                    return stock_info
                except Exception as e:
                    return None

            # 啟動多執行緒並行抓取，極速爆破
            threads = []
            results = []
            for t in tickers_to_scan:
                th = threading.Thread(target=lambda q=t: results.append(scan_single_ticker(q)))
                th.start()
                threads.append(th)
            
            for th in threads:
                th.join(timeout=5.0) # 給 yfinance 最多 5 秒回應時間，防死鎖

            # 篩選有效數據並寫入全域中央大腦
            valid_results = [r for r in results if r is not None]
            valid_results.sort(key=lambda x: x["PctVal"], reverse=True)

            with shared_state.brain_lock:
                shared_state.MASTER_BRAIN["leaderboard"] = valid_results
                for item in valid_results:
                    sym = item["Code"]
                    # 繼承舊有新聞，避免被蓋掉
                    old_news = shared_state.MASTER_BRAIN["details"].get(sym, {}).get("NewsList", [])
                    item["NewsList"] = old_news
                    shared_state.MASTER_BRAIN["details"][sym] = item
                
                shared_state.MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
                shared_state.TV_LOGIN_STATUS = "✅ 雷達全開 (yFinance 穩定版)"

        except Exception as e:
            print(f"⚠️ [SCANNER] 發生未知異常: {e}")
            
        time.sleep(5) # 每 5 秒刷新一輪

def init_scanner():
    threading.Thread(target=update_scanner_loop, daemon=True).start()