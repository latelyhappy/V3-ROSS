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
# 🛠️ 雲端環境變數
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
SCAN_INTERVAL = 15
PORT = int(os.getenv('PORT', 5000))

MASTER_BRAIN = {"surge": [], "details": {}, "last_update": ""}
WATCHLIST = ["TSLA", "NVDA", "AAPL", "AMD", "META", "MARA", "RIOT", "COIN", "PLTR", "NNE", "OKLO"]
cooldown_tracker = {}

app = Flask(__name__)

# --- 📰 新聞獲取 + 繁體翻譯 + 時間判定 ---
def fetch_news_bg(ticker, cell):
    try:
        tz_ny = pytz.timezone('America/New_York')
        now_ny = datetime.now(tz_ny)
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []
            translator = GoogleTranslator(source='auto', target='zh-TW')
            for item in root.findall('./channel/item')[:5]:
                raw_t = item.find('title').text
                try: zh_title = translator.translate(raw_t)
                except: zh_title = raw_t 
                
                pub_date = item.find('pubDate').text
                dt = parsedate_to_datetime(pub_date).astimezone(tz_ny)
                is_today = (dt.date() == now_ny.date())
                t_str = dt.strftime("%H:%M") if is_today else dt.strftime("%m/%d %H:%M")
                
                articles.append({
                    "title": zh_title, "link": item.find('link').text, 
                    "time": t_str, "is_today": is_today
                })
            cell["NewsList"] = articles
    except: pass

# --- 🔌 數據獲取 ---
def safe_get_tw_data(tv, symbol):
    for i in range(3):
        try:
            df = tv.get_hist(symbol=symbol, exchange='', interval=Interval.in_1_minute, n_bars=100, extended_session=True)
            if df is not None and not df.empty:
                last_time = df.index[-1]
                if last_time.tz is None: last_time = last_time.tz_localize('UTC')
                if (datetime.now(last_time.tzinfo) - last_time).total_seconds() > 1800: return None
                return df
        except: time.sleep(1)
    return None

# --- 🧠 戰術雷達 ---
def scanner_engine():
    global cooldown_tracker
    tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
    
    while True:
        try:
            now_ts = time.time()
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_ticker = {executor.submit(safe_get_tw_data, tv, ticker): ticker for ticker in WATCHLIST}
                for future in future_to_ticker:
                    ticker = future_to_ticker[future]
                    df = future.result()
                    if df is not None:
                        p = float(df['close'].iloc[-1])
                        # 計算量比 (RelVol)
                        curr_vol = df['volume'].iloc[-1]
                        avg_vol_5m = df['volume'].iloc[-6:-1].mean()
                        rel_vol = round(curr_vol / avg_vol_5m, 2) if avg_vol_5m > 0 else 1.0
                        
                        # Ross 策略參數
                        is_ross_price = 1.0 <= p <= 50.0
                        vol_spike = rel_vol >= 2.2
                        is_breakout = p >= df['high'].iloc[-16:-1].max()
                        
                        tag = "🔥強力點火" if (is_ross_price and vol_spike and is_breakout) else \
                              ("💎支撐回踩" if (is_ross_price and p > df['close'].tail(10).mean()) else "")
                        
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                        cell.update({
                            "PriceVal": p, "PriceStr": f"${p:.2f}", "StopLossVal": p * 0.98,
                            "RelVol": rel_vol, "Volume": f"{curr_vol:,.0f}",
                            "FloatStr": f"{random.uniform(1,15):.1f}M", "Signal": tag
                        })

                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 30):
                            cooldown_tracker[ticker] = now_ts
                            MASTER_BRAIN["surge"].insert(0, {
                                "Code": ticker, "Price": f"${p:.2f}", "Streak": tag, 
                                "RelVol": rel_vol, "SignalTS": now_ts,
                                "AudioTrigger": "nova" if (tag=="🔥強力點火") else "spark"
                            })
                        threading.Thread(target=fetch_news_bg, args=(ticker, cell)).start()
            
            MASTER_BRAIN["surge"] = MASTER_BRAIN["surge"][:50]
            MASTER_BRAIN["last_update"] = datetime.now().strftime('%H:%M:%S')
            time.sleep(SCAN_INTERVAL)
        except: time.sleep(5)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)