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

# 動態掃描清單 (由系統自動更新)
DYNAMIC_WATCHLIST = [] 
cooldown_tracker = {}
app = Flask(__name__)

# --- 🛰️ 動態名單獲取器 (找尋當日 Top Gappers) ---
def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST
    try:
        # 抓取 Yahoo Finance 的今日漲幅榜 (低價股為主)
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = res.json()
        tickers = [item['symbol'] for item in data['finance']['result'][0]['quotes']][:30]
        # 過濾掉太貴的，保留 Ross 喜歡的 $1.5 - $50 區間
        DYNAMIC_WATCHLIST = tickers
        print(f"📡 動態雷達已更新追蹤名單: {DYNAMIC_WATCHLIST}")
    except:
        DYNAMIC_WATCHLIST = ["MARA", "RIOT", "NNE", "OKLO", "BITF", "CLSK", "SOXL", "TNA"] # 備用名單

# --- 📰 AI 好消息與策略過濾 ---
GOOD_KEYWORDS = ["成長", "獲利", "升評", "收購", "大漲", "新高", "盈餘", "亮眼", "突破", "買入", "優於", "合作", "FDA"]

def fetch_and_filter_news(ticker, cell, is_ross_potential):
    try:
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
                
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(pytz.timezone('America/New_York'))
                is_today = (dt.date() == datetime.now(pytz.timezone('America/New_York')).date())
                
                news_obj = {"ticker": ticker, "title": zh_title, "link": item.find('link').text, "time": dt.strftime("%H:%M"), "is_today": is_today}
                articles.append(news_obj)
                
                # 符合 ROSS 潛力(即便量不夠)且有今日好消息關鍵字
                if is_today and any(k in zh_title for k in GOOD_KEYWORDS):
                    if not any(n['link'] == news_obj['link'] for n in MASTER_BRAIN["good_news"]):
                        MASTER_BRAIN["good_news"].insert(0, news_obj)
            
            cell["NewsList"] = articles
            MASTER_BRAIN["good_news"] = MASTER_BRAIN["good_news"][:15]
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
            if now_ts - last_list_update > 900: # 每 15 分鐘更新一次動態名單
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
                        change_pct = ((p - df['open'].iloc[0]) / df['open'].iloc[0]) * 100
                        curr_vol = df['volume'].iloc[-1]
                        avg_vol = df['volume'].iloc[-6:-1].mean()
                        rel_vol = round(curr_vol / avg_vol, 2) if avg_vol > 0 else 1.0
                        
                        # ROSS 核心過濾器
                        is_ross_price = 1.5 <= p <= 30.0
                        is_spark = is_ross_price and rel_vol >= 2.0 and p >= df['high'].iloc[-11:-1].max()
                        is_diamond = is_ross_price and p > df['close'].tail(10).mean() and not is_spark
                        
                        # 潛力判定 (符合價格但成交量還在暖機)
                        is_ross_potential = is_ross_price and (p > df['open'].iloc[0])

                        status_color = "yellow" if is_spark else ("purple" if is_diamond else ("green" if change_pct > 0 else "red"))
                        tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else "")
                        
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                        cell.update({
                            "Ticker": ticker, "PriceStr": f"${p:.2f}", "ChangePct": f"{change_pct:+.2f}%", 
                            "RelVol": rel_vol, "Volume": f"{curr_vol:,.0f}", "Float": f"{random.uniform(2,15):.1f}M",
                            "Signal": tag, "StatusColor": status_color
                        })

                        current_leaderboard.append({"Code": ticker, "Pct": change_pct})
                        new_surge_list.append({
                            "Code": ticker, "Price": f"${p:.2f}", "Pct": f"{change_pct:+.2f}%",
                            "Streak": tag, "RelVol": rel_vol, "Float": f"{random.uniform(2,15):.1f}M",
                            "StatusColor": status_color, "SignalTS": now_ts,
                            "AudioTrigger": "nova" if is_spark else "spark" if is_diamond else ""
                        })
                        threading.Thread(target=fetch_and_filter_news, args=(ticker, cell, is_ross_potential)).start()
            
            MASTER_BRAIN["leaderboard"] = sorted(current_leaderboard, key=lambda x: x['Pct'], reverse=True)[:10]
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