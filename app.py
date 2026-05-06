import os
# 💡 作業系統級消音器：從根源封殺所有 Python 警告！
os.environ["PYTHONWARNINGS"] = "ignore"

import warnings
warnings.filterwarnings("ignore")

import logging
# 💡 封殺第三方套件的底層連線日誌，保持終端機極度乾淨
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
logging.getLogger('tvDatafeed').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

from flask import Flask, jsonify, render_template, request, send_file
import io
import time, threading, json, re, base64, copy, math
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import pytz
from tvDatafeed import TvDatafeed, Interval
from deep_translator import GoogleTranslator
from collections import Counter
import concurrent.futures 
import yfinance as yf  

# 匯入 Alpaca 混合引擎
from alpaca_worker import init_alpaca
# 💡 匯入 V46 數據收集器
import collector

# ==========================================
# 💡 必須在這裡初始化 Flask 應用程式！
# ==========================================
app = Flask(__name__)

# ==========================================
# 📡 戰略情報匯出接口 (支援分批與當日過濾)
# ==========================================
@app.route('/api/export_intelligence')
def export_intelligence():
    """
    獲取實戰 JSON 檔案。
    可用參數: ?limit=500 或 ?today=true
    """
    # 解析網址參數
    limit = request.args.get('limit', default=None, type=int)
    today = request.args.get('today', default='false').lower() == 'true'
    
    # 呼叫底層 collector 執行過濾匯出
    export_path = collector.export_to_json(limit=limit, today_only=today)
    
    # 確認檔案存在並發送給使用者下載
    if export_path and os.path.exists(export_path):
        # 加上時間戳記避免檔名重複
        filename = f"sniper_intel_{datetime.now().strftime('%m%d_%H%M')}.json"
        return send_file(export_path, mimetype='application/json', as_attachment=True, download_name=filename)
        
    return jsonify({"status": "error", "message": "無符合條件之資料，或資料庫為空"}), 404


# ==========================================
# 🧠 AI 戰略摘要引擎接口 (減輕大數據分析負擔)
# ==========================================
@app.route('/api/intelligence_summary')
def intelligence_summary():
    """
    由後端 Python 直接運算前 50 大高勝率、高漲幅詞彙，
    輸出輕量化 JSON 報表，供指揮官直接貼給 AI 進行分析。
    """
    result = collector.generate_intelligence_summary()
    
    if result.get("status") == "success":
        return jsonify(result), 200
    else:
        return jsonify(result), 500

brain_lock = threading.RLock() 
TRENDS_FILE_PATH = os.path.join(os.path.dirname(__file__), 'trends.json')
TRENDS_DRAFT_PATH = os.path.join(os.path.dirname(__file__), 'trends_draft.json') 
DISCOVERY_LOG_PATH = os.path.join(os.path.dirname(__file__), 'discovery_log.json') 
SESSION_BACKUP_PATH = os.path.join(os.path.dirname(__file__), 'session_state.json')
FLOAT_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'float_cache.json')

FINNHUB_TOKEN = os.getenv('FINNHUB_TOKEN')
_cached_trends = {}
_last_trends_update = 0
TRENDS_CACHE_TTL = 60
_discovery_buffer = []
_last_flush_time = time.time()
_last_list_update = 0
SPY_real_pct = 0.0
_last_spy_update = 0
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 8080))
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')
MIN_RELVOL_LIMIT = 3.0 
FLOAT_CACHE = {}

CATALYST_ARMORY = {}
armory_path = os.path.join(os.path.dirname(__file__), 'catalysts.json')
def reload_armory():
    global CATALYST_ARMORY
    try:
        with open(armory_path, 'r', encoding='utf-8') as f: 
            CATALYST_ARMORY = json.load(f)
    except: CATALYST_ARMORY = {}

reload_armory()

def load_float_cache():
    global FLOAT_CACHE
    if os.path.exists(FLOAT_CACHE_FILE):
        try:
            with open(FLOAT_CACHE_FILE, 'r', encoding='utf-8') as f:
                FLOAT_CACHE = json.load(f)
        except: pass

def save_float_cache():
    try:
        with open(FLOAT_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(FLOAT_CACHE, f)
    except: pass

def get_real_float(symbol):
    if symbol in FLOAT_CACHE: return FLOAT_CACHE[symbol]
    real_float = 0.0
    try:
        yf_symbol = symbol.replace('-', '.')
        ticker = yf.Ticker(yf_symbol)
        yf_float = ticker.info.get('floatShares')
        if yf_float and yf_float > 0:
            real_float = yf_float
    except: pass
    if real_float == 0.0 and FINNHUB_TOKEN:
        try:
            url = f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}&metric=all&token={FINNHUB_TOKEN}"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                fh_float = res.json().get('metric', {}).get('floatShares', 0)
                if fh_float > 0:
                    real_float = fh_float * 1e6 if fh_float < 50000 else fh_float
        except: pass
    if real_float > 0:
        FLOAT_CACHE[symbol] = real_float
        save_float_cache()
        return real_float
    return 0.0

MASTER_BRAIN = {"surge_log": [], "details": {}, "leaderboard": [], "vwap_list": [], "top_catalysts": [], "last_update": "", "elite_words": []}
DYNAMIC_WATCHLIST = []
STATS_MAP = {} 
news_cache = {} 
cooldown_tracker = {}
STATE_TRACKER = {}
app = Flask(__name__)

