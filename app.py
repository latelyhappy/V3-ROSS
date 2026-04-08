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
# 🛠️ 雲端環境與設定
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))

MASTER_BRAIN = {
    "surge": [], "details": {}, "leaderboard": [], 
    "good_news": [], "last_update": ""
}

DYNAMIC_WATCHLIST = [] 
cooldown_tracker = {}
app = Flask(__name__)

# --- 🛰️ 動態名單獲取器 (ROSS 規格：低價強勢股) ---
def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = res.json()
        quotes = data['finance']['result'][0]['quotes']
        # 🟢 嚴格過濾價格 1-40 塊
        tickers = [q['symbol'] for q in quotes if 1.0 <= q.get('regularMarketPrice', 0) <= 40.0][:30]
        DYNAMIC_WATCHLIST = tickers
        print(f"📡 動態雷達掃描中: {DYNAMIC_WATCHLIST}")
    except:
        DYNAMIC_WATCHLIST = ["MARA", "RIOT", "NNE", "OKLO", "BITF", "CLSK", "PLTR", "SOXL"]

# --- 📰 AI 好消息 (帶完整數據包) ---
GOOD_KEYWORDS = ["成長", "獲利", "升評", "收購", "大漲", "新高", "盈餘", "亮眼", "突破", "買入", "優於", "合作"]

def fetch_and_filter_news(ticker, cell, stock_stats):
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            translator = GoogleTranslator(source='auto', target='zh-TW')
            
            for item in root.findall('./channel/item')[:3]:
                raw_t = item.find('title').text
                try: zh_title = translator.translate(raw_t)
                except: zh_title = raw_t 
                
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(pytz.timezone('America/New_York'))
                is_today = (dt.date() == datetime.now(pytz.timezone('America/New_York')).date())
                
                if is_today and any(k in zh_title for k in GOOD_KEYWORDS):
                    news_obj = {
                        "ticker": ticker, "title": zh_title, "link": item.find('link').text, 
                        "time": dt.strftime("%H:%M"), **stock_stats # 注入即時數據
                    }
                    if not any(n['link'] == news_obj['link'] for n in MASTER_BRAIN["good_news"]):
                        MASTER_BRAIN["good_news"].insert(0, news_obj)
            MASTER_BRAIN["good_news"] = MASTER_BRAIN["good_news"][:10]
    except: pass

# --- 🔌 數據獲取 ---
def safe_get_tw_data(tv, symbol):
    try:
        return tv.get_hist(symbol=symbol, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
    except: return None

# --- 🧠 戰術雷達 ---
def scanner_engine():
    global cooldown_tracker
    tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
    last_list_update = 0
    
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_list_update > 600: # 每 10 分鐘更新
                update_dynamic_watchlist()
                last_list_update = now_ts
                
            current_leaderboard = []
            new_surge_list = []
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_ticker = {executor.submit(safe_get_tw_data, tv, ticker): ticker for ticker in DYNAMIC_WATCHLIST}
                for future in future_to_ticker:
                    ticker = future_to_ticker[future]
                    df = future.result()
                    if df is not None and not df.empty:
                        p = float(df['close'].iloc[-1])
                        
                        # 🔴 核心過濾：只處理 1-40 塊的股票
                        if not (1.0 <= p <= 40.0): continue
                        
                        o = float(df['open'].iloc[0])
                        chg_amt = p - o
                        chg_pct = (chg_amt / o) * 100
                        curr_v = df['volume'].iloc[-1]
                        rel_vol = round(curr_v / df['volume'].iloc[-6:-1].mean(), 2) if df['volume'].iloc[-6:-1].mean() > 0 else 1.0
                        mock_float = f"{random.uniform(2,15):.1f}M"
                        
                        # 訊號
                        is_spark = rel_vol >= 2.0 and p >= df['high'].iloc[-11:-1].max()
                        is_diamond = p > df['close'].tail(10).mean() and not is_spark
                        tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else "")
                        status = "yellow" if is_spark else ("purple" if is_diamond else ("green" if chg_amt > 0 else "red"))

                        # 數據包
                        stats = {
                            "Code": ticker, "Price": f"${p:.2f}", "RelVol": f"{rel_vol}x", 
                            "Float": mock_float, "Pct": f"{chg_pct:+.2f}%", "Amt": f"{chg_amt:+.2f}",
                            "Status": status
                        }

                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                        cell.update({**stats, "PriceVal": p, "StopLoss": p*0.98, "Volume": f"{curr_v:,.0f}"})

                        current_leaderboard.append(stats)
                        new_surge_list.append({**stats, "Signal": tag, "SignalTS": now_ts, "Audio": "nova" if is_spark else "spark" if is_diamond else ""})
                        
                        threading.Thread(target=fetch_and_filter_news, args=(ticker, cell, stats)).start()
            
            MASTER_BRAIN["leaderboard"] = sorted(current_leaderboard, key=lambda x: float(x['Pct'].replace('%','')), reverse=True)[:10]
            MASTER_BRAIN["surge"] = sorted(new_surge_list, key=lambda x: x['Code'])
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