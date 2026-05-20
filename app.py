import os
import sys
import warnings

# --- 🌌 絕對領域：終極警告封殺器 ---
os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")

class CleanStderr:
    def __init__(self, original_stderr):
        self.original_stderr = original_stderr
    def write(self, msg):
        if "Pandas4Warning" in msg or "Timestamp.utcnow" in msg or "FutureWarning" in msg or "deprecated" in msg:
            return
        self.original_stderr.write(msg)
    def flush(self):
        self.original_stderr.flush()

sys.stderr = CleanStderr(sys.stderr)
# --------------------------------

import logging
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
logging.getLogger('tvDatafeed').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

from flask import Flask, jsonify, render_template, request, send_file
import io, time, threading, json, re, base64, copy, math
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

from alpaca_worker import init_alpaca
import collector
import memory_worker 

app = Flask(__name__)

# ==========================================
# 🛡️ 防禦裝甲：確保傳給前端的數字絕不崩潰
# ==========================================
def safe_float(v):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f): return 0.0
        return f
    except: return 0.0

# ==========================================
# 🚀 Ross 格式化引擎：動態 K/M 切換
# ==========================================
def format_shares_k_m(n):
    if n <= 0 or math.isnan(n): return "未知"
    if n < 1_000_000:
        return f"{n/1000:.2f}K"
    return f"{n/1_000_000:.2f}M"

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.2f}K"
    return str(int(n))

# ==========================================
# 🛠️ 拆股異常校準器 (YFinance Fallback)
# ==========================================
def fetch_yfinance_prev_close(ticker):
    try:
        tkr = yf.Ticker(ticker.replace('-', '.'))
        hist = tkr.history(period="5d")
        if not hist.empty and len(hist) >= 2:
            return float(hist['Close'].iloc[-2])
        elif len(hist) == 1:
            return float(hist['Close'].iloc[0])
    except:
        pass
    return 0.0

# ==========================================
# 🌍 動態時區校準引擎 (完美處理美東夏/冬令時差)
# ==========================================
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

def convert_to_taiwan_time(raw_time, source="yahoo"):
    try:
        if source == "yahoo":
            dt_obj = parsedate_to_datetime(raw_time)
            if dt_obj.tzinfo is None:
                dt_obj = TZ_NY.localize(dt_obj)
            return dt_obj.astimezone(TZ_TW)
        elif source == "finnhub":
            utc_dt = datetime.fromtimestamp(raw_time, pytz.UTC)
            return utc_dt.astimezone(TZ_TW)
    except Exception as e:
        return datetime.now(TZ_TW)

# ==========================================
# 📊 V58 核心數學引擎：CVD、HMA 與極限軌道
# ==========================================
def calc_wma(s, length):
    weights = np.arange(1, length + 1)
    return s.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def calc_hma(s, length):
    if len(s) < length: return s.copy() 
    half_length = int(length / 2)
    sqrt_length = int(np.sqrt(length))
    wmaf = calc_wma(s, half_length) * 2
    wmas = calc_wma(s, length)
    diff = wmaf - wmas
    return calc_wma(diff, sqrt_length)

# ==========================================

@app.route('/api/export_intelligence')
def export_intelligence():
    limit = request.args.get('limit', default=None, type=int)
    today = request.args.get('today', default='false').lower() == 'true'
    export_path = collector.export_corpus_csv(limit=limit, today_only=today) 
    if export_path and os.path.exists(export_path):
        filename = f"sniper_corpus_{datetime.now().strftime('%m%d_%H%M')}.csv"
        return send_file(export_path, mimetype='text/csv', as_attachment=True, download_name=filename)
    return jsonify({"status": "error", "message": "無符合條件之資料，或資料庫為空"}), 404

@app.route('/api/intelligence_summary')
def intelligence_summary():
    result = collector.generate_intelligence_summary()
    if result.get("status") == "success":
        return jsonify(result), 200
    else:
        return jsonify(result), 500

brain_lock = threading.RLock() 
TRENDS_FILE_PATH = os.path.join(os.path.dirname(__file__), 'trends.json')
DISCOVERY_LOG_PATH = os.path.join(os.path.dirname(__file__), 'discovery_log.json') 
SESSION_BACKUP_PATH = os.path.join(os.path.dirname(__file__), 'session_state.json')

# 🚀 V58.2: 雙股數快取 (Float & Outstanding)
SHARES_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'shares_cache.json')
SHARES_CACHE = {}

FINNHUB_TOKEN = os.getenv('FINNHUB_TOKEN')
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 8080))

_cached_trends = {}
_last_trends_update = 0
TRENDS_CACHE_TTL = 60
_last_list_update = 0
SPY_real_pct = 0.0
_last_spy_update = 0
MIN_RELVOL_LIMIT = 3.0 
TV_LOGIN_STATUS = "檢查中..." 

def get_current_trading_date():
    now_ny = datetime.now(TZ_NY)
    if now_ny.hour < 4:
        return (now_ny - timedelta(days=1)).strftime("%Y-%m-%d")
    return now_ny.strftime("%Y-%m-%d")

_current_session_date = get_current_trading_date()

CATALYST_ARMORY = {}
armory_path = os.path.join(os.path.dirname(__file__), 'catalysts.json')
learned_path = os.path.join(os.path.dirname(__file__), 'learned_catalysts.json') 

