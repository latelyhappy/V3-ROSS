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
# 🛠️ 雲端環境與時區設定
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))

TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

MASTER_BRAIN = {
    "surge_log": [], "details": {}, "leaderboard": [], 
    "good_news": [], "last_update": ""
}

DYNAMIC_WATCHLIST = []
PREV_CLOSE_MAP = {}
news_cache_timer = {}
cooldown_tracker = {}
app = Flask(__name__)

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

# --- 📰 翻譯與新聞快取 ---
def fetch_news_smart(ticker, cell, stats):
    now = time.time()
    if ticker in news_cache_timer and (now - news_cache_timer[ticker] < 1200): return
    news_cache_timer[ticker] = now
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            translator = GoogleTranslator(source='auto', target='zh-TW')
            articles = []; has_today = False
            for item in root.findall('./channel/item')[:3]:
                raw_t = item.find('title').text
                try: zh_t = translator.translate(raw_t)
                except: zh_t = raw_t
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_NY)
                is_today = (dt.date() == datetime.now(TZ_NY).date())
                if is_today: has_today = True
                articles.append({"ticker": ticker, "title": zh_t, "link": item.find('link').text, "time": dt.strftime("%H:%M"), "is_today": is_today})
            cell["NewsList"] = articles
            cell["HasNews"] = has_today
    except: pass

# --- 🛰️ 名單獲取 (鎖定昨日收盤價) ---
def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, PREV_CLOSE_MAP
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        quotes = res.json()['finance']['result'][0]['quotes']
        new_list = []
        for q in quotes:
            symbol = q['symbol']
            price = q.get('regularMarketPrice', 0)
            if 1.0 <= price <= 40.0:
                new_list.append(symbol)
                PREV_CLOSE_MAP[symbol] = q.get('regularMarketPreviousClose', price)
        DYNAMIC_WATCHLIST = new_list[:20]
        print(f"📡 已更新掃描名單: {DYNAMIC_WATCHLIST}")
    except: pass

# --- 🧠 戰術雷達主引擎 ---
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
                    time.sleep(1.0) # 呼吸間隔，防止封鎖
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
                    if df is not None and not df.empty:
                        p = float(df['close'].iloc[-1])
                        o = float(df['open'].iloc[0])
                        prev_close = PREV_CLOSE_MAP.get(ticker, o)
                        
                        # 📈 數據計算
                        real_chg_pct = ((p - prev_close) / prev_close) * 100
                        daily_cum_vol = int(df['volume'].sum())
                        rel_vol = round(df['volume'].iloc[-1] / df['volume'].iloc[-11:-1].mean(), 2) if df['volume'].iloc[-11:-1].mean() > 0 else 1.0
                        
                        # 💡 EMA 系統
                        ema20 = df['close'].ewm(span=20, adjust=False).mean()
                        curr_ema20 = ema20.iloc[-1]
                        is_ema20_up = curr_ema20 > ema20.iloc[-2]
                        
                        # 🦅 Ross 策略：點火 & 趨勢滑行
                        is_spark = (rel_vol >= 2.0) and (p >= df['high'].iloc[-11:-1].max()) and (real_chg_pct > 3.0)
                        is_ride = is_ema20_up and (abs(p - curr_ema20)/curr_ema20 < 0.006) and (p > curr_ema20) and (real_chg_pct > 2.0) and not is_spark
                        is_weak = (p < o) or (real_chg_pct < -2.0)
                        
                        tag = "🔥強力點火" if is_spark else ("💎趨勢滑行" if is_ride else ("💀趨勢轉弱" if is_weak else ""))
                        status = "yellow" if is_spark else ("purple" if is_ride else ("red" if is_weak else "green"))

                        stats = {
                            "Code": ticker, "Price": f"${p:.2f}", "PriceVal": p, "RelVol": f"{rel_vol}x",
                            "Float": f"{random.uniform(2,15):.1f}M", "Vol": format_vol(daily_cum_vol),
                            "Pct": f"{real_chg_pct:+.2f}%", "ChangePct": f"{real_chg_pct:+.2f}%",
                            "Amt": f"{(p-prev_close):+.2f}", "Status": status, "Signal": tag, "StopLoss": curr_ema20*0.99
                        }
                        
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "HasNews": False})
                        cell.update(stats)
                        current_leaderboard.append(stats)

                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 45):
                            cooldown_tracker[ticker] = now_ts
                            log_entry = {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova" if is_spark else "spark"}
                            MASTER_BRAIN["surge_log"].insert(0, log_entry)
                            MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:1000]
                        
                        threading.Thread(target=fetch_news_smart, args=(ticker, cell, stats)).start()
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