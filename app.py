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

# ==========================================
# 🛠️ 全局緩存：儲存昨日收盤價
# ==========================================
PREV_CLOSE_MAP = {} 

# --- 🛰️ 動態名單獲取器 (同步抓取昨日收盤價) ---
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
            prev_close = q.get('regularMarketPreviousClose', 0)
            
            # 🟢 嚴格過濾 1-40 塊
            if 1.0 <= price <= 40.0:
                new_list.append(symbol)
                PREV_CLOSE_MAP[symbol] = prev_close # 💡 存入昨日收盤價
        
        DYNAMIC_WATCHLIST = new_list[:25]
        print(f"📡 動態雷達已更新，已鎖定 {len(DYNAMIC_WATCHLIST)} 檔標的昨日收盤價")
    except Exception as e:
        print(f"⚠️ 名單更新失敗: {e}")

# --- 🧠 戰術雷達 (真實漲幅校準版) ---
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
                    time.sleep(1.0)
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=30, extended_session=True)
                    if df is not None and not df.empty:
                        p = float(df['close'].iloc[-1])
                        # 💡 關鍵修正：從 Map 中獲取昨日收盤價，若無則暫用今日開盤
                        prev_close = PREV_CLOSE_MAP.get(ticker, df['open'].iloc[0])
                        
                        # 📈 計算真實漲幅 (對比昨日收盤)
                        real_chg_amt = p - prev_close
                        real_chg_pct = (real_chg_amt / prev_close) * 100
                        
                        # 📊 計算盤中漲幅 (對比今日開盤，輔助判定趨勢)
                        intraday_chg = p - df['open'].iloc[0]
                        
                        curr_v = int(df['volume'].sum()) # 累計量
                        rel_vol = round(df['volume'].iloc[-1] / df['volume'].iloc[-11:-1].mean(), 2) if df['volume'].iloc[-11:-1].mean() > 0 else 1.0
                        
                        # 🦅 Ross 策略判定
                        # 只要總漲幅 > 3% 且 盤中沒崩盤，就不標示為「趨勢轉弱」
                        is_spark = (rel_vol >= 2.0) and (p >= df['high'].iloc[-16:-1].max()) and (real_chg_pct > 3.0)
                        is_diamond = (p > df['close'].tail(10).mean()) and (real_chg_pct > 2.0) and not is_spark
                        is_weak = (real_chg_pct < 0) or (intraday_chg < -3.0) # 跌破昨收或盤中重挫才算弱
                        
                        tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else ("💀趨勢轉弱" if is_weak else ""))
                        status = "yellow" if is_spark else ("purple" if is_diamond else ("red" if is_weak else "green"))

                        stats = {
                            "Code": ticker, "Price": f"${p:.2f}", "PriceVal": p, "RelVol": f"{rel_vol}x",
                            "Float": f"{random.uniform(2,15):.1f}M", "Vol": format_vol(curr_v),
                            "Pct": f"{real_chg_pct:+.2f}%", "Amt": f"{real_chg_amt:+.2f}", # 💡 改為顯示真實漲跌
                            "Status": status, "StopLoss": p*0.97
                        }
                        
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "HasNews": False})
                        cell.update(stats)
                        current_leaderboard.append(stats)
                        
                        # 日誌發報與新聞... (維持原有邏輯)
                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 60):
                            cooldown_tracker[ticker] = now_ts
                            log_entry = {**stats, "Signal": tag, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova" if is_spark else "spark" if is_diamond else ""}
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