import time, threading, json, os, random
from datetime import datetime
import pandas as pd
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import pytz
from tvDatafeed import TvDatafeed, Interval
from flask import Flask, jsonify, render_template
from deep_translator import GoogleTranslator

# ==========================================
# 🛠️ 環境與時區設定
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))

TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

MASTER_BRAIN = {"surge_log": [], "details": {}, "leaderboard": [], "good_news": [], "last_update": ""}
DYNAMIC_WATCHLIST = []
news_cache = {} 
cooldown_tracker = {}
app = Flask(__name__)

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

# --- 📰 新聞連動與橘色標記 ---
def fetch_news_sequential(ticker, cell, stats):
    now = time.time()
    if ticker in news_cache and (now - news_cache[ticker] < 1200): return
    news_cache[ticker] = now
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            translator = GoogleTranslator(source='auto', target='zh-TW')
            articles = []; has_today = False
            for item in root.findall('./channel/item')[:5]:
                raw_t = item.find('title').text
                try: zh_t = translator.translate(raw_t)
                except: zh_t = raw_t
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_NY)
                is_today = (dt.date() == datetime.now(TZ_NY).date())
                if is_today: has_today = True
                
                news_obj = {"ticker": ticker, "title": zh_t, "link": item.find('link').text, "time": dt.strftime("%H:%M"), "is_today": is_today}
                articles.append(news_obj)
            
            cell["NewsList"] = articles
            cell["HasTodayNews"] = has_today # 用於前端顯示圖示
    except: pass

# --- 🧠 戰術雷達 (EMA 趨勢滑行版) ---
def scanner_engine():
    tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
    last_list_update = 0
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_list_update > 600:
                update_dynamic_watchlist()
                last_list_update = now_ts

            current_leaderboard = []
            for ticker in DYNAMIC_WATCHLIST:
                try:
                    time.sleep(0.8)
                    # 💡 抓取更多 K 線以準確計算 EMA
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
                    if df is not None and not df.empty:
                        p = float(df['close'].iloc[-1])
                        o = float(df['open'].iloc[0])
                        prev_close = PREV_CLOSE_MAP.get(ticker, o)
                        
                        # 📈 漲幅與量化
                        real_chg_pct = ((p - prev_close) / prev_close) * 100
                        curr_v = int(df['volume'].sum())
                        rel_vol = round(df['volume'].iloc[-1] / df['volume'].iloc[-11:-1].mean(), 2) if df['volume'].iloc[-11:-1].mean() > 0 else 1.0
                        
                        # 💡 計算 EMA 系統
                        ema9 = df['close'].ewm(span=9, adjust=False).mean()
                        ema20 = df['close'].ewm(span=20, adjust=False).mean()
                        curr_ema9 = ema9.iloc[-1]
                        curr_ema20 = ema20.iloc[-1]
                        prev_ema20 = ema20.iloc[-2]
                        
                        # 🦅 Ross 策略 - 趨勢滑行判斷
                        # 1. 強力點火 (Spark)：爆量、突破、且在 EMA9 之上
                        is_spark = (rel_vol >= 2.0) and (p >= df['high'].iloc[-11:-1].max()) and (p > curr_ema9)
                        
                        # 2. 趨勢滑行 (Diamond)：取代傳統回踩
                        # 條件：EMA20 向上翹 + 價格貼近 EMA20 (0.5% 內) + 價格 > EMA20 + 總漲幅 > 3%
                        is_ema_sloping_up = curr_ema20 > prev_ema20
                        is_near_ema20 = abs(p - curr_ema20) / curr_ema20 < 0.005 
                        is_trend_ride = is_ema_sloping_up and is_near_ema20 and (p > curr_ema20) and (real_chg_pct > 3.0) and not is_spark
                        
                        tag = "🔥強力點火" if is_spark else ("💎趨勢滑行" if is_trend_ride else "")
                        status = "yellow" if is_spark else ("purple" if is_trend_ride else ("green" if p > o else "red"))

                        stats = {
                            "Code": ticker, "Price": f"${p:.2f}", "RelVol": f"{rel_vol}x",
                            "Vol": format_vol(curr_v), "Pct": f"{real_chg_pct:+.2f}%", 
                            "Amt": f"{(p-prev_close):+.2f}", "Status": status, "Signal": tag,
                            "PriceVal": p, "StopLoss": curr_ema20 * 0.99 # 💡 止損設在 EMA20 下方一點點
                        }
                        
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "HasTodayNews": False})
                        cell.update(stats)
                        current_leaderboard.append(stats)

                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 40):
                            cooldown_tracker[ticker] = now_ts
                            log_entry = {**stats, "Signal": tag, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova" if is_spark else "spark"}
                            MASTER_BRAIN["surge_log"].insert(0, log_entry)
                        
                        fetch_news_sequential(ticker, cell, stats)
                except: continue

            MASTER_BRAIN["leaderboard"] = sorted(current_leaderboard, key=lambda x: float(x['Pct'].replace('%','')), reverse=True)[:15]
            MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
            time.sleep(10)
        except: time.sleep(15)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)