def reload_armory():
    global CATALYST_ARMORY
    try:
        with open(armory_path, 'r', encoding='utf-8') as f: 
            CATALYST_ARMORY = json.load(f)
    except: CATALYST_ARMORY = {}

    try:
        if os.path.exists(learned_path):
            with open(learned_path, 'r', encoding='utf-8') as f:
                learned_data = json.load(f)
                if "THEMATIC_TRENDS" not in CATALYST_ARMORY:
                    CATALYST_ARMORY["THEMATIC_TRENDS"] = {}
                CATALYST_ARMORY["THEMATIC_TRENDS"].update(learned_data)
    except: pass

reload_armory()

def load_shares_cache():
    global SHARES_CACHE
    if os.path.exists(SHARES_CACHE_FILE):
        try:
            with open(SHARES_CACHE_FILE, 'r', encoding='utf-8') as f: 
                SHARES_CACHE = json.load(f)
        except: pass

def save_shares_cache():
    try:
        with open(SHARES_CACHE_FILE, 'w', encoding='utf-8') as f: 
            json.dump(SHARES_CACHE, f)
    except: pass

# 🚀 V58.2: 獲取雙重股數 (Float 與 Outstanding)
def get_shares_data(symbol):
    if symbol in SHARES_CACHE and isinstance(SHARES_CACHE[symbol], dict):
        return SHARES_CACHE[symbol].get('float', 0.0), SHARES_CACHE[symbol].get('outstanding', 0.0)
    
    float_shares, out_shares = 0.0, 0.0
    try:
        yf_symbol = symbol.replace('-', '.')
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info
        yf_float = info.get('floatShares')
        yf_out = info.get('sharesOutstanding')
        
        if yf_float and yf_float > 0: float_shares = float(yf_float)
        if yf_out and yf_out > 0: out_shares = float(yf_out)
    except: pass
    
    if (float_shares == 0.0 or out_shares == 0.0) and FINNHUB_TOKEN:
        try:
            url = f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}&metric=all&token={FINNHUB_TOKEN}"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                metric = res.json().get('metric', {})
                fh_float = metric.get('floatShares', 0)
                fh_out = metric.get('sharesOutstanding', 0)
                if fh_float > 0 and float_shares == 0.0:
                    float_shares = fh_float * 1e6 if fh_float < 50000 else fh_float
                if fh_out > 0 and out_shares == 0.0:
                    out_shares = fh_out * 1e6 if fh_out < 50000 else fh_out
        except: pass
        
    if float_shares > 0 or out_shares > 0:
        SHARES_CACHE[symbol] = {'float': float_shares, 'outstanding': out_shares}
        save_shares_cache()
        
    return float_shares, out_shares

MASTER_BRAIN = {"surge_log": [], "details": {}, "leaderboard": [], "vwap_list": [], "top_catalysts": [], "last_update": "", "elite_words": []}
DYNAMIC_WATCHLIST = []
STATS_MAP = {} 
news_cache = {} 
cooldown_tracker = {}
STATE_TRACKER = {}

def load_intraday_state():
    global MASTER_BRAIN, DYNAMIC_WATCHLIST, STATS_MAP, cooldown_tracker, STATE_TRACKER
    try:
        if os.path.exists(SESSION_BACKUP_PATH):
            with open(SESSION_BACKUP_PATH, 'r', encoding='utf-8') as f: data = json.load(f)
            if data.get("date") == get_current_trading_date():
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
                    "date": get_current_trading_date(), "MASTER_BRAIN": MASTER_BRAIN, "DYNAMIC_WATCHLIST": DYNAMIC_WATCHLIST,
                    "STATS_MAP": STATS_MAP, "cooldown_tracker": cooldown_tracker, "STATE_TRACKER": STATE_TRACKER
                }
            with open(SESSION_BACKUP_PATH, 'w', encoding='utf-8') as f: json.dump(backup_data, f, ensure_ascii=False)
        except: pass

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

    CORE_WORDS = ["WOLFPACK", "AFRL", "MARINE CORPS", "FDA APPROVAL", "FAST TRACK", "DEPARTMENT OF DEFENSE", "DOD"]
    is_core = False
    for kw in CORE_WORDS:
        if kw in text:
            total_score += 100
            elite_hits.append("💎" + kw)
            is_core = True

    reload_armory()
    INVERTED_TRAPS = list(CATALYST_ARMORY.get("INVERTED_TRAPS", {}).keys())
    TOXIC_OFFERINGS = list(CATALYST_ARMORY.get("TOXIC_OFFERINGS", {}).keys())
    MEGACAPS = list(CATALYST_ARMORY.get("SYMPATHY_MEGACAPS", {}).keys())
    EARNINGS_PREVIEW = list(CATALYST_ARMORY.get("EARNINGS_PREVIEW", {}).keys())
    MA_WORDS = CATALYST_ARMORY.get("MEGA_CATALYSTS", {})

    is_toxic = False
    for kw, score in CATALYST_ARMORY.get("TOXIC_OFFERINGS", {}).items():
        if kw in text: 
            total_score += score
            is_toxic = True
    
    for kw, score in CATALYST_ARMORY.get("INVERTED_TRAPS", {}).items():
        if kw in text:
            total_score += score
            is_toxic = False 
            elite_hits.append(kw)

    for kw, score in MA_WORDS.items():
        if kw in text: total_score += score
        
    for kw, data in get_live_trends().items():
        if kw in text:
            score = data.get("score", 5)
            total_score += score
            if score >= 10 or data.get("count", 0) >= 3: elite_hits.append(kw)

    for cat in ["CLINICAL_SUCCESS", "COMPLIANCE_WINS", "THEMATIC_TRENDS"]: 
        for kw, score in CATALYST_ARMORY.get(cat, {}).items():
            if kw in text: 
                total_score += score
                if score >= 8: elite_hits.append(kw)
                
    return total_score, is_toxic, elite_hits

