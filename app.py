import time, threading, os, json
from datetime import datetime, timedelta
import pandas as pd
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import pytz
from tvDatafeed import TvDatafeed, Interval
from flask import Flask, jsonify, render_template
from deep_translator import GoogleTranslator

# ==========================================
# 🛠️ 戰略設定與全局記憶體 (V41.7 完整整合版)
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

CATALYST_ARMORY = {}
armory_path = os.path.join(os.path.dirname(__file__), 'catalysts.json')
try:
    with open(armory_path, 'r', encoding='utf-8') as f:
        CATALYST_ARMORY = json.load(f)
except:
    CATALYST_ARMORY = {"INVERTED_TRAPS":{}, "THEMATIC_TRENDS":{}, "RED":{}, "ORANGE":{}, "YELLOW":{}, "BLACK":{}}

MASTER_BRAIN = {"surge_log": [], "details": {}, "leaderboard": [], "top_catalysts": [], "last_update": ""}
DYNAMIC_WATCHLIST = []
STATS_MAP = {} 
news_cache = {} 
cooldown_tracker = {}
STATE_TRACKER = {}
app = Flask(__name__)

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

def calculate_hft_score(headline):
    text = headline.upper()
    total_score = 0
    for kw, score in CATALYST_ARMORY.get("INVERTED_TRAPS", {}).items():
        if kw in text: total_score += score; text = text.replace(kw, "")
    for kw, score in CATALYST_ARMORY.get("BLACK", {}).items():
        if kw in text: return -50, True
    for category in ["RED", "ORANGE", "YELLOW", "THEMATIC_TRENDS"]:
        for kw, score in CATALYST_ARMORY.get(category, {}).items():
            if kw in text: total_score += score
    return total_score, False

def background_translate_worker(ticker, en_headline):
    try:
        zh_text = GoogleTranslator(source='auto', target='zh-TW').translate(en_headline)
        if ticker in MASTER_BRAIN['details']:
            for art in MASTER_BRAIN['details'][ticker].get('NewsList', []):
                if art.get('raw_title') == en_headline:
                    art['title'] = zh_text
                    break
    except: pass

def fetch_and_score_news(ticker, cell):
    now = time.time()
    if ticker in news_cache and (now - news_cache[ticker] < 900): return
    news_cache[ticker] = now
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []; total_score = 0; has_black = False
            now_ny = datetime.now(TZ_NY)
            three_days_ago = now_ny.date() - timedelta(days=3)
            
            for item in root.findall('./channel/item')[:5]:
                dt_str = item.find('pubDate').text
                dt = parsedate_to_datetime(dt_str).astimezone(TZ_NY)
                if dt.date() < three_days_ago: continue
                raw_t = item.find('title').text
                score, is_trap = calculate_hft_score(raw_t)
                if is_trap: has_black = True
                total_score += score
                articles.append({
                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": raw_t,
                    "link": item.find('link').text, "time": dt.strftime("%Y-%m-%d %H:%M"),
                    "is_today": (dt.date() == now_ny.date()), "score": score
                })
            cell["NewsList"] = articles; cell["CatScore"] = total_score
            cell["IsTrap"] = has_black; cell["HasNews"] = len(articles) > 0
            if articles:
                for art in articles:
                    threading.Thread(target=background_translate_worker, args=(ticker, art['raw_title']), daemon=True).start()
    except: pass

def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP
    try:
        url = "https://scanner.tradingview.com/america/scan"
        payload = {
            "filter": [{"left": "exchange", "operation": "in_range", "right": ["AMEX", "NASDAQ", "NYSE"]},
                       {"left": "type", "operation": "in_range", "right": ["stock", "fund"]}],
            "columns": ["name", "premarket_close", "close", "premarket_change", "premarket_volume", "market_cap_basic", "type"],
            "sort": {"sortBy": "premarket_change", "sortOrder": "desc"}, "range": [0, 40]
        }
        res = requests.post(url, json=payload, timeout=10)
        data = res.json()
        if data.get('data'):
            DYNAMIC_WATCHLIST = [x['d'][0] for x in data['data']]
            for x in data['data']:
                sym = x['d'][0]
                price = x['d'][1] if x['d'][1] is not None else x['d'][2]
                mc = x['d'][5]
                float_m = (mc / price) / 1_000_000 if mc and price else 0.0
                asset_type = x['d'][6]
                float_str = "N/A (ETF)" if asset_type == 'fund' else (f"{float_m:.1f}M" if float_m > 0 else "未知")
                if asset_type != 'fund' and float_m > 50.0: float_str = f"⚠️{float_str}"
                STATS_MAP[sym] = {'prev': price / (1 + (x['d'][3]/100)) if x['d'][3] else price, 'float_str': float_str, 'type': asset_type}
    except: pass

def scanner_engine():
    tv = TvDatafeed()
    last_list_update = 0
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_list_update > 300: update_dynamic_watchlist(); last_list_update = now_ts
            
            for ticker in DYNAMIC_WATCHLIST:
                time.sleep(1.0)
                df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
                if df is None or df.empty: continue
                
                p_live = float(df['close'].iloc[-1])
                daily_vol = int(df['volume'].sum())
                lookback = min(12, len(df))
                avg_vol = df['volume'].iloc[-lookback:-2].mean() if lookback > 2 else 1
                rel_vol = round(float(df['volume'].iloc[-1]) / avg_vol, 2)
                
                stat = STATS_MAP.get(ticker, {'prev': p_live, 'float_str': '-', 'type': 'stock'})
                real_pct = ((p_live - stat['prev']) / stat['prev']) * 100
                
                cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False})
                cell.update({
                    "Code": ticker, "Price": f"${p_live:.2f}", "RelVol": f"{rel_vol}x", "Vol": format_vol(daily_vol),
                    "Pct": f"{real_pct:+.2f}%", "Float": stat['float_str'], "Type": stat['type']
                })
                threading.Thread(target=fetch_and_score_news, args=(ticker, cell), daemon=True).start()
                
                # [V41.7 訊號邏輯修補]
                sig = ""
                if rel_vol >= 2.5 and real_pct > 3.0: sig = "🔥 強力點火"
                cell["Signal"] = sig
                if sig and (now_ts - cooldown_tracker.get(ticker, 0) > 60):
                    cooldown_tracker[ticker] = now_ts
                    MASTER_BRAIN["surge_log"].insert(0, {**cell, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts})
            
            # [V41.7 數據分發補全]
            MASTER_BRAIN["leaderboard"] = [MASTER_BRAIN["details"][t] for t in DYNAMIC_WATCHLIST if t in MASTER_BRAIN["details"]][:20]
            news_stocks = [MASTER_BRAIN["details"][t] for t in MASTER_BRAIN["details"] if MASTER_BRAIN["details"][t].get("NewsList")]
            MASTER_BRAIN["top_catalysts"] = sorted(news_stocks, key=lambda x: (x['CatScore'], x['NewsList'][0]['time']), reverse=True)
            MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
        except: time.sleep(5)

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)
@app.route('/')
def index(): return render_template('index.html')

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)