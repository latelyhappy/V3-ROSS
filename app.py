import time, threading, os, json, copy
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
# 🛡️ 系統防護與戰術設定 (V41.12 銅牆鐵壁版)
# ==========================================
brain_lock = threading.Lock() # 💡 執行緒安全鎖
TRENDS_FILE_PATH = os.path.join(os.path.dirname(__file__), 'trends.json')
_cached_trends = {}
_last_trends_update = 0
TRENDS_CACHE_TTL = 60  # 熱更新快取時間 (秒)

TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 8080))
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

# 💡 載入軍火庫
CATALYST_ARMORY = {}
armory_path = os.path.join(os.path.dirname(__file__), 'catalysts.json')
try:
    with open(armory_path, 'r', encoding='utf-8') as f:
        CATALYST_ARMORY = json.load(f)
    print("✅ 成功載入 catalysts.json 軍火庫")
except:
    CATALYST_ARMORY = {"INVERTED_TRAPS":{}, "RED":{}, "ORANGE":{}, "YELLOW":{}, "BLACK":{}}

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

def get_live_trends():
    """動態讀取熱點詞彙，實現熱更新"""
    global _cached_trends, _last_trends_update
    now = time.time()
    if now - _last_trends_update < TRENDS_CACHE_TTL: return _cached_trends
    try:
        if os.path.exists(TRENDS_FILE_PATH):
            with open(TRENDS_FILE_PATH, 'r', encoding='utf-8') as f:
                _cached_trends = json.load(f)
                _last_trends_update = now
        else: _cached_trends = {}
    except: pass
    return _cached_trends

def calculate_hft_score(headline):
    text = (headline or "").upper()
    total_score = 0
    for kw, score in CATALYST_ARMORY.get("INVERTED_TRAPS", {}).items():
        if kw in text: total_score += score; text = text.replace(kw, "") 
    for kw, score in CATALYST_ARMORY.get("BLACK", {}).items():
        if kw in text: return -50, True
    
    # 💡 整合動態熱點
    live_trends = get_live_trends()
    for kw, score in live_trends.items():
        if kw in text: total_score += score

    for cat in ["RED", "ORANGE", "YELLOW"]:
        for kw, score in CATALYST_ARMORY.get(cat, {}).items():
            if kw in text: total_score += score
    return total_score, False

def background_translate_worker(ticker, en_headline, master_brain):
    try:
        zh_text = GoogleTranslator(source='auto', target='zh-TW').translate(en_headline)
        with brain_lock:
            if ticker in master_brain['details']:
                for article in master_brain['details'][ticker].get('NewsList', []):
                    if article.get('raw_title') == en_headline:
                        article['title'] = zh_text
                        break 
    except: pass

def check_sec_fatal_traps(ticker):
    """合規 SEC 爬蟲 Header"""
    headers = {'User-Agent': 'SniperTrader/1.12 (contact@yourdomain.com)'}
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=&output=atom"
    try:
        res = requests.get(url, headers=headers, timeout=3)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for entry in root.findall('{http://www.w3.org/2005/Atom}entry')[:3]:
                title_elem = entry.find('{http://www.w3.org/2005/Atom}title')
                if title_elem is not None and any(trap in title_elem.text.upper() for trap in ['S-1', 'S-3', 'F-1', 'F-3']): return True
        return False
    except: return False

def fetch_and_score_news(ticker, cell):
    now = time.time()
    if ticker in news_cache and (now - news_cache[ticker] < 900): return
    news_cache[ticker] = now
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []; total_score = 0; has_black = False
            now_ny = datetime.now(TZ_NY)
            three_days_ago = now_ny.date() - timedelta(days=3)
            
            for item in root.findall('./channel/item')[:5]:
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_NY)
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
            
            with brain_lock:
                cell["NewsList"] = articles; cell["CatScore"] = total_score
                cell["IsTrap"] = has_black; cell["HasNews"] = len(articles) > 0 

            if articles:
                for art in articles:
                    threading.Thread(target=background_translate_worker, args=(ticker, art['raw_title'], MASTER_BRAIN), daemon=True).start()
                    time.sleep(0.5) # 🛡️ 翻譯流量控制
    except: pass

