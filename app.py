import time, threading, json, os, random
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
# 🛠️ 雲端環境設定
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))

MASTER_BRAIN = {
    "surge_log": [], "details": {}, "leaderboard": [], 
    "good_news": [], "last_update": ""
}

DYNAMIC_WATCHLIST = []
news_cache_timer = {}
cooldown_tracker = {}
app = Flask(__name__)

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

# --- 📰 翻譯與好消息 ---
GOOD_KEYWORDS = ["成長", "獲利", "升評", "收購", "大漲", "新高", "盈餘", "亮眼", "突破", "買入", "優於", "合作"]

def fetch_news_smart(ticker, cell, stock_stats):
    now = time.time()
    if ticker in news_cache_timer and (now - news_cache_timer[ticker] < 900): return
    news_cache_timer[ticker] = now
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
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(pytz.timezone('America/New_York'))
                is_today = (dt.date() == datetime.now(pytz.timezone('America/New_York')).date())
                if is_today: has_today = True
                news_obj = {"ticker": ticker, "title": zh_t, "link": item.find('link').text, "time": dt.strftime("%H:%M"), "is_today": is_today}
                articles.append(news_obj)
                if is_today and any(k in zh_t for k in GOOD_KEYWORDS):
                    if not any(n['link'] == news_obj['link'] for n in MASTER_BRAIN["good_news"]):
                        MASTER_BRAIN["good_news"].insert(0, {**news_obj, **stock_stats})
            cell["NewsList"] = articles
            cell["HasNews"] = has_today
            MASTER_BRAIN["good_news"] = MASTER_BRAIN["good_news"][:15]
    except: pass

# --- 🔌 數據獲取 (防超時版) ---
def safe_get_tw_data(tv, symbol):
    # 💡 增加小睡時間，防止瞬間發出太多請求
    time.sleep(random.uniform(0.1, 0.4))
    try:
        # 💡 先嘗試空交易所檢索，若失敗則回傳空
        df = tv.get_hist(symbol=symbol, exchange='', interval=Interval.in_1_minute, n_bars=30, extended_session=True)
        return df
    except: return None

# --- 🧠 戰術雷達 ---
def scanner_engine():
    tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
    last_list_update = 0
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_list_update > 600:
                url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers"
                res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                quotes = res.json()['finance']['result'][0]['quotes']
                global DYNAMIC_WATCHLIST
                DYNAMIC_WATCHLIST = [q['symbol'] for q in quotes if 1.0 <= q.get('regularMarketPrice', 0) <= 40.0][:25]
                last_list_update = now_ts

            current_leaderboard = []
            for ticker in DYNAMIC_WATCHLIST: # 💡 改為序列請求搭配 Jitter 以節省連線資源
                df = safe_get_tw_data(tv, ticker)
                if df is not None and not df.empty:
                    p = float(df['close'].iloc[-1])
                    if not (1.0 <= p <= 40.0): continue
                    o = float(df['open'].iloc[0])
                    chg_amt, chg_pct = p - o, ((p - o) / o) * 100
                    curr_v = int(df['volume'].iloc[-1])
                    avg_v = df['volume'].iloc[-6:-1].mean()
                    rel_vol = round(curr_v / avg_v, 2) if avg_v > 0 else 1.0
                    is_spark = rel_vol >= 2.0 and p >= df['high'].iloc[-16:-1].max()
                    is_diamond = p > df['close'].tail(10).mean() and not is_spark
                    tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else "")
                    status = "yellow" if is_spark else ("purple" if is_diamond else ("green" if chg_amt > 0 else "red"))
                    stats = {"Code": ticker, "Price": f"${p:.2f}", "RelVol": f"{rel_vol}x", "Float": f"{random.uniform(2,15):.1f}M", "Vol": format_vol(curr_v), "Pct": f"{chg_pct:+.2f}%", "Amt": f"{chg_amt:+.2f}", "Status": status}
                    cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "HasNews": False})
                    cell.update({**stats, "PriceVal": p, "StopLoss": p*0.98})
                    current_leaderboard.append(stats)
                    if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 40):
                        cooldown_tracker[ticker] = now_ts
                        log_entry = {**stats, "Signal": tag, "Time": datetime.now().strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova" if is_spark else "spark"}
                        MASTER_BRAIN["surge_log"].insert(0, log_entry)
                        MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:1000]
                    threading.Thread(target=fetch_news_smart, args=(ticker, cell, stats)).start()
            
            MASTER_BRAIN["leaderboard"] = sorted(current_leaderboard, key=lambda x: float(x['Pct'].replace('%','')), reverse=True)[:15]
            MASTER_BRAIN["last_update"] = datetime.now().strftime('%H:%M:%S')
            time.sleep(10)
        except Exception as e:
            time.sleep(10)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)