def update_dynamic_catscore(cell, current_vwap_dev=None, current_ema9=None, current_ema20=None, gap_pct=None):
    news_list = cell.get('NewsList', [])
    if not news_list:
        cell["CatScore"] = 0
        cell["IsTrap"] = False
        return
    
    news_list.sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
    latest_news = news_list[0]
    
    if latest_news.get('is_trap', False):
        cell["CatScore"] = -50
        cell["IsTrap"] = True
        cell["StickySignal"] = "💀 增發/毒藥陷阱"
        return
        
    best_score = 0
    now_ts = time.time()
    
    v_dev = current_vwap_dev if current_vwap_dev is not None else cell.get("VWAP_Dev", 0)
    e9 = current_ema9 if current_ema9 is not None else cell.get("EMA9", 0)
    e20 = current_ema20 if current_ema20 is not None else cell.get("EMA52", 0) 
    g_pct = gap_pct if gap_pct is not None else cell.get("GapPct", 0) 
    
    is_stagnant = (v_dev <= 0 or e9 <= e20)
        
    has_trap = False
    for art in news_list:
        if art.get('is_trap', False):
            has_trap = True
            
        art_score = art.get('score', 0)
        pub_ts = art.get('pub_ts', now_ts)
        
        if (now_ts - pub_ts) > 2700 and is_stagnant:
            art_score = int(art_score * 0.1)
            
        if art_score > best_score:
            best_score = art_score

    if best_score >= 50 and (g_pct <= -10.0 or v_dev <= -10.0):
        cell["CatScore"] = -99  
        cell["IsTrap"] = True
        cell["StickySignal"] = "💀 假利多真出貨"
        return

    cell["CatScore"] = best_score
    cell["IsTrap"] = has_trap

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
                dt_tpe = convert_to_taiwan_time(item.find('pubDate').text, source="yahoo")
                if dt_tpe.date() < three_days_ago_tpe: continue
                raw_t = item.find('title').text
                if raw_t in finnhub_titles: continue 
        
                if is_news_echo(raw_t, dt_tpe, cell.get('NewsList', [])):
                    score, is_trap, elites = calculate_hft_score(raw_t, ticker)
                    raw_t = "[Echo] " + raw_t
                else:
                    score, is_trap, elites = calculate_hft_score(raw_t, ticker)
                
                if is_trap: has_trap = True
                all_elites.update(elites)
                if any("💎" in e for e in elites): raw_t = "[💎 核心情報] " + raw_t

                p_news = cell.get('HighVal', 0.0)
                articles.append({
                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": raw_t,
                    "link": item.find('link').text, 
                    "time": dt_tpe.strftime("%m-%d %H:%M"),
                    "pub_ts": dt_tpe.timestamp(), "is_today": (dt_tpe.date() == now_tpe.date()), 
                    "score": score, "elites": list(elites), "source": "Yahoo",
                    "p_news": p_news, "max_p_15m": p_news, "fetch_time_ts": time.time()
                })
            
            with brain_lock:
                combined = articles + [n for n in cell.get('NewsList', []) if n['raw_title'] not in {a['raw_title'] for a in articles}]
                combined.sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
                cell["NewsList"] = combined[:10]
                update_dynamic_catscore(cell)
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
    except Exception as e: 
        print(f"❌ [新聞錯誤] Yahoo 抓取異常: {e}")