def extract_top_catalysts(master_brain):
    top_list = []
    with brain_lock:
        for ticker, data in master_brain.get('details', {}).items():
            news_list = data.get('NewsList', [])
            if news_list and any(n.get('is_today', False) for n in news_list):
                top_list.append(data)
    try:
        return sorted(top_list, key=lambda x: (x.get('CatScore', 0), x['NewsList'][0].get('time', '00:00')), reverse=True)
    except: return top_list

def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP
    try:
        url = "https://scanner.tradingview.com/america/scan"
        payload = {"filter": [{"left": "type", "operation": "in_range", "right": ["stock", "fund"]}], "columns": ["name", "premarket_close", "close", "premarket_change", "premarket_volume", "market_cap_basic", "type"], "sort": {"sortBy": "premarket_change", "sortOrder": "desc"}, "range": [0, 40]}
        res = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = res.json()
        if data.get('data'):
            DYNAMIC_WATCHLIST = [x['d'][0] for x in data['data']]
            for x in data['data']:
                sym, p, c, pct, v, mc, t = x['d'][0], x['d'][1], x['d'][2], x['d'][3], x['d'][4], x['d'][5], x['d'][6]
                p_eff = p if p is not None else c
                float_m = (mc / p_eff) / 1_000_000 if mc and p_eff else 0.0
                prev_est = p_eff / (1 + (pct/100)) if pct else p_eff
                f_str = "N/A (ETF)" if t == 'fund' else (f"{float_m:.1f}M" if float_m > 0 else "未知")
                if t != 'fund' and float_m > 50.0: f_str = f"⚠️{f_str}"
                STATS_MAP[sym] = {'prev': prev_est, 'float_str': f_str, 'type': t}
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
                stat = STATS_MAP.get(ticker, {'prev': p_live, 'float_str': '-', 'type': 'stock'})
                real_pct = ((p_live - stat['prev']) / stat['prev']) * 100
                daily_vol = int(df['volume'].sum())
                lookback = min(12, len(df))
                avg_v = df['volume'].iloc[-lookback:-2].mean() if lookback > 2 else 1
                rel_v = round(float(df['volume'].iloc[-1]) / avg_v, 2)
                
                sig = "🔥強力點火" if rel_v >= 2.5 and real_pct > 3.0 else ""
                stats = {"Code": ticker, "Price": f"${p_live:.2f}", "RelVol": f"{rel_v}x", "Vol": format_vol(daily_vol), "Pct": f"{real_pct:+.2f}%", "Float": stat['float_str'], "Type": stat['type'], "Signal": sig, "StopLoss": p_live * 0.98}
                
                with brain_lock:
                    cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False, "Price": "-", "Pct": "-"})
                    cell.update(stats)
                    if sig and (now_ts - cooldown_tracker.get(ticker, 0) > 60):
                        if check_sec_fatal_traps(ticker): stats["Signal"] += " 💀(SEC陷阱)"; cell["IsTrap"] = True
                        cooldown_tracker[ticker] = now_ts
                        log_entry = {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova" if real_pct > 5 else "spark"}
                        MASTER_BRAIN["surge_log"].insert(0, log_entry)
                        MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:500]

                threading.Thread(target=fetch_and_score_news, args=(ticker, cell), daemon=True).start()
            
            MASTER_BRAIN["leaderboard"] = [MASTER_BRAIN["details"][t] for t in DYNAMIC_WATCHLIST if t in MASTER_BRAIN["details"]][:20]
            MASTER_BRAIN["top_catalysts"] = extract_top_catalysts(MASTER_BRAIN)
            MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
        except: time.sleep(5)

@app.route('/')
def index(): return render_template('index.html')
@app.route('/data')
def data():
    with brain_lock: safe_brain = copy.deepcopy(MASTER_BRAIN)
    return jsonify(safe_brain)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)