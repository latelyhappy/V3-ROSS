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
from deep_translator import GoogleTranslator

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
# 建議指揮官盤前可以先觀察這幾檔熱門股
WATCHLIST = ["AAPL", "TSLA", "NVDA", "AMD", "META", "MSFT", "GOOGL", "MARA", "RIOT"] 
cooldown_tracker = {}

app = Flask(__name__)

# --- 📰 新聞獲取 + 繁體翻譯 ---
def fetch_news_bg(ticker, cell):
    try:
        tz_ny = pytz.timezone('America/New_York')
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []
            translator = GoogleTranslator(source='auto', target='zh-TW')
            for item in root.findall('./channel/item')[:5]:
                raw_t = item.find('title').text
                try:
                    zh_title = translator.translate(raw_t)
                except:
                    zh_title = raw_t 
                link = item.find('link').text
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(tz_ny)
                t_str = dt.strftime("%m/%d %H:%M")
                articles.append({"title": zh_title, "link": link, "time": t_str})
            cell["NewsList"] = articles
    except: pass

# --- 🔌 數據獲取 (盤前強化版) ---
def safe_get_tw_data(tv, symbol):
    for i in range(3):
        try:
            # 💡 extended_session=True 才能抓盤前資料
            df = tv.get_hist(symbol=symbol, exchange='NASDAQ', interval=Interval.in_1_minute, n_bars=30, extended_session=True)
            if df is not None and not df.empty:
                last_time = df.index[-1]
                if last_time.tz is None: last_time = last_time.tz_localize('UTC')
                delay = (datetime.now(last_time.tzinfo) - last_time).total_seconds()
                # 💡 盤前成交稀疏，延遲容許放寬到 600 秒 (10分鐘)
                if delay > 600: raise ValueError(f"延遲 {delay}秒")
                return df
            raise ValueError("無數據")
        except:
            time.sleep((2 ** i) + random.random())
    return None

def analyze_liquidity(df, threshold=2000): # 盤前門檻設低一點
    if df is None or df.empty: return False, 0
    if df.index.tz is None: df.index = df.index.tz_localize('UTC')
    df_market = df.tz_convert('US/Eastern').between_time('04:00', '20:00') # 涵蓋盤前盤後
    if df_market.empty or df_market['volume'].iloc[-1] <= 0: return False, 0
    df_market['dollar_vol'] = df_market['close'] * df_market['volume']
    avg_vol = df_market['dollar_vol'].rolling(window=5, min_periods=1).mean().iloc[-1]
    return avg_vol >= threshold, avg_vol

# --- 🧠 戰術雷達 (ROSS 策略實裝) ---
def scanner_engine():
    global cooldown_tracker
    print("🔄 啟動 Sniper V19.8 (ROSS 策略實彈版)...")
    try:
        tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
        print("✅ TradingView 數據源準備就緒！")
    except Exception as e:
        print(f"❌ TW 連線失敗: {e}")
        return

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
                        is_liquid, avg_vol = analyze_liquidity(df)
                        p = float(df['close'].iloc[-1])
                        
                        # 🦅 ROSS 策略邏輯
                        is_ross_price = 1.0 <= p <= 30.0 # 盤前範圍稍微放大
                        recent_vol_mean = df['volume'].iloc[-6:-1].mean()
                        vol_spike = df['volume'].iloc[-1] > (recent_vol_mean * 2.0) if recent_vol_mean > 0 else False
                        recent_high = df['high'].iloc[-11:-1].max()
                        is_breakout = p > recent_high
                        
                        is_spark = is_ross_price and vol_spike and is_breakout and is_liquid
                        is_diamond = is_ross_price and is_liquid and (p > df['close'].tail(10).mean()) and not is_spark
                        
                        mock_float = random.uniform(1.0, 15.0) # 暫用隨機，Ross 喜歡 < 10M 的
                        tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else "")
                        audio_trigger = "nova" if (is_spark and mock_float < 3.0) else ("spark" if is_spark else ("diamond" if is_diamond else ""))

                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                        cell.update({
                            "PriceStr": f"${p:.2f}", "PriceVal": p, "StopLossVal": p * 0.98,
                            "Volume": f"{avg_vol/1000:.1f}K", "FloatStr": f"{mock_float:.1f}M",
                            "Change": "盤前掃描中", "Signal": tag
                        })

                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 30):
                            cooldown_tracker[ticker] = now_ts
                            MASTER_BRAIN["surge"].insert(0, {
                                "Code": ticker, "Price": f"${p:.2f}", "Streak": tag, 
                                "FloatStr": f"{mock_float:.1f}M", "AudioTrigger": audio_trigger, "SignalTS": now_ts
                            })
                        threading.Thread(target=fetch_news_bg, args=(ticker, cell)).start()

            MASTER_BRAIN["surge"] = MASTER_BRAIN["surge"][:50]
            MASTER_BRAIN["last_update"] = datetime.now().strftime('%H:%M:%S')
            time.sleep(max(5, SCAN_INTERVAL + random.uniform(-2, 2)))
        except: time.sleep(5)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)