def finnhub_news_monitor_worker():
    if not FINNHUB_TOKEN: return
    time.sleep(10)
    seen_news_urls = set()
    while True:
        try:
            with brain_lock: current_watchlist = DYNAMIC_WATCHLIST.copy()
            if not current_watchlist: time.sleep(10); continue
            now_ny = datetime.now(TZ_NY)
            end_date = now_ny.strftime('%Y-%m-%d')
            start_date = (now_ny - timedelta(days=3)).strftime('%Y-%m-%d')

            for ticker in current_watchlist:
                try:
                    url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={start_date}&to={end_date}&token={FINNHUB_TOKEN}"
                    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    if res.status_code == 200:
                        for art in res.json()[:5]:
                            art_url = art.get('url', '')
                            art_headline = art.get('headline', '')
                            if not art_url or not art_headline or art_url in seen_news_urls: continue
                            
                            dt_tpe = convert_to_taiwan_time(art.get('datetime', time.time()), source="finnhub")
                           
                            with brain_lock:
                                cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                                if is_news_echo(art_headline, dt_tpe, cell.get('NewsList', [])):
                                    score, is_trap, elites = calculate_hft_score(art_headline, ticker)
                                    art_headline = "[Echo] " + art_headline
                                else:
                                    score, is_trap, elites = calculate_hft_score(art_headline, ticker)

                                score = int(score * 1.2)
                                if any("💎" in e for e in elites): art_headline = "[💎 核心情報] " + art_headline

                                p_news = cell.get('HighVal', 0.0)
                                new_article = {
                                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": art_headline, "link": art_url,
                                    "time": dt_tpe.strftime("%m-%d %H:%M"),
                                    "pub_ts": dt_tpe.timestamp(),
                                    "is_today": (dt_tpe.date() == datetime.now(TZ_TW).date()),
                                    "score": score, "elites": list(elites), "source": "Finnhub PR",
                                    "p_news": p_news, "max_p_15m": p_news, "fetch_time_ts": time.time()
                                }

                                if not any(n['link'] == art_url for n in cell['NewsList']):
                                    cell['NewsList'].append(new_article)
                                    cell['NewsList'].sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
                                    cell['NewsList'] = cell['NewsList'][:10]
                                    update_dynamic_catscore(cell)
                                    cell["IsTrap"] = cell.get("IsTrap", False) or is_trap
                                    cell["HasNews"] = True
                                    MASTER_BRAIN["top_catalysts"] = extract_top_catalysts(MASTER_BRAIN)
                                    for e in elites:
                                        if e not in MASTER_BRAIN["elite_words"]: MASTER_BRAIN["elite_words"].append(e)
                                    threading.Thread(target=background_translate_worker, args=(ticker, art_headline, MASTER_BRAIN), daemon=True).start()
                                    time.sleep(0.5)
                            seen_news_urls.add(art_url) 
                            if len(seen_news_urls) > 1000: seen_news_urls.pop() 
                except Exception as e: 
                    pass
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

        price_col = "premarket_close" if is_premarket else "close"
        chg_col = "premarket_change" if is_premarket else "change"
        vol_col = "premarket_volume" if is_premarket else "volume"

        payload = {
            "filter": [
                {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]},
                {"left": "type", "operation": "in_range", "right": ["stock", "fund", "dr"]}, 
                # 🚀 V58.4: 價格區間放水至 $0.1 ~ $30.0，杜絕漏掉超低空核彈妖股 (如 SLXN)
                {"left": price_col, "operation": "in_range", "right": [0.1, 30]},
                {"left": chg_col, "operation": "egreater", "right": -50.0},
                {"left": vol_col, "operation": "egreater", "right": 500}
            ], 
            "columns": ["name", "close", "change", "volume", "premarket_close", "premarket_change", "premarket_volume", "market_cap_basic", "type", "average_volume_10d_calc", "relative_volume_10d_calc"], 
            "sort": {"sortBy": chg_col, "sortOrder": "desc"}, 
            "range": [0, 100] 
        }

        res = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        
        if res.status_code != 200:
            print(f"❌ [掃描器錯誤] TV Scanner 拒絕連線 (狀態碼: {res.status_code})")
            return
            
        data = res.json()
        if data.get('data'):
            for x in data['data']:
                sym, c, pct, v, pm_c, pm_pct, pm_v, mc, t, avg_vol_10d, native_rel_vol = x['d'][0], x['d'][1], x['d'][2], x['d'][3], x['d'][4], x['d'][5], x['d'][6], x['d'][7], x['d'][8], x['d'][9], x['d'][10]
                if sym not in new_watchlist: new_watchlist.append(sym)
                
                if native_rel_vol is None: native_rel_vol = 0.0
                if avg_vol_10d is None: avg_vol_10d = 0.0
                
                p_eff = pm_c if is_premarket and pm_c is not None else c
                actual_pct = pm_pct if is_premarket and pm_pct is not None else pct
                if p_eff is None: p_eff = c
                if actual_pct is None: actual_pct = 0.0

                total_vol_from_scanner = pm_v if is_premarket and pm_v is not None else v
                if total_vol_from_scanner is None: total_vol_from_scanner = 0

                temp_stats[sym] = {
                    'type': t, 
                    'avg_vol_10d': avg_vol_10d,
                    'native_rel_vol': native_rel_vol,
                    'total_vol': total_vol_from_scanner,
                    'live_price': p_eff,
                    'live_pct': actual_pct
                }
        else:
            print(f"⚠️ [掃描器警告] TV 回傳資料為空，目前可能無符合條件之標的。")
        
        with brain_lock:
            DYNAMIC_WATCHLIST.clear()
            DYNAMIC_WATCHLIST.extend(new_watchlist[:100])
            STATS_MAP.update(temp_stats)
    except Exception as e: 
        print(f"❌ [掃描器錯誤] 獲取 TV 名單失敗: {e}")

