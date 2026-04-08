import time, threading, json, math, os, random
from datetime import datetime
import pandas as pd
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import pytz
from tvDatafeed import TvDatafeed, Interval
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template

# ==========================================
# 🛠️ 雲端環境變數設定區
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
SCAN_INTERVAL = 15
PORT = int(os.getenv('PORT', 5000))
# ==========================================

MASTER_BRAIN = {
    "surge": [], "details": {}, "last_update": "", 
    "system_alert": "", "system_alert_ts": 0
}
WATCHLIST = ["AAPL", "TSLA", "NVDA", "AMD", "META"] # 測試名單
cooldown_tracker = {} # 記錄音效冷卻時間

app = Flask(__name__)

# --- 📰 新聞獲取模組 (背景執行) ---
def fetch_news_bg(ticker, cell):
    try:
        tz_ny = pytz.timezone('America/New_York')
        now_ny = datetime.now(tz_ny)
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []
            for item in root.findall('./channel/item')[:5]:
                raw_t = item.find('title').text
                link = item.find('link').text
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(tz_ny)
                if (now_ny.date() - dt.date()).days > 4: continue
                is_today = (dt.date() == now_ny.date())
                t_str = f"今日 {dt.strftime('%H:%M')}" if is_today else dt.strftime("%m/%d %H:%M")
                articles.append({"title": raw_t, "link": link, "time": t_str, "is_today": is_today})
            cell["NewsList"] = articles
    except: pass

# --- 🔌 數據獲取與流動性分析 ---
def safe_get_tw_data(tv, symbol):
    for i in range(3):
        try:
            df = tv.get_hist(symbol=symbol, exchange='NASDAQ', interval=Interval.in_1_minute, n_bars=20)
            if df is not None and not df.empty:
                last_time = df.index[-1]
                if last_time.tz is None: last_time = last_time.tz_localize('UTC')
                # 90秒延遲鎖：防止拿到殭屍資料
                if (datetime.now(last_time.tzinfo) - last_time).total_seconds() > 90:
                    raise ValueError("數據延遲過高")
                return df
            raise ValueError("數據為空")
        except Exception as e:
            time.sleep((2 ** i) + random.random())
    return None

def analyze_liquidity(df, threshold=5000): # 盤前門檻暫降為 5000
    if df is None or df.empty: return False, 0
    if df.index.tz is None: df.index = df.index.tz_localize('UTC')
    
    # 時間鎖解開至盤前 04:00
    df_market = df.tz_convert('US/Eastern').between_time('04:00', '16:00')
    if df_market.empty or df_market['volume'].iloc[-1] <= 0: return False, 0

    df_market['dollar_vol'] = df_market['close'] * df_market['volume']
    avg_vol = df_market['dollar_vol'].rolling(window=5, min_periods=1).mean().iloc[-1]
    return avg_vol >= threshold, avg_vol

# --- 🧠 戰術雷達主引擎 ---
def scanner_engine():
    global cooldown_tracker
    print("🔄 啟動 Sniper V19.8 (雲端聲學與盤前版)...")
    try:
        tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
        print("✅ TradingView 數據源準備就緒！")
    except Exception as e:
        print(f"❌ TW 連線失敗: {e}")
        MASTER_BRAIN["system_alert"] = "system_down"
        MASTER_BRAIN["system_alert_ts"] = time.time()
        return

    fail_count = 0

    while True:
        try:
            now_ts = time.time()
            batch_success = False

            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_ticker = {executor.submit(safe_get_tw_data, tv, ticker): ticker for ticker in WATCHLIST}
                
                for future in future_to_ticker:
                    ticker = future_to_ticker[future]
                    df = future.result()
                    
                    if df is not None:
                        batch_success = True
                        fail_count = 0
                        is_liquid, avg_vol = analyze_liquidity(df)
                        p = float(df['close'].iloc[-1])
                        stop_loss = float(df['low'].tail(3).min() - 0.05)
                        
                        # 模擬訊號
                        is_spark = random.random() > 0.85 and is_liquid 
                        is_diamond = random.random() > 0.90 and is_liquid and not is_spark
                        mock_float = random.uniform(1.0, 10.0)
                        
                        tag = ""
                        audio_trigger = ""
                        
                        # 音效排程與 30 秒冷卻邏輯
                        last_audio_ts = cooldown_tracker.get(ticker, 0)
                        if (is_spark or is_diamond) and (now_ts - last_audio_ts > 30):
                            if is_spark and mock_float < 2.0:
                                audio_trigger = "nova"
                            elif is_spark:
                                audio_trigger = "spark"
                            elif is_diamond:
                                audio_trigger = "diamond"
                            cooldown_tracker[ticker] = now_ts
                            
                            tag = "🔥強力點火" if is_spark else "💎支撐回踩"

                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                        cell.update({
                            "PriceStr": f"${p:.2f}", "PriceVal": p, "StopLossVal": stop_loss,
                            "Volume": f"{avg_vol/1000:.1f}K", "FloatStr": f"{mock_float:.1f}M",
                            "Change": "+5.50%", "Signal": tag
                        })

                        if tag:
                            item = {
                                "Code": ticker, "Price": f"${p:.2f}", "Streak": tag, 
                                "FloatStr": f"{mock_float:.1f}M", 
                                "AudioTrigger": audio_trigger, "SignalTS": now_ts # 前端播放依據
                            }
                            MASTER_BRAIN["surge"].insert(0, item)
                        
                        threading.Thread(target=fetch_news_bg, args=(ticker, cell)).start()

            if not batch_success:
                fail_count += 1
                if fail_count >= 3:
                    MASTER_BRAIN["system_alert"] = "system_down"
                    MASTER_BRAIN["system_alert_ts"] = time.time()

            MASTER_BRAIN["surge"] = MASTER_BRAIN["surge"][:50]
            MASTER_BRAIN["last_update"] = datetime.now().strftime('%H:%M:%S')
            
            time.sleep(max(5, SCAN_INTERVAL + random.uniform(-3, 3)))
            
        except Exception as e:
            time.sleep(5)

# --- 🌐 Web API 端點 ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, use_reloader=False)