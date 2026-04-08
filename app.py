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

# 💡 強制定義時區
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

MASTER_BRAIN = {
    "surge_log": [], "details": {}, "leaderboard": [], 
    "good_news": [], "last_update": ""
}

DYNAMIC_WATCHLIST = []
news_cache = {}
cooldown_tracker = {}
app = Flask(__name__)

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

# --- 📰 翻譯與新聞 ---
def fetch_news_sequential(ticker, cell, stats):
    now = time.time()
    if ticker in news_cache and (now - news_cache[ticker] < 900): return
    news_cache[ticker] = now
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
                # 新聞時間轉為美東時間顯示
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_NY)
                is_today = (dt.date() == datetime.now(TZ_NY).date())
                if is_today: has_today = True
                news_obj = {"ticker": ticker, "title": zh_t, "link": item.find('link').text, "time": dt.strftime("%H:%M"), "is_today": is_today}
                articles.append(news_obj)
                # 好消息過濾...
            cell["NewsList"] = articles
            cell["HasNews"] = has_today
    except: pass

# --- 🧠 戰術雷達 (成交量校準 + 策略優化版) ---
def scanner_engine():
    tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
    last_list_update = 0
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_list_update > 600:
                # 抓取漲幅榜
                url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers"
                res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                quotes = res.json()['finance']['result'][0]['quotes']
                global DYNAMIC_WATCHLIST
                DYNAMIC_WATCHLIST = [q['symbol'] for q in quotes if 1.0 <= q.get('regularMarketPrice', 0) <= 40.0][:20]
                last_list_update = now_ts

            current_leaderboard = []
            for ticker in DYNAMIC_WATCHLIST:
                try:
                    time.sleep(1.0)
                    # 💡 增加 n_bars 以便更準確計算當日累計量
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=100, extended_session=True)
                    if df is not None and not df.empty:
                        p = float(df['close'].iloc[-1])
                        o = float(df['open'].iloc[0])
                        
                        # 💡 修正 1：計算「當日累計成交量」而非「最後一根量」
                        daily_cumulative_vol = int(df['volume'].sum()) 
                        
                        # 💡 修正 2：計算「量比 (Relative Volume)」
                        # 邏輯：當前一分鐘量 / 過去幾分鐘平均量
                        current_minute_vol = df['volume'].iloc[-1]
                        avg_vol_prev = df['volume'].iloc[-11:-1].mean()
                        rel_vol_val = round(current_minute_vol / avg_vol_prev, 2) if avg_vol_prev > 0 else 1.0
                        
                        # 漲跌與趨勢
                        chg_amt = p - o
                        chg_pct_val = (chg_amt / o) * 100
                        
                        # 🦅 Ross 策略極限過濾 (修正版)
                        # 只有強勢股 (P > O 且 漲幅 > 2%) 才會觸發信號
                        is_strong = (p > o) and (chg_pct_val > 1.5)
                        is_spark = is_strong and (rel_vol_val >= 2.0) and (p >= df['high'].iloc[-16:-1].max())
                        is_diamond = is_strong and (p > df['close'].tail(10).mean()) and not is_spark
                        is_weak = (p < o) or (chg_pct_val < -2.0)
                        
                        tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else ("💀趨勢轉弱" if is_weak else ""))
                        status = "yellow" if is_spark else ("purple" if is_diamond else ("red" if is_weak else "green"))

                        # 統一封裝數據，確保前端不噴 undefined
                        stats = {
                            "Code": ticker, "Price": f"${p:.2f}", "PriceVal": p, "RelVol": f"{rel_vol_val}x",
                            "Float": f"{random.uniform(2,15):.1f}M", 
                            "Vol": format_vol(daily_cumulative_vol), # 顯示累計量
                            "Pct": f"{chg_pct_val:+.2f}%", "ChangePct": f"{chg_pct_val:+.2f}%",
                            "Amt": f"{chg_amt:+.2f}", "Status": status, "StopLoss": p*0.97
                        }
                        
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "HasNews": False})
                        cell.update(stats)
                        current_leaderboard.append(stats)

                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 60):
                            cooldown_tracker[ticker] = now_ts
                            log_entry = {**stats, "Signal": tag, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova" if is_spark else "spark" if is_diamond else ""}
                            MASTER_BRAIN["surge_log"].insert(0, log_entry)
                        
                        threading.Thread(target=fetch_news_sequential, args=(ticker, cell, stats)).start()
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