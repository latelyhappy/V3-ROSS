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
PORT = int(os.getenv('PORT', 5000))

MASTER_BRAIN = {
    "surge": [], 
    "details": {}, 
    "leaderboard": [], 
    "good_news": [],
    "last_update": ""
}

WATCHLIST = ["TSLA", "NVDA", "AAPL", "AMD", "META", "MARA", "RIOT", "COIN", "PLTR", "NNE", "OKLO", "SOXL"]
cooldown_tracker = {}

app = Flask(__name__)

# --- 📰 翻譯與好消息判定器 ---
GOOD_KEYWORDS = ["成長", "獲利", "升評", "收購", "大漲", "新高", "盈餘", "亮眼", "突破", "買入", "優於"]

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
                try: zh_title = translator.translate(raw_t)
                except: zh_title = raw_t 
                
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(tz_ny)
                is_today = (dt.date() == datetime.now(tz_ny).date())
                t_str = dt.strftime("%H:%M") if is_today else dt.strftime("%m/%d %H:%M")
                
                news_obj = {"ticker": ticker, "title": zh_title, "link": item.find('link').text, "time": t_str, "is_today": is_today}
                articles.append(news_obj)
                
                if any(k in zh_title for k in GOOD_KEYWORDS) and is_today:
                    if news_obj not in MASTER_BRAIN["good_news"]:
                        MASTER_BRAIN["good_news"].insert(0, news_obj)
            
            cell["NewsList"] = articles
            MASTER_BRAIN["good_news"] = MASTER_BRAIN["good_news"][:10]
    except: pass

def safe_get_tw_data(tv, symbol):
    try:
        return tv.get_hist(symbol=symbol, exchange='', interval=Interval.in_1_minute, n_bars=100, extended_session=True)
    except: return None

# --- 🧠 戰術雷達主引擎 ---
def scanner_engine():
    global cooldown_tracker
    tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
    
    while True:
        try:
            now_ts = time.time()
            current_leaderboard = []
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_ticker = {executor.submit(safe_get_tw_data, tv, ticker): ticker for ticker in WATCHLIST}
                for future in future_to_ticker:
                    ticker = future_to_ticker[future]
                    df = future.result()
                    if df is not None and not df.empty:
                        p = float(df['close'].iloc[-1])
                        o = float(df['open'].iloc[0]) 
                        
                        change_amt = p - o
                        change_pct = (change_amt / o) * 100
                        curr_vol = df['volume'].iloc[-1]
                        avg_vol = df['volume'].iloc[-6:-1].mean()
                        rel_vol = round(curr_vol / avg_vol, 2) if avg_vol > 0 else 1.0
                        
                        is_ross_price = 1.0 <= p <= 200.0 # 擴大價格範圍
                        is_spark = is_ross_price and rel_vol >= 2.2 and p >= df['high'].iloc[-16:-1].max()
                        is_diamond = is_ross_price and p > df['close'].tail(10).mean() and not is_spark
                        
                        # 決定全行狀態色
                        status_color = "yellow" if is_spark else ("purple" if is_diamond else ("green" if change_amt > 0 else "red"))
                        tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else "")
                        
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                        cell.update({
                            "Ticker": ticker, "PriceVal": p, "PriceStr": f"${p:.2f}", 
                            "ChangeAmt": f"{change_amt:+.2f}", "ChangePct": f"{change_pct:+.2f}%", 
                            "RelVol": rel_vol, "Volume": f"{curr_vol:,.0f}", 
                            "Float": f"{random.uniform(2, 15):.1f}M", "Signal": tag,
                            "StatusColor": status_color
                        })

                        current_leaderboard.append({"Code": ticker, "Price": p, "Pct": change_pct})

                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 30):
                            cooldown_tracker[ticker] = now_ts
                            MASTER_BRAIN["surge"].insert(0, {
                                "Code": ticker, "Price": f"${p:.2f}", "Amt": f"{change_amt:+.2f}",
                                "Streak": tag, "RelVol": rel_vol, "Float": f"{random.uniform(2, 15):.1f}M",
                                "StatusColor": status_color, "SignalTS": now_ts,
                                "AudioTrigger": "nova" if is_spark else "spark"
                            })
                        threading.Thread(target=fetch_news_bg, args=(ticker, cell)).start()
            
            MASTER_BRAIN["leaderboard"] = sorted(current_leaderboard, key=lambda x: x['Pct'], reverse=True)[:10]
            MASTER_BRAIN["surge"] = MASTER_BRAIN["surge"][:40]
            MASTER_BRAIN["last_update"] = datetime.now().strftime('%H:%M:%S')
            time.sleep(15)
        except: time.sleep(5)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)