def load_intraday_state():
    global MASTER_BRAIN, DYNAMIC_WATCHLIST, STATS_MAP, cooldown_tracker, STATE_TRACKER
    try:
        if os.path.exists(SESSION_BACKUP_PATH):
            with open(SESSION_BACKUP_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            current_date = datetime.now(TZ_NY).strftime("%Y-%m-%d")
            if data.get("date") == current_date:
                with brain_lock:
                    MASTER_BRAIN = data.get("MASTER_BRAIN", MASTER_BRAIN)
                    DYNAMIC_WATCHLIST = data.get("DYNAMIC_WATCHLIST", [])
                    STATS_MAP = data.get("STATS_MAP", {})
                    cooldown_tracker = data.get("cooldown_tracker", {})
                    STATE_TRACKER = data.get("STATE_TRACKER", {})
    except Exception as e: pass

def state_auto_save_worker():
    while True:
        time.sleep(15) 
        try:
            with brain_lock:
                backup_data = {
                    "date": datetime.now(TZ_NY).strftime("%Y-%m-%d"),
                    "MASTER_BRAIN": MASTER_BRAIN,
                    "DYNAMIC_WATCHLIST": DYNAMIC_WATCHLIST,
                    "STATS_MAP": STATS_MAP,
                    "cooldown_tracker": cooldown_tracker,
                    "STATE_TRACKER": STATE_TRACKER
                }
            with open(SESSION_BACKUP_PATH, 'w', encoding='utf-8') as f:
                json.dump(backup_data, f, ensure_ascii=False)
        except: pass

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

def get_live_trends():
    global _cached_trends, _last_trends_update
    now = time.time()
    if now - _last_trends_update < TRENDS_CACHE_TTL: return _cached_trends
    try:
        if os.path.exists(TRENDS_FILE_PATH):
            with open(TRENDS_FILE_PATH, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
                _cached_trends = {}
                for k, v in raw_data.items():
                    if isinstance(v, dict):
                        if "avg_impact_pct" not in v: v["avg_impact_pct"] = 0.0
                        _cached_trends[k] = v
                    else:
                        _cached_trends[k] = {"score": v, "count": 1, "avg_impact_pct": 0.0}
                _last_trends_update = now
        else: _cached_trends = {}
    except: pass
    return _cached_trends

def is_news_echo(new_title, new_dt_tpe, existing_news_list):
    # 💡 V46 升級：字元限制從 4 放寬為 3，確保抓到 FDA、SMR、IPO
    new_words = set(re.findall(r'\b[A-Z]{3,}\b', new_title.upper()))
    if not new_words: return False
    for n in existing_news_list:
        try:
            old_dt_str = f"{new_dt_tpe.year}-{n['time']}"
            old_dt_tpe = datetime.strptime(old_dt_str, "%Y-%m-%d %H:%M")
            old_dt_tpe = TZ_TW.localize(old_dt_tpe)
            if abs((new_dt_tpe - old_dt_tpe).total_seconds()) <= 10800:
                old_words = set(re.findall(r'\b[A-Z]{3,}\b', n['raw_title'].upper()))
                if old_words:
                    overlap = len(new_words.intersection(old_words)) / len(new_words.union(old_words))
                    if overlap >= 0.6: return True
        except: pass
    return False

def calculate_hft_score(headline, ticker=""):
    text = (headline or "").upper()
    total_score = 0
    elite_hits = []

    reload_armory()
    INVERTED_TRAPS = list(CATALYST_ARMORY.get("INVERTED_TRAPS", {}).keys())
    HINDSIGHT_TRAPS = list(CATALYST_ARMORY.get("HINDSIGHT_NOISE", {}).keys())
    TOXIC_OFFERINGS = list(CATALYST_ARMORY.get("TOXIC_OFFERINGS", {}).keys())
    CLASS_ACTION_SPAM = list(CATALYST_ARMORY.get("CLASS_ACTION_SPAM", {}).keys())
    MEGACAPS = list(CATALYST_ARMORY.get("SYMPATHY_MEGACAPS", {}).keys())
    EARNINGS_PREVIEW = list(CATALYST_ARMORY.get("EARNINGS_PREVIEW", {}).keys())
    MA_WORDS = CATALYST_ARMORY.get("MEGA_CATALYSTS", {})

    if "?" in text or any(trap in text for trap in HINDSIGHT_TRAPS) or all(x in text for x in ["WHY", "IS"]) or all(x in text for x in ["WHY", "ARE"]):
        return 0, False, []
    if any(trap in text for trap in CLASS_ACTION_SPAM): return 0, False, []
    if any(mc in text for mc in MEGACAPS):
        partnership_words = ["PARTNERSHIP", "COLLABORATION", "CONTRACT", "AGREEMENT", "JOINS", "INTEGRATES"]
        if not any(pw in text for pw in partnership_words): return 0, False, [] 
    if any(trap in text for trap in EARNINGS_PREVIEW): return 0, False, []

    # 💡 毒藥與解藥邏輯 (V46)：先扣分，如果看到解藥再加回來
    is_toxic = False
    for kw, score in CATALYST_ARMORY.get("TOXIC_OFFERINGS", {}).items():
        if kw in text: 
            total_score += score
            is_toxic = True
    
    for kw, score in CATALYST_ARMORY.get("INVERTED_TRAPS", {}).items():
        if kw in text:
            total_score += score
            is_toxic = False # 被解藥化解，解除陷阱狀態
            elite_hits.append(kw)

    for kw, score in MA_WORDS.items():
        if kw in text: total_score += score
        
    for kw, data in get_live_trends().items():
        if kw in text:
            score = data.get("score", 5)
            total_score += score
            if score >= 10 or data.get("count", 0) >= 3: elite_hits.append(kw)

    # 包含 V45 的所有生技與熱點分類
    for cat in ["CLINICAL_SUCCESS", "COMPLIANCE_WINS", "THEMATIC_TRENDS"]: 
        for kw, score in CATALYST_ARMORY.get(cat, {}).items():
            if kw in text: 
                total_score += score
                if score >= 8: elite_hits.append(kw)
                
    return total_score, is_toxic, elite_hits

def extract_top_catalysts(master_brain):
    top_list = []
    for ticker, data in master_brain.get('details', {}).items():
        if data.get('NewsList', []): 
            data['NewsList'].sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
            top_list.append(data)
    try: return sorted(top_list, key=lambda x: (x['NewsList'][0].get('time', '00-00 00:00'), x.get('CatScore', 0)), reverse=True)
    except: return top_list

def background_translate_worker(ticker, en_headline, master_brain):
    try:
        zh_text = GoogleTranslator(source='auto', target='zh-TW').translate(en_headline)
        with brain_lock:
            found = False
            for t in ticker.split(','):
                if t in master_brain['details']:
                    for article in master_brain['details'][t].get('NewsList', []):
                        if article.get('raw_title') == en_headline and article['title'] == "⏳ 翻譯中...": article['title'] = zh_text; found = True
    except: pass

def check_sec_fatal_traps(ticker):
    headers = {"User-Agent": "SniperQuantSystem_V45 AdminContact@yourdomain.com", "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"}
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=&output=atom"
    try:
        time.sleep(0.5) 
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for entry in root.findall('{http://www.w3.org/2005/Atom}entry')[:3]:
                title_elem = entry.find('{http://www.w3.org/2005/Atom}title')
                if title_elem is not None and any(trap in title_elem.text.upper() for trap in ['S-1', 'S-3', 'F-1', 'F-3']): return True
        return False
    except: return False

def fetch_and_score_news(ticker, cell, force=False):
    now = time.time()
    if not force and ticker in news_cache and (now - news_cache.get(ticker, 0) < 900): return
    news_cache[ticker] = now
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []; all_elites = set(); has_trap = False
            now_tpe = datetime.now(TZ_TW)
            three_days_ago_tpe = now_tpe.date() - timedelta(days=3)
            
            with brain_lock:
                finnhub_titles = {n['raw_title'] for n in cell.get('NewsList', []) if n.get('source') == 'Finnhub PR'}
            
            for item in root.findall('./channel/item')[:5]:
                dt_tpe = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_TW)
                if dt_tpe.date() < three_days_ago_tpe: continue
                raw_t = item.find('title').text
                if raw_t in finnhub_titles: continue 
        
                if is_news_echo(raw_t, dt_tpe, cell.get('NewsList', [])):
                    # 💡 重複新聞 (Echo) 不直接歸零，保留原始分數並打上標記
                    score, is_trap, elites = calculate_hft_score(raw_t, ticker)
                    raw_t = "[Echo] " + raw_t
                else:
                    score, is_trap, elites = calculate_hft_score(raw_t, ticker)
                
                if is_trap: has_trap = True
                all_elites.update(elites)
                
                p_news = cell.get('HighVal', 0.0)
                articles.append({
                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": raw_t,
                    "link": item.find('link').text, "time": dt_tpe.strftime("%m-%d %H:%M"),
                    "is_today": (dt_tpe.date() == now_tpe.date()), 
                    "score": score, "elites": list(elites), "source": "Yahoo",
                    "p_news": p_news, "max_p_15m": p_news, "fetch_time_ts": time.time()
                })
            
            with brain_lock:
                combined = articles + [n for n in cell.get('NewsList', []) if n['raw_title'] not in {a['raw_title'] for a in articles}]
                combined.sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
                cell["NewsList"] = combined[:10]
                if cell["NewsList"]: cell["CatScore"] = max(n['score'] for n in cell["NewsList"])
                else: cell["CatScore"] = 0
                cell["IsTrap"] = cell.get("IsTrap", False) or has_trap 
                cell["HasNews"] = len(cell["NewsList"]) > 0 
                MASTER_BRAIN["top_catalysts"] = extract_top_catalysts(MASTER_BRAIN)
                for e in all_elites:
                    if e not in MASTER_BRAIN["elite_words"]: MASTER_BRAIN["elite_words"].append(e)

            if articles:
                for art in articles:
                    if art['title'] == "⏳ 翻譯中...":
                        threading.Thread(target=background_translate_worker, args=(ticker, art['raw_title'], MASTER_BRAIN), daemon=True).start()
                        time.sleep(0.5) 
    except: pass

def finnhub_news_monitor_worker():
    if not FINNHUB_TOKEN: return
    time.sleep(10)
    seen_news_urls = set()
    while True:
        try:
            with brain_lock: current_watchlist = DYNAMIC_WATCHLIST.copy()
            if not current_watchlist: time.sleep(10); continue
            now_ny = datetime.now(TZ_NY)
            end_date = now_ny.strftime('%Y-%m-%d'); start_date = (now_ny - timedelta(days=3)).strftime('%Y-%m-%d')

            for ticker in current_watchlist:
                try:
                    url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={start_date}&to={end_date}&token={FINNHUB_TOKEN}"
                    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    if res.status_code == 200:
                        for art in res.json()[:5]:
                            art_url = art.get('url', ''); art_headline = art.get('headline', '')
                            if not art_url or not art_headline or art_url in seen_news_urls: continue
                            dt_tpe = datetime.fromtimestamp(art.get('datetime', time.time()), pytz.UTC).astimezone(TZ_TW)
                            with brain_lock:
                                cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                                if is_news_echo(art_headline, dt_tpe, cell.get('NewsList', [])):
                                    score, is_trap, elites = calculate_hft_score(art_headline, ticker)
                                    art_headline = "[Echo] " + art_headline
                                else:
                                    score, is_trap, elites = calculate_hft_score(art_headline, ticker)

                                p_news = cell.get('HighVal', 0.0)
                                new_article = {
                                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": art_headline, "link": art_url,
                                    "time": dt_tpe.strftime("%m-%d %H:%M"), "is_today": (dt_tpe.date() == datetime.now(TZ_TW).date()),
                                    "score": score, "elites": list(elites), "source": "Finnhub PR",
                                    "p_news": p_news, "max_p_15m": p_news, "fetch_time_ts": time.time()
                                }

                                if not any(n['link'] == art_url for n in cell['NewsList']):
                                    cell['NewsList'].append(new_article)
                                    cell['NewsList'].sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
                                    cell['NewsList'] = cell['NewsList'][:10]
                                    cell["CatScore"] = max(n['score'] for n in cell["NewsList"])
                                    cell["IsTrap"] = cell.get("IsTrap", False) or is_trap
                                    cell["HasNews"] = True
                                    MASTER_BRAIN["top_catalysts"] = extract_top_catalysts(MASTER_BRAIN)
                                    for e in elites:
                                        if e not in MASTER_BRAIN["elite_words"]: MASTER_BRAIN["elite_words"].append(e)
                                    threading.Thread(target=background_translate_worker, args=(ticker, art_headline, MASTER_BRAIN), daemon=True).start()
                                    time.sleep(0.5)

                            seen_news_urls.add(art_url) 
                            if len(seen_news_urls) > 1000: seen_news_urls.pop() 
                except: pass
                time.sleep(1.5)
        except: time.sleep(30) 

def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP
    try:
        url = "https://scanner.tradingview.com/america/scan"
        new_watchlist = []
        temp_stats = {}

        now_ny = datetime.now(TZ_NY)
        is_premarket = now_ny.time() < datetime.strptime("09:30", "%H:%M").time()

        chg_col = "premarket_change" if is_premarket else "change"
        vol_col = "premarket_volume" if is_premarket else "volume"

        payload = {
            "filter": [
                {"left": "type", "operation": "in_range", "right": ["stock", "fund"]}, 
                {"left": "close", "operation": "in_range", "right": [1, 50]}, 
                {"left": chg_col, "operation": "egreater", "right": 2.0}, 
                {"left": vol_col, "operation": "egreater", "right": 10000} 
            ], 
            "columns": ["name", "close", "change", "volume", "premarket_close", "premarket_change", "premarket_volume", "market_cap_basic", "type"], 
            "sort": {"sortBy": chg_col, "sortOrder": "desc"}, 
            "range": [0, 40]
        }

        res = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        data = res.json()
        if data.get('data'):
            for x in data['data']:
                sym, c, pct, v, pm_c, pm_pct, pm_v, mc, t = x['d'][0], x['d'][1], x['d'][2], x['d'][3], x['d'][4], x['d'][5], x['d'][6], x['d'][7], x['d'][8]
                if sym not in new_watchlist: new_watchlist.append(sym)
                
                p_eff = pm_c if is_premarket and pm_c is not None else c
                actual_pct = pm_pct if is_premarket and pm_pct is not None else pct
                if p_eff is None: p_eff = c
                if actual_pct is None: actual_pct = 0.0

                real_shares = get_real_float(sym)
                if real_shares > 0:
                    float_m = real_shares / 1_000_000
                else:
                    float_m = (mc / p_eff) / 1_000_000 if mc and p_eff else 0.0
                
                prev_est = p_eff / (1 + (actual_pct/100)) if actual_pct != -100 else p_eff
                
                f_str = "N/A (ETF)" if t == 'fund' else (f"{float_m:.1f}M" if float_m > 0 else "未知")
                if t != 'fund' and float_m > 50.0: f_str = f"⚠️{f_str}"
                
                float_comp = 100.0 / math.sqrt(float_m) if float_m > 0 else 0.0
                
                temp_stats[sym] = {
                    'prev': prev_est, 'float_str': f_str, 'type': t, 
                    'float_comp': float_comp
                }
        
        with brain_lock:
            DYNAMIC_WATCHLIST.clear()
            DYNAMIC_WATCHLIST.extend(new_watchlist[:40]) 
            STATS_MAP.update(temp_stats)
    except: pass

def scanner_engine():
    global _last_list_update, SPY_real_pct, _last_spy_update, MIN_RELVOL_LIMIT
    tv = TvDatafeed()
    try:
        if TW_USERNAME != 'guest': tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
    except: pass

    consecutive_errors = 0 
    while True:
        try:
            now_ts = time.time()
            if now_ts - _last_spy_update > 30:
                try:
                    url = "https://scanner.tradingview.com/america/scan"
                    payload = {"symbols": {"tickers": ["AMEX:SPY"]}, "columns": ["close", "premarket_change", "change"]}
                    res = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    data = res.json()
                    if data.get('data'):
                        spy_data = data['data'][0]['d']
                        now_ny = datetime.now(TZ_NY)
                        pct = spy_data[1] if now_ny.time() < datetime.strptime("09:30", "%H:%M").time() and spy_data[1] is not None else spy_data[2]
                        if pct is not None: SPY_real_pct = float(pct)
                    _last_spy_update = now_ts
                except: pass

            if now_ts - _last_list_update > 300: 
                update_dynamic_watchlist()
                _last_list_update = now_ts
                
            if not DYNAMIC_WATCHLIST: time.sleep(5); continue
            current_watchlist = DYNAMIC_WATCHLIST.copy()

            def _process_ticker(ticker):
                nonlocal consecutive_errors, tv
                try:
                    time.sleep(0.5) 
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=960, extended_session=True)
                    if df is None or df.empty or len(df) < 5: 
                        consecutive_errors += 1
                        if consecutive_errors > 15:
                            tv = TvDatafeed()
                            try:
                                if TW_USERNAME != 'guest': tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
                            except: pass
                            consecutive_errors = 0
                        return
                    consecutive_errors = 0 

                    time_diff = df.index.to_series().diff()
                    new_sessions = time_diff[time_diff > pd.Timedelta(hours=4)]
                    if not new_sessions.empty:
                        session_start = new_sessions.index[-1]
                        today_df = df.loc[session_start:]
                    else:
                        last_date = df.index[-1].date()
                        today_df = df[df.index.date == last_date]

                    p_live = float(df['close'].iloc[-1])
                    p_prev = float(df['close'].iloc[-2]) if len(df) > 1 else p_live
                    v_live = float(df['volume'].iloc[-1])
                    v_prev = float(df['volume'].iloc[-2]) if len(df) > 1 else v_live
                    
                    # 💡 持續更新大數據資料庫中該股的 15 分鐘最高點
                    collector.update_max_price(ticker, p_live)
                    
                    daily_vol = int(today_df['volume'].sum()) if not today_df.empty else int(v_live)

                    try:
                        daily_df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_daily, n_bars=2)
                        if daily_df is not None and not daily_df.empty:
                            true_prev_close = float(daily_df['close'].iloc[-2]) if len(daily_df) > 1 else float(daily_df['open'].iloc[0])
                        else:
                            true_prev_close = 0.0
                    except: true_prev_close = 0.0

                    stat_data = STATS_MAP.get(ticker, {'prev': p_live, 'float_str': '-', 'type': 'stock', 'float_comp': 0.0})
                    
                    time_lapsed_mins = len(today_df)
                    avg_vol = float(today_df['volume'].iloc[:-1].mean()) if time_lapsed_mins > 2 else v_live
                    if avg_vol == 0: avg_vol = 1.0 
                    rel_vol_live = float(round(v_live / avg_vol, 2))
                    rel_vol_prev = float(round(v_prev / avg_vol, 2))
                    rel_vol_display = max(rel_vol_live, rel_vol_prev)
                    vol_3m = float(df['volume'].iloc[-3:].sum()) if len(df) >= 3 else v_live

                    zero_vol_mins = 0
                    if time_lapsed_mins >= 15:
                        last_15_vols = today_df['volume'].iloc[-15:].values
                        zero_vol_mins = (last_15_vols == 0).sum()
                    is_continuous = (zero_vol_mins <= 3) 

                    if time_lapsed_mins > 0:
                        typical_price = (today_df['high'] + today_df['low'] + today_df['close']) / 3
                        cumulative_vol = today_df['volume'].cumsum()
                        vwap_series = (typical_price * today_df['volume']).cumsum() / cumulative_vol
                        current_vwap = float(vwap_series.iloc[-1])
                        vwap_dev = float(round(((p_live - current_vwap) / current_vwap) * 100, 2)) if current_vwap > 0 else 0.0
                    else: current_vwap = p_live; vwap_dev = 0.0

                    vwap_rating = "High" if vwap_dev > 15.0 else ("Mid" if vwap_dev > 5.0 else "Low") 
                    df['tr'] = pd.concat([(df['high'] - df['low']), (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
                    
                    prev_close = true_prev_close if true_prev_close > 0 else (float(stat_data['prev']) if stat_data['prev'] > 0 else float(df['low'].min()))
                    real_pct = float(((p_live - prev_close) / prev_close) * 100) if prev_close > 0 else 0.0
                    
                    real_float = get_real_float(ticker)
                    float_m = real_float / 1_000_000 if real_float > 0 else 999.0
                    
                    if stat_data.get('type') == 'fund':
                        float_str = "N/A (ETF)"
                    elif real_float > 0:
                        float_str = f"⚠️{float_m:.1f}M" if float_m > 50.0 else f"{float_m:.1f}M"
                    else:
                        float_str = "未知"

                    curr_ema9 = float(df['close'].ewm(span=9, adjust=False).mean().iloc[-1]) if len(df) >= 9 else p_live
                    curr_ema20 = float(df['close'].ewm(span=20, adjust=False).mean().iloc[-1]) if len(df) >= 20 else p_live
                    curr_ema52 = float(df['close'].ewm(span=52, adjust=False).mean().iloc[-1]) if len(df) >= 52 else p_live
                    past_high = float(df['high'].iloc[-11:-1].max()) if len(df) >= 11 else p_live
                    
                    is_spark = bool(((rel_vol_live >= 2.5 and p_live >= past_high) or (rel_vol_prev >= 2.5 and p_prev >= past_high)) and (real_pct > 3.0))
                    is_ross_match = bool(real_pct >= 4.0 and float_m <= 50.0 and rel_vol_display >= 1.8)
                    is_vwap_breakout = bool(p_prev < current_vwap and p_live > current_vwap and rel_vol_display >= 1.5 and float_m <= 50.0)
                    is_dead_bounce = bool((p_live < current_vwap) and (curr_ema9 < curr_ema20) and (real_pct > 0))

                    is_micro_pullback = False
                    if len(df) >= 3 and float_m <= 50.0:
                        prev_open = float(df['open'].iloc[-2])
                        prev_high = float(df['high'].iloc[-2])
                        if p_prev <= prev_open and p_prev > curr_ema9:
                            if p_live > prev_high and rel_vol_live >= 1.5:
                                is_micro_pullback = True

                    is_whole_dollar = False
                    if float_m <= 50.0 and rel_vol_display >= 1.5 and p_live > p_prev:
                        remainder = p_live % 1.0
                        if 0.90 <= remainder <= 0.98:
                            is_whole_dollar = True
                            target_dollar = math.ceil(p_live)

                    is_massive_inflow = False
                    if len(df) >= 10:
                        recent_vol_max = float(df['volume'].iloc[-11:-1].max()) if len(df) >= 11 else v_live
                        if v_live > recent_vol_max and v_live >= avg_vol * 3.0 and p_live >= df['open'].iloc[-1]:
                            is_massive_inflow = True

                    ratio_3v3 = 1.0; vol_acc_str = "-"; vr_acc = 0.0 
                    
                    if len(df) >= 6:
                        vol_A = vol_3m
                        vol_B = float(df['volume'].iloc[-6:-3].sum())
                        vol_C = float(df['volume'].iloc[-9:-6].sum()) if len(df) >= 9 else 0.0
                        
                        if vol_B >= 0: vr_acc = float(round(((vol_A - vol_B) / max(vol_B, 0.01)) * 100, 2))
                        if vol_B > 0:
                            ratio_3v3 = float(vol_A / vol_B)
                            sym_up, sym_extreme = ("↗", "🔥") if p_live >= p_prev else ("🔻", "🩸")
                            
                            if len(df) >= 9:
                                if vol_A > vol_B > vol_C: vol_acc_str = f"{sym_extreme} {ratio_3v3:.2f}x"
                                elif vol_A > vol_B and vol_B <= vol_C: vol_acc_str = f"{sym_up} {ratio_3v3:.2f}x"
                                elif vol_A < vol_B < vol_C: vol_acc_str = f"🧊 {ratio_3v3:.2f}x"
                                elif vol_A < vol_B and vol_B >= vol_C: vol_acc_str = f"↘ {ratio_3v3:.2f}x"
                                else: vol_acc_str = f"- {ratio_3v3:.2f}x"
                            else:
                                vol_acc_str = f"{sym_up} {ratio_3v3:.2f}x" if vol_A > vol_B else f"↘ {ratio_3v3:.2f}x"

                    # 💡 V46：物理乘數 (Float Multiplier)
                    base_sn_score = (stat_data.get('float_comp', 0) + real_pct) * (ratio_3v3 if ratio_3v3 > 1.0 else 0.5)
                    if float_m < 2.0:
                        base_sn_score *= 2.5 # 妖股加乘
                    elif float_m > 80.0:
                        base_sn_score = min(base_sn_score, 30.0) # 巨頭封頂
                    sn_score = int(round(base_sn_score))
                    
                    vol_warn = " (⚠️量低)" if (p_live * max(v_live, v_prev, avg_vol)) < 50000 else ""
                    if not is_continuous:
                        vol_warn += " (❄️斷層停滯)"

                    status_color = "green"
                    current_signal = ""
                    
                    if is_dead_bounce and is_ross_match:
                        current_signal = f"🚨水下反彈警報{vol_warn}"
                        status_color = "red"
                    elif is_micro_pullback:
                        current_signal = f"🚩牛旗回踩突破{vol_warn}"
                        status_color = "yellow"
                    elif is_whole_dollar:
                        current_signal = f"🧲整數磁吸(${target_dollar:.2f}){vol_warn}"
                        status_color = "yellow"
                    elif is_vwap_breakout:
                        current_signal = f"⚔️VWAP帶量突破{vol_warn}"
                        status_color = "yellow"
                    elif is_ross_match:
                        current_signal = f"🚀Ross勢頭確立{vol_warn}"
                        status_color = "yellow"
                    elif is_spark:
                        current_signal = f"🔥強力點火{vol_warn}"
                        status_color = "yellow"
                        
                    if is_massive_inflow:
                        if current_signal:
                            current_signal += " [📦爆量箱子]"
                        else:
                            current_signal = f"📦爆量箱子(大量進場){vol_warn}"
                        status_color = "purple"

                    final_vol_text = format_vol(daily_vol)
                    if is_massive_inflow:
                        final_vol_text = f'<span style="color: #d942f5; font-weight: 900; text-shadow: 0 0 6px rgba(217, 66, 245, 0.5);">📦 {final_vol_text}</span>'

                    delta_threshold = 0.03 if p_live < 5.0 else 0.015
                    last_alert_info = STATE_TRACKER.get(f"{ticker}_last_alert", {"price": p_live, "vol": daily_vol})
                    last_alert_price = last_alert_info["price"]
                    last_alert_vol = last_alert_info["vol"]
                    price_change_ratio = (p_live - last_alert_price) / last_alert_price if last_alert_price > 0 else 0.0
                    vol_increase = daily_vol - last_alert_vol
                    
                    trigger_type = "none"
                    delta_pct_str = ""
                    
                    if price_change_ratio >= delta_threshold:
                        trigger_type = "up"
                        delta_pct_str = f"+{(price_change_ratio*100):.1f}% ↗"
                        STATE_TRACKER[f"{ticker}_last_alert"] = {"price": p_live, "vol": daily_vol}
                    elif price_change_ratio <= -delta_threshold:
                        trigger_type = "down"
                        delta_pct_str = f"{(price_change_ratio*100):.1f}% ↘"
                        STATE_TRACKER[f"{ticker}_last_alert"] = {"price": p_live, "vol": daily_vol}
                    elif vol_increase > 500000 or (last_alert_vol > 0 and vol_increase / last_alert_vol > 1.0):
                        trigger_type = "vol_spike"
                        delta_pct_str = f"📦 大單"
                        STATE_TRACKER[f"{ticker}_last_alert"] = {"price": p_live, "vol": daily_vol}

                    with brain_lock:
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False, "StickySignal": "", "StickyColor": "green", "StickyTime": 0})
                        
                        # 💡 V46: 情緒反轉 (Sentiment Flip) 偵測
                        has_sentiment_flip = False
                        if cell.get("CatScore", 0) < 0 and p_live > current_vwap and curr_ema9 > curr_ema20:
                            # 利空不跌，強行校正
                            cell["CatScore"] = 60
                            cell["IsTrap"] = False
                            has_sentiment_flip = True
                            if not current_signal: current_signal = "🚀"
                            current_signal += " [💎 利空不跌]"
                            status_color = "purple"
                        
                        if current_signal:
                            if "⚡" in cell.get("StickySignal", "") and now_ts - cell.get("StickyTime", 0) < 15:
                                current_signal = current_signal + " [⚡極速]"
                                status_color = "purple"
                            
                            cell["StickySignal"] = current_signal
                            cell["StickyColor"] = status_color
                            cell["StickyTime"] = now_ts
                        else:
                            if now_ts - cell.get("StickyTime", 0) < 900: # 900秒 = 15分鐘
                                current_signal = cell.get("StickySignal", "")
                                status_color = cell.get("StickyColor", "green")

                        stats = {
                            "Code": ticker, "Price": f"${p_live:.2f}", "RelVol": f"{rel_vol_display}x", "Vol": final_vol_text,
                            "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p_live-prev_close):+.2f}", "Status": status_color, "Signal": current_signal,
                            "PriceVal": float(p_live), "HighVal": float(df['high'].iloc[-1]), "StopLoss": curr_ema20 * 0.99, 
                            "Float": float_str, "Type": stat_data.get('type', 'stock'), "VolAcc": vol_acc_str, "Is100K": False, 
                            "SN_Score": sn_score, "VsaState": 0, "VR_Acc": vr_acc, "VWAP_Dev": vwap_dev, "VWAP_Rating": vwap_rating,
                            "EMA9": curr_ema9, "EMA52": curr_ema52,
                            "TriggerType": trigger_type, 
                            "DeltaStr": delta_pct_str,
                            "TriggerTS": now_ts if trigger_type != "none" else cell.get("TriggerTS", 0),
                            "HasSentimentFlip": has_sentiment_flip # 傳遞給前端渲染金黃色標籤
                        }
                        
                        cell.update(stats)
                        
                        if is_ross_match or is_spark or is_dead_bounce or is_massive_inflow or is_vwap_breakout or is_micro_pullback or is_whole_dollar:
                            last_trigger_time = cooldown_tracker.get(ticker, 0)
                            last_massive_time = STATE_TRACKER.get(f"{ticker}_massive", 0)
                            
                            can_trigger = False
                            if now_ts - last_trigger_time > 60:
                                can_trigger = True
                            elif is_massive_inflow and (now_ts - last_massive_time > 60):
                                can_trigger = True
                                
                            if can_trigger:
                                if is_massive_inflow: STATE_TRACKER[f"{ticker}_massive"] = now_ts
                                cooldown_tracker[ticker] = now_ts 
                                
                                is_sec_trap = check_sec_fatal_traps(ticker)
                                if is_sec_trap and not has_sentiment_flip:
                                    stats["Signal"] += " 💀(SEC陷阱)"
                                    cell["IsTrap"] = True
                                    
                                MASTER_BRAIN["surge_log"].insert(0, {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova"})
                                MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:1000]
                                
                                # 💡 V46: 觸發大數據記錄 (只記錄帶有新聞的爆發)
                                if cell.get('NewsList'):
                                    headline = cell['NewsList'][0].get('raw_title', '')
                                    collector.log_event(ticker, headline, float_m, float(p_live))
                                
                    threading.Thread(target=fetch_and_score_news, args=(ticker, cell, True), daemon=True).start()
                except Exception as e: return

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                list(executor.map(_process_ticker, current_watchlist))
            
            with brain_lock:
                all_items = list(MASTER_BRAIN["details"].values())
                active_items = [x for x in all_items if x.get("Code") in DYNAMIC_WATCHLIST]
                MASTER_BRAIN["leaderboard"] = sorted(active_items, key=lambda x: float(x.get('Pct', '0').replace('%', '')), reverse=True)[:20]
                
                vwap_items = [x for x in active_items if x.get("VR_Acc", 0) > 30.0 and x.get("VWAP_Dev", 0) > 0.0]
                MASTER_BRAIN["vwap_list"] = sorted(vwap_items, key=lambda x: x.get("VWAP_Dev", 0), reverse=True)
                
                MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
            time.sleep(1)
        except Exception as e: time.sleep(5)

def auto_trend_updater():
    global MASTER_BRAIN
    while True:
        time.sleep(3600) 
        now_ny = datetime.now(TZ_NY)
        if now_ny.hour == 16 and 30 <= now_ny.minute <= 59:
            try:
                word_stats = {}
                stop_words = {'WITH', 'THAT', 'THIS', 'FROM', 'THEIR', 'WILL', 'HAVE', 'BEEN', 'WERE', 'AFTER', 'OVER', 'MORE', 'THAN', 'ABOUT', 'INC.', 'CORP', 'LTD', 'ANNOUNCES', 'REPORTS', 'TODAY', 'UPDATE', 'QUARTERLY', 'RESULTS', 'SHARES', 'STOCK', 'COMPANY', 'EARNINGS', 'THIRD', 'FOURTH', 'FIRST', 'SECOND', 'QUARTER', 'FINANCIAL', 'OPERATIONAL', 'BUSINESS'}
                hindsight = set(CATALYST_ARMORY.get("HINDSIGHT_NOISE", {}).keys())
                toxic = set(CATALYST_ARMORY.get("TOXIC_OFFERINGS", {}).keys())
                
                with brain_lock:
                    for ticker, data in MASTER_BRAIN.get('details', {}).items():
                        try: pct = float(str(data.get('Pct', '0')).replace('%', '').replace('+', ''))
                        except: pct = 0.0
                        float_str = str(data.get('Float', ''))
                        try: float_val = float(float_str.replace('M', '')) if 'M' in float_str else 999.0
                        except: float_val = 999.0
                            
                        if pct >= 15.0 and float_val <= 50.0:
                            for news in data.get('NewsList', []):
                                if news.get('is_today'):
                                    title = news.get('raw_title', '').upper()
                                    # 💡 V46 升級：字元限制從 4 放寬為 3
                                    words = set(re.findall(r'\b[A-Z]{3,}\b', title))
                                    words = words - stop_words
                                    words = {w for w in words if not any(trap in w for trap in hindsight) and not any(trap in w for trap in toxic)}
                                    for w in words:
                                        if w not in word_stats: word_stats[w] = {'pcts': [], 'tickers': set()}
                                        word_stats[w]['pcts'].append(pct)
                                        word_stats[w]['tickers'].add(ticker)
                                        
                new_drafts = {}
                for w, stats in word_stats.items():
                    if len(stats['tickers']) >= 2:
                        avg_pct = sum(stats['pcts']) / len(stats['pcts'])
                        if avg_pct >= 20.0:
                            new_drafts[w] = {
                                "score": 10, "count": len(stats['tickers']),
                                "avg_impact_pct": round(avg_pct, 1),
                                "note": f"🤖 AI 自動學習: 今日在 {len(stats['tickers'])} 檔暴漲股 ({','.join(list(stats['tickers'])[:3])}) 中同時出現"
                            }
                if new_drafts:
                    if os.path.exists(TRENDS_DRAFT_PATH):
                        with open(TRENDS_DRAFT_PATH, 'r', encoding='utf-8') as f:
                            try: existing = json.load(f); existing.update(new_drafts); new_drafts = existing
                            except: pass
                    with open(TRENDS_DRAFT_PATH, 'w', encoding='utf-8') as f:
                        json.dump(new_drafts, f, ensure_ascii=False, indent=4)
            except Exception as e: print(f"學習引擎錯誤: {e}")
            time.sleep(43200) 

@app.route('/api/config', methods=['POST'])
def update_config():
    global MIN_RELVOL_LIMIT, _last_list_update
    data = request.json
    if 'relvol_limit' in data:
        try: MIN_RELVOL_LIMIT = float(data['relvol_limit'])
        except: pass
    _last_list_update = 0 
    return jsonify({"status": "success", "relvol_limit": MIN_RELVOL_LIMIT})

@app.route('/api/export_news')
def export_news():
    rows = []
    with brain_lock:
        for ticker_data in MASTER_BRAIN.get('top_catalysts', []):
            ticker = ticker_data.get('Code') or ticker_data.get('ticker')
            for n in ticker_data.get('NewsList', []):
                rows.append({
                    "發布時間": n.get('time'), "代碼": ticker, "總分": ticker_data.get('CatScore', 0),
                    "標題": n.get('title'), "價格": ticker_data.get('Price', '-'), "漲幅": ticker_data.get('Pct', '-')
                })
    if not rows: return "無數據", 404
    df = pd.DataFrame(rows); output = io.BytesIO(); df.to_csv(output, index=False, encoding='utf-8-sig'); output.seek(0)
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name="news.csv")

# 💡 V46: 新增匯出戰略情報數據庫的 API
@app.route('/api/export_intelligence')
def export_intelligence():
    export_path = collector.export_to_json()
    if export_path and os.path.exists(export_path):
        return send_file(export_path, mimetype='application/json', as_attachment=True, download_name="sniper_intelligence_export.json")
    return jsonify({"status": "error", "message": "無已結案資料可匯出"}), 404

@app.route('/api/drafts', methods=['GET'])
def get_drafts():
    if os.path.exists(TRENDS_DRAFT_PATH):
        with open(TRENDS_DRAFT_PATH, 'r', encoding='utf-8') as f:
            try: return jsonify(json.load(f))
            except: return jsonify({})
    return jsonify({})

@app.route('/api/approve_draft', methods=['POST'])
def approve_draft():
    data = request.json
    word = data.get('word'); action = data.get('action')
    if not os.path.exists(TRENDS_DRAFT_PATH): return jsonify({"status": "error"})
    
    with open(TRENDS_DRAFT_PATH, 'r', encoding='utf-8') as f: drafts = json.load(f)
    if word in drafts:
        if action == 'approve':
            trends = {}
            if os.path.exists(TRENDS_FILE_PATH):
                with open(TRENDS_FILE_PATH, 'r', encoding='utf-8') as f:
                    try: trends = json.load(f)
                    except: pass
            trends[word] = drafts[word]
            with open(TRENDS_FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(trends, f, ensure_ascii=False, indent=4)
                
        del drafts[word]
        with open(TRENDS_DRAFT_PATH, 'w', encoding='utf-8') as f:
            json.dump(drafts, f, ensure_ascii=False, indent=4)
        return jsonify({"status": "success"})
    return jsonify({"status": "error"})

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data():
    with brain_lock: 
        safe_brain = copy.deepcopy(MASTER_BRAIN)
        safe_brain["live_trends"] = get_live_trends()
        safe_brain["current_relvol"] = MIN_RELVOL_LIMIT 
    return jsonify(safe_brain)

if __name__ == '__main__':
    collector.init_db() # 💡 初始化戰略數據庫
    load_float_cache()
    load_intraday_state()
    threading.Thread(target=state_auto_save_worker, daemon=True).start()
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=finnhub_news_monitor_worker, daemon=True).start()
    threading.Thread(target=auto_trend_updater, daemon=True).start()
    
    init_alpaca(MASTER_BRAIN, DYNAMIC_WATCHLIST, brain_lock)
    
    app.run(host='0.0.0.0', port=PORT, use_reloader=False)