def scanner_engine():
    global _last_list_update, SPY_real_pct, _last_spy_update, MIN_RELVOL_LIMIT, TV_LOGIN_STATUS
    tv = TvDatafeed()
    TV_LOGIN_STATUS = "👤 訪客模式 (15分延遲)"
    
    if TW_USERNAME and TW_USERNAME != 'guest':
        try:
            tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
            TV_LOGIN_STATUS = "✅ 登入成功"
        except Exception as e:
            TV_LOGIN_STATUS = "⚠️ 帳密錯誤 (訪客模式)"

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

            if now_ts - _last_list_update > 60: 
                update_dynamic_watchlist()
                _last_list_update = now_ts
                  
            if not DYNAMIC_WATCHLIST: 
                time.sleep(5)
                continue
                
            current_watchlist = DYNAMIC_WATCHLIST.copy()

            def _process_ticker(ticker):
                nonlocal tv
                try:
                    df = None
                    try:
                        df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=500, extended_session=True)
                    except: pass
                    
                    stat_data = STATS_MAP.get(ticker, {'type': 'stock', 'avg_vol_10d': 0.0, 'native_rel_vol': 0.0, 'total_vol': 0, 'live_price': 0.0, 'live_pct': 0.0})
                    
                    tv_live_price = float(stat_data.get('live_price', 0.0))
                    tv_native_pct = float(stat_data.get('live_pct', 0.0))
                    tv_scanner_vol = int(stat_data.get('total_vol', 0))
                    tv_prev_est = tv_live_price / (1 + (tv_native_pct/100)) if tv_native_pct != -100 else tv_live_price

                    real_float, out_shares = get_shares_data(ticker)

                    # 🚀 V58.5 修正：不要直接從 WATCHLIST remove，改為標記為無效，避免執行緒崩潰
                    if real_float >= 20_000_000:
                        with brain_lock:
                            if ticker in MASTER_BRAIN["details"]: MASTER_BRAIN["details"][ticker]["IsInvalid"] = True
                        return
                    
                    is_etf = stat_data.get('type') == 'fund'
                    if is_etf:
                        float_str = out_str = "N/A (ETF)"; float_color = "gray-bg"
                    else:
                        float_str = format_shares_k_m(real_float); out_str = format_shares_k_m(out_shares)
                        float_color = "cyan-bg" if (0 < real_float < 1000000) else "gray-bg"

                    if df is None or df.empty: 
                        with brain_lock:
                            cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False, "StickySignal": "", "StickyColor": "green", "StickyTime": 0})
                            # ... (stats 初始化代碼與先前一致) ...
                            stats = {
                                "Code": ticker, "RelVol": f"{stat_data.get('native_rel_vol', 0.0)}x", "Vol": format_vol(tv_scanner_vol),
                                "Status": "gray", "Signal": "⏳ 待K線生成",
                                "HighVal": safe_float(tv_live_price), "StopLoss": safe_float(tv_live_price * 0.99), "Type": stat_data.get('type', 'stock'),
                                "Float": float_str, "Outstanding": out_str, "Float_Color": float_color,
                                "Real_Float_Shares": real_float, "Shares_Outstanding_Raw": out_shares,
                                "Price": f"${tv_live_price:.2f}", "PriceVal": safe_float(tv_live_price), "Pct": f"{tv_native_pct:+.2f}%"
                            }
                            cell.update(stats)
                        return

                    df = df.ffill().bfill().fillna(0)

                    time_diff = df.index.to_series().diff()
                    new_sessions = time_diff[time_diff > pd.Timedelta(hours=4)]
                    if not new_sessions.empty:
                        session_start = new_sessions.index[-1]
                        today_df = df.loc[session_start:].copy()
                    else:
                        last_date = df.index[-1].date()
                        today_df = df[df.index.date == last_date].copy()

                    p_live = float(today_df['close'].iloc[-1])
                    if p_live < 0.1 or p_live > 30.0: return # 允許 0.1 進入運算
                        
                    # 獲取目前的 CatScore 判定是否需要特赦
                    with brain_lock:
                        current_cat_score = MASTER_BRAIN["details"].get(ticker, {}).get("CatScore", 0)
                        
                    # 🚀 V58.4 智慧特赦邏輯：0.1 ~ 0.5 之間的股票，必須有 CatScore >= 10 才能發送日誌與進榜
                    is_under_radar = (p_live < 0.5 and current_cat_score < 10)
                        
                    v_live = float(today_df['volume'].iloc[-1])
                    v_prev = float(today_df['volume'].iloc[-2]) if len(today_df) > 1 else v_live
                    
                    collector.update_max_price(ticker, p_live)
                    
                    kbar_sum_vol = int(today_df['volume'].sum()) if not today_df.empty else int(v_live)
                    daily_vol_raw = max(tv_scanner_vol, kbar_sum_vol)

                    kbar_prev = float(today_df['low'].min()) if not today_df.empty else p_live
                    base_prev = tv_prev_est if tv_prev_est > 0 else kbar_prev
                    
                    if tv_native_pct < -20.0 and p_live > base_prev:
                        yf_prev = fetch_yfinance_prev_close(ticker)
                        if yf_prev > 0:
                            correct_prev = yf_prev
                            real_pct = ((p_live - correct_prev) / correct_prev) * 100
                        else:
                            correct_prev = base_prev
                            real_pct = ((p_live - correct_prev) / correct_prev) * 100 if correct_prev > 0 else 0.0
                    else:
                        correct_prev = base_prev
                        real_pct = ((p_live - correct_prev) / correct_prev) * 100 if correct_prev > 0 else 0.0

                    k_range = today_df['high'] - today_df['low']
                    k_range = k_range.replace(0, 1e-5) 
                    
                    buy_vol = np.where(today_df['close'] >= today_df['open'], 
                                       today_df['volume'] * ((today_df['close'] - today_df['low']) / k_range),
                                       today_df['volume'] * ((today_df['high'] - today_df['open']) / k_range))
                    
                    sell_vol = today_df['volume'] - buy_vol
                    delta = buy_vol - sell_vol
                    cvd_series = delta.cumsum()
                    
                    cvd_hma = calc_hma(cvd_series, 14)
                    cvd_sma20 = cvd_series.rolling(20).mean()
                    cvd_std20 = cvd_series.rolling(20).std()
                    cvd_upper = cvd_sma20 + (2 * cvd_std20)

                    time_lapsed_mins = len(today_df)
                    
                    calc_float = real_float if real_float > 0 else 999_000_000.0
                    turnover = (daily_vol_raw / calc_float) if calc_float > 0 else 0.0

                    avg_vol_10d = stat_data.get('avg_vol_10d', 0.0)
                    native_rel_vol = stat_data.get('native_rel_vol', 0.0)
                    
                    if avg_vol_10d > 0: historical_1m_avg = avg_vol_10d / 390.0
                    else: historical_1m_avg = float(today_df['volume'].iloc[:-1].mean()) if time_lapsed_mins > 2 else v_live
                    if historical_1m_avg <= 0: historical_1m_avg = 1.0 
                    
                    if native_rel_vol < 0.1 and real_pct > 15.0 and avg_vol_10d > 0:
                        expected_vol = (avg_vol_10d / 390.0) * max(1, time_lapsed_mins)
                        rel_vol_display = float(round(daily_vol_raw / expected_vol, 2)) if expected_vol > 0 else 1.0
                    else:
                        if native_rel_vol > 0: rel_vol_display = float(round(native_rel_vol, 2))
                        elif avg_vol_10d > 0: rel_vol_display = float(round(daily_vol_raw / avg_vol_10d, 2))
                        else: rel_vol_display = 1.0

                    if daily_vol_raw < 500: rel_vol_display = 0.0

                    if time_lapsed_mins > 0:
                        typical_price = (today_df['high'] + today_df['low'] + today_df['close']) / 3
                        cumulative_vol = today_df['volume'].cumsum()
                        vwap_series = (typical_price * today_df['volume']).cumsum() / cumulative_vol
                        current_vwap = float(vwap_series.iloc[-1])
                        vwap_dev = float(round(((p_live - current_vwap) / current_vwap) * 100, 2)) if current_vwap > 0 else 0.0
                    else: current_vwap = p_live; vwap_dev = 0.0

                    now_ny = datetime.now(TZ_NY)
                    is_premarket = now_ny.time() < datetime.strptime("09:30", "%H:%M").time()
                    if is_premarket: 
                        gap_pct = real_pct 
                    else:
                        try:
                            if today_df.index.tzinfo is None: ny_index = today_df.index.tz_localize('UTC').tz_convert('America/New_York')
                            else: ny_index = today_df.index.tz_convert('America/New_York')
                                
                            reg_df = today_df[ny_index.time >= datetime.strptime("09:30", "%H:%M").time()]
                            if not reg_df.empty:
                                reg_open = float(reg_df['open'].iloc[0])
                                gap_pct = float(((reg_open - correct_prev) / correct_prev) * 100) if correct_prev > 0 else 0.0
                            else: gap_pct = real_pct
                        except: gap_pct = real_pct

                    try:
                        if len(df) >= 3: p_2m_ago = float(df['close'].iloc[-3])
                        else: p_2m_ago = float(df['close'].iloc[0])
                        pct_2m = float(((p_live - p_2m_ago) / p_2m_ago) * 100) if p_2m_ago > 0 else 0.0
                    except: pct_2m = 0.0

                    try:
                        if not today_df.empty:
                            hod_val = float(today_df['high'].max())
                            hod_idx = today_df[today_df['high'] == hod_val].index[-1]
                            current_utc = datetime.now(pytz.UTC)
                            if hod_idx.tzinfo is None: hod_idx = pytz.UTC.localize(hod_idx)
                            minutes_since_hod = int((current_utc - hod_idx).total_seconds() / 60)
                            minutes_since_hod = max(0, minutes_since_hod)
                        else: minutes_since_hod = 0
                    except: minutes_since_hod = 0

                    curr_ema9 = float(df['close'].ewm(span=9, adjust=False).mean().iloc[-1]) if len(df) >= 9 else p_live
                    curr_ema20 = float(df['close'].ewm(span=20, adjust=False).mean().iloc[-1]) if len(df) >= 20 else p_live
                    curr_ema52 = float(df['close'].ewm(span=52, adjust=False).mean().iloc[-1]) if len(df) >= 52 else p_live
                    
                    ema9_dev = float(round(((p_live - curr_ema9) / curr_ema9) * 100, 2)) if curr_ema9 > 0 else 0.0
                    
                    vol_tier = 1 
                    if (v_live >= historical_1m_avg * 5.0 and v_live >= 5000): vol_tier = 3 
                    elif v_live >= historical_1m_avg * 2.0: vol_tier = 2 

                    ratio_5v5 = 1.0; vol_acc_str = "-" 
                    if len(today_df) >= 15:
                        vol_A = float(today_df['volume'].iloc[-5:].sum())
                        vol_B = float(today_df['volume'].iloc[-10:-5].sum())
                        vol_C = float(today_df['volume'].iloc[-15:-10].sum())
                        if vol_B > 0:
                            ratio_5v5 = float(vol_A / vol_B)
                            sym_up, sym_extreme = ("↗", "🔥") if p_live >= p_prev else ("🔻", "🩸")
                            if vol_A > vol_B > vol_C: vol_acc_str = f"{sym_extreme} {ratio_5v5:.2f}x"
                            elif vol_A > vol_B: vol_acc_str = f"{sym_up} {ratio_5v5:.2f}x"
                            elif vol_A < vol_B < vol_C: vol_acc_str = f"🧊 {ratio_5v5:.2f}x"
                            else: vol_acc_str = f"↘ {ratio_5v5:.2f}x"

                    float_comp = 100.0 / math.sqrt(calc_float/1e6) if calc_float > 0 else 0.0
                    base_sn_score = (float_comp + real_pct) * (ratio_5v5 if ratio_5v5 > 1.0 else 0.5)
                    sn_score = int(round(base_sn_score))
                    
                    is_spring_trap = False
                    is_peak_hook = False
                    is_golden_cross = False
                    is_death_cross = False
                    is_cvd_frontrun = False
                    is_iceberg_dist = False
                    
                    vol_sma5 = today_df['volume'].rolling(5).mean().iloc[-1] if len(today_df) >= 5 else 0
                    has_active_volume = v_live > vol_sma5 and vol_sma5 > 0

                    if len(today_df) >= 20 and has_active_volume:
                        curr_cvd = safe_float(cvd_series.iloc[-1])
                        prev_cvd = safe_float(cvd_series.iloc[-2])
                        prev2_cvd = safe_float(cvd_series.iloc[-3])
                        
                        curr_hma = safe_float(cvd_hma.iloc[-1])
                        prev_hma = safe_float(cvd_hma.iloc[-2])
                        
                        prev_upper = safe_float(cvd_upper.iloc[-2])
                        curr_delta = safe_float(delta.iloc[-1])
                        
                        if (p_live < current_vwap * 1.01) and (p_live > p_prev) and (curr_delta > 0) and vol_tier >= 3:
                            is_spring_trap = True
                            
                        if (prev_cvd > prev_upper) and (curr_cvd < prev_cvd):
                            is_peak_hook = True
                            
                        if (prev_cvd < prev_hma) and (curr_cvd > curr_hma):
                            is_golden_cross = True
                            
                        if (prev_cvd > prev_hma) and (curr_cvd < curr_hma):
                            is_death_cross = True
                            
                        if (curr_cvd > prev_cvd > prev2_cvd) and (v_live < historical_1m_avg):
                            ema_max = max(curr_ema9, curr_ema20)
                            ema_min = min(curr_ema9, curr_ema20)
                            is_ema_tight = (ema_max - ema_min) / ema_min < 0.02 if ema_min > 0 else False
                            if is_ema_tight: is_cvd_frontrun = True
                            
                        c_open = float(today_df['open'].iloc[-1]); c_close = float(today_df['close'].iloc[-1])
                        c_body = abs(c_open - c_close)
                        c_upper = float(today_df['high'].iloc[-1]) - max(c_open, c_close)
                        if (p_live > current_vwap) and (c_upper > c_body * 2) and (v_live > historical_1m_avg * 2) and (curr_delta < 0):
                            is_iceberg_dist = True

                    status_color = "green"
                    current_signal = ""
                    
                    if is_peak_hook:
                        current_signal = "💥 山峰極限反轉"
                        status_color = "red"
                    elif is_spring_trap:
                        current_signal = "⚡ 獵殺陷阱(假跌破拉回)"
                        status_color = "purple"
                    elif is_iceberg_dist:
                        current_signal = "⚠️ 主力倒貨(準備停利)"
                        status_color = "yellow"
                    elif is_cvd_frontrun:
                        current_signal = "🎯 狙擊陣型(量縮CVD潛伏)"
                        status_color = "yellow"
                    elif is_death_cross:
                        current_signal = "❌ 動能死叉"
                        status_color = "gray"
                    elif is_golden_cross:
                        current_signal = "● 金叉啟動"
                        status_color = "green"

                    delta_threshold = 0.03 if p_live < 5.0 else 0.015
                    last_alert_info = STATE_TRACKER.get(f"{ticker}_last_alert", {"price": p_live, "vol": daily_vol_raw})
                    price_change_ratio = (p_live - last_alert_info["price"]) / last_alert_info["price"] if last_alert_info["price"] > 0 else 0.0
                    
                    trigger_type = "none"
                    delta_pct_str = ""
                    if price_change_ratio >= delta_threshold:
                        trigger_type = "up"
                        delta_pct_str = f"+{(price_change_ratio*100):.1f}% ↗"
                        STATE_TRACKER[f"{ticker}_last_alert"] = {"price": p_live, "vol": daily_vol_raw}
                    elif price_change_ratio <= -delta_threshold:
                        trigger_type = "down"
                        delta_pct_str = f"{(price_change_ratio*100):.1f}% ↘"
                        STATE_TRACKER[f"{ticker}_last_alert"] = {"price": p_live, "vol": daily_vol_raw}

                    with brain_lock:
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False, "StickySignal": "", "StickyColor": "green", "StickyTime": 0})
                        
                        cell["TV_Live_Price"] = p_live
                        cell["Correct_Prev_Close"] = correct_prev
                        cell["PctVal"] = real_pct
                        cell["AmtVal"] = p_live - correct_prev
                        cell["TV_Scanner_Vol"] = tv_scanner_vol
                        cell["KBar_Cum_Vol"] = kbar_sum_vol
                        cell["Daily_Vol_Raw"] = daily_vol_raw
                        cell["TV_Native_RelVol"] = native_rel_vol
                        cell["Real_Float_Shares"] = real_float
                        cell["Shares_Outstanding_Raw"] = out_shares

                        if len(today_df) >= 20 and has_active_volume:
                            cell["CVD_Value"] = curr_cvd
                            cell["CVD_HMA"] = curr_hma

                        has_sentiment_flip = False
                        if cell.get("CatScore", 0) < 0 and p_live > current_vwap and curr_ema9 > curr_ema20:
                            cell["CatScore"] = 60
                            cell["IsTrap"] = False
                            has_sentiment_flip = True
                            if not current_signal: current_signal = "🚀"
                            current_signal += " [💎 利空不跌]"
                            status_color = "purple"
                        
                        if current_signal:
                            cell["StickySignal"] = current_signal
                            cell["StickyColor"] = status_color
                            cell["StickyTime"] = now_ts
                        else:
                            if now_ts - cell.get("StickyTime", 0) < 900: 
                                current_signal = cell.get("StickySignal", "")
                                status_color = cell.get("StickyColor", "green")

                        is_alpaca_active = cell.get("Alpaca_Active", False)
                        alpaca_cost_price = cell.get("Alpaca_Cost_Price", 0.0)

                        if is_alpaca_active and alpaca_cost_price > 0:
                            ui_price_str = f"${alpaca_cost_price:.2f} (持倉)"
                            ui_price_val = alpaca_cost_price
                        else:
                            ui_price_str = f"${p_live:.2f}"
                            ui_price_val = p_live

                        stats = {
                            "Code": ticker, "RelVol": f"{rel_vol_display}x", "Vol": format_vol(daily_vol_raw),
                            "Status": status_color, "Signal": current_signal,
                            "HighVal": safe_float(today_df['high'].max() if not today_df.empty else p_live), 
                            "StopLoss": safe_float(curr_ema20 * 0.99), 
                            "Type": stat_data.get('type', 'stock'),
                            "Float": float_str, "Outstanding": out_str, "Float_Color": float_color,
                            "VolAcc": vol_acc_str, "VolTier": vol_tier, "EMA9_Dev": safe_float(ema9_dev), 
                            "IsSniperTarget": False, "IsTrendingNews": cell.get("IsTrendingNews", False), 
                            "SN_Score": safe_float(sn_score), "VsaState": 0, "VR_Acc": 0.0, "VWAP_Dev": safe_float(vwap_dev), "VWAP_Rating": "Low",
                            "EMA9": safe_float(curr_ema9), "EMA52": safe_float(curr_ema52), "TriggerType": trigger_type, 
                            "Turnover": f"{turnover*100:.1f}%",
                            "DeltaStr": delta_pct_str, "TriggerTS": now_ts if trigger_type != "none" else cell.get("TriggerTS", 0),
                            "HasSentimentFlip": has_sentiment_flip, "GapPct": safe_float(gap_pct), "Pct2Min": safe_float(pct_2m), "HOD_Cooldown": minutes_since_hod,
                            "PrevClose": safe_float(correct_prev),
                            "Price": ui_price_str, "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p_live - correct_prev):+.2f}", "PriceVal": safe_float(ui_price_val)
                        }

                        cell.update(stats)
                        
                        if is_spring_trap or is_peak_hook or is_iceberg_dist or is_cvd_frontrun or is_golden_cross or is_death_cross:
                            last_trigger_time = cooldown_tracker.get(ticker, 0)
                            
                            # 🚀 V58.4: 低於 0.5 且沒新聞的股票，靜音不報警 (is_under_radar 攔截)
                            if now_ts - last_trigger_time > 60 and not is_under_radar:
                                cooldown_tracker[ticker] = now_ts 
                                
                                is_sec_trap = check_sec_fatal_traps(ticker)
                                if is_sec_trap and not has_sentiment_flip:
                                    stats["Signal"] += " 💀(SEC陷阱)"
                                    cell["IsTrap"] = True
                                    
                                alert_audio = "nova" if (is_spring_trap or is_peak_hook) else None

                                row_status = "flash-green" if (vol_tier == 3 or ratio_5v5 >= 2.0) else "normal"

                                log_entry = {
                                    **stats, 
                                    "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), 
                                    "SignalTS": now_ts,
                                    "Row_Status": row_status 
                                }
                                if alert_audio: log_entry["Audio"] = alert_audio
                                    
                                MASTER_BRAIN["surge_log"].insert(0, log_entry)
                                MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:1000]
                                
                    now_time = time.time()
                    if ticker not in news_cache or (now_time - news_cache.get(ticker, 0) >= 900):
                        threading.Thread(target=fetch_and_score_news, args=(ticker, cell, False), daemon=True).start()
                
                except Exception as e: return

            # 🚀 執行並行處理
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                executor.map(_process_ticker, current_watchlist)
                
            with brain_lock:
                all_items = list(MASTER_BRAIN["details"].values())
                # 🚀 V58.5 最終修正：排行榜改為顯示所有有效標的，不再過濾低價且未評分股
                active_items = [x for x in all_items if x.get("Code") in DYNAMIC_WATCHLIST and not x.get("IsInvalid", False)]
                MASTER_BRAIN["leaderboard"] = sorted(active_items, key=lambda x: float(str(x.get('Pct', '0')).replace('%', '')), reverse=True)[:100]
                MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
            
            time.sleep(1)
        except Exception as e: time.sleep(5)

def daily_flush_worker():
    global _current_session_date, MASTER_BRAIN, DYNAMIC_WATCHLIST, STATS_MAP, cooldown_tracker, STATE_TRACKER
    while True:
        time.sleep(10)
        new_session_date = get_current_trading_date()
        if new_session_date != _current_session_date:
            with brain_lock:
                MASTER_BRAIN["surge_log"] = []
                MASTER_BRAIN["details"] = {}
                MASTER_BRAIN["leaderboard"] = []
                DYNAMIC_WATCHLIST.clear()
                STATS_MAP.clear()
                cooldown_tracker.clear()
                STATE_TRACKER.clear()
                _current_session_date = new_session_date

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

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data():
    with brain_lock: 
        safe_brain = copy.deepcopy(MASTER_BRAIN)
        safe_brain["live_trends"] = get_live_trends()
        safe_brain["current_relvol"] = MIN_RELVOL_LIMIT 
        safe_brain["tv_status"] = TV_LOGIN_STATUS 
    return jsonify(safe_brain)

if __name__ == '__main__':
    collector.init_db() 
    load_shares_cache()
    load_intraday_state()
    memory_worker.init_worker() 
    threading.Thread(target=state_auto_save_worker, daemon=True).start()
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=finnhub_news_monitor_worker, daemon=True).start()
    threading.Thread(target=daily_flush_worker, daemon=True).start()
    init_alpaca(MASTER_BRAIN, DYNAMIC_WATCHLIST, brain_lock)
    
    app.run(host='0.0.0.0', port=PORT, use_reloader=False)