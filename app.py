from flask import Flask, jsonify, render_template, request, send_file
import io
import time, threading, os, json, re, base64, copy, math
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
import logging

logging.getLogger('tvDatafeed').setLevel(logging.CRITICAL)

brain_lock = threading.RLock() 
TRENDS_FILE_PATH = os.path.join(os.path.dirname(__file__), 'trends.json')
DISCOVERY_LOG_PATH = os.path.join(os.path.dirname(__file__), 'discovery_log.json') 
SESSION_BACKUP_PATH = os.path.join(os.path.dirname(__file__), 'session_state.json')

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

CATALYST_ARMORY = {}
armory_path = os.path.join(os.path.dirname(__file__), 'catalysts.json')
def reload_armory():
    global CATALYST_ARMORY
    try:
        with open(armory_path, 'r', encoding='utf-8') as f: 
            CATALYST_ARMORY = json.load(f)
    except: CATALYST_ARMORY = {}

reload_armory()

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
    new_words = set(re.findall(r'\b[A-Z]{4,}\b', new_title.upper()))
    if not new_words: return False
    for n in existing_news_list:
        try:
            old_dt_str = f"{new_dt_tpe.year}-{n['time']}"
            old_dt_tpe = datetime.strptime(old_dt_str, "%Y-%m-%d %H:%M")
            old_dt_tpe = TZ_TW.localize(old_dt_tpe)
            if abs((new_dt_tpe - old_dt_tpe).total_seconds()) <= 10800:
                old_words = set(re.findall(r'\b[A-Z]{4,}\b', n['raw_title'].upper()))
                if old_words:
                    overlap = len(new_words.intersection(old_words)) / len(new_words.union(old_words))
                    if overlap >= 0.6: return True
        except: pass
    return False

def calculate_hft_score(headline, ticker=""):
    text = (headline or "").upper()
    total_score = 0
    elite_hits = []

    reload_armory() # 每次動態讀取以確保套用最新修改

    # 從 JSON 抓取字典
    HINDSIGHT_TRAPS = list(CATALYST_ARMORY.get("HINDSIGHT_NOISE", {}).keys())
    TOXIC_OFFERINGS = list(CATALYST_ARMORY.get("TOXIC_OFFERINGS", {}).keys())
    CLASS_ACTION_SPAM = list(CATALYST_ARMORY.get("CLASS_ACTION_SPAM", {}).keys())
    MEGACAPS = list(CATALYST_ARMORY.get("SYMPATHY_MEGACAPS", {}).keys())
    EARNINGS_PREVIEW = list(CATALYST_ARMORY.get("EARNINGS_PREVIEW", {}).keys())
    MA_WORDS = CATALYST_ARMORY.get("MEGA_CATALYSTS", {})

    # 🚨 防禦鎖 4：主觀農場與馬後炮過濾
    if "?" in text or any(trap in text for trap in HINDSIGHT_TRAPS) or all(x in text for x in ["WHY", "IS"]) or all(x in text for x in ["WHY", "ARE"]):
        return 0, False, []

    # 🚨 防禦鎖 1：增資毒藥鎖
    if any(trap in text for trap in TOXIC_OFFERINGS):
        return -50, True, [] 

    # 🚨 防禦鎖 2：訴訟垃圾桶
    if any(trap in text for trap in CLASS_ACTION_SPAM):
        return 0, False, []

    # 🚨 防禦鎖 3：主體關聯度過濾
    if any(mc in text for mc in MEGACAPS):
        partnership_words = ["PARTNERSHIP", "COLLABORATION", "CONTRACT", "AGREEMENT", "JOINS", "INTEGRATES"]
        if not any(pw in text for pw in partnership_words):
            return 0, False, [] 

    # 🚨 法說會「預告」過濾器
    if any(trap in text for trap in EARNINGS_PREVIEW):
        return 0, False, []

    # 🚨 併購權重絕對優先
    for kw, score in MA_WORDS.items():
        if kw in text: total_score += score

    # 原有邏輯：黑名單與趨勢加權
    for kw, score in CATALYST_ARMORY.get("BLACK", {}).items():
        if kw in text: return -100, True, []
        
    for kw, data in get_live_trends().items():
        if kw in text:
            score = data.get("score", 5)
            total_score += score
            if score >= 10 or data.get("count", 0) >= 3: elite_hits.append(kw)

    for cat in ["RED", "ORANGE", "YELLOW"]:
        for kw, score in CATALYST_ARMORY.get(cat, {}).items():
            if kw in text: 
                total_score += score
                if score >= 8: elite_hits.append(kw)
                
    return total_score, False, elite_hits

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
            if found: pass
    except: pass

def check_sec_fatal_traps(ticker):
    headers = {"User-Agent": "SniperQuantSystem_V43 AdminContact@yourdomain.com", "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"}
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
                    score = 0
                    is_trap = False
                    elites = []
                    raw_t = "[Echo] " + raw_t
                else:
                    score, is_trap, elites = calculate_hft_score(raw_t, ticker)
                
                if is_trap: has_trap = True
                all_elites.update(elites)
                articles.append({
                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": raw_t,
                    "link": item.find('link').text, "time": dt_tpe.strftime("%m-%d %H:%M"),
                    "is_today": (dt_tpe.date() == now_tpe.date()), 
                    "score": score, "elites": list(elites), "source": "Yahoo"
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
                                    score = 0; is_trap = False; elites = []; art_headline = "[Echo] " + art_headline
                                else:
                                    score, is_trap, elites = calculate_hft_score(art_headline, ticker)

                                new_article = {
                                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": art_headline, "link": art_url,
                                    "time": dt_tpe.strftime("%m-%d %H:%M"), "is_today": (dt_tpe.date() == datetime.now(TZ_TW).date()),
                                    "score": score, "elites": list(elites), "source": "Finnhub PR"
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

        # 🚀 統一雷達：純動能與成交量驅動 (完全拔除跳空限制)
        payload = {
            "filter": [
                {"left": "type", "operation": "in_range", "right": ["stock", "fund"]}, 
                {"left": "close", "operation": "in_range", "right": [1, 50]}, 
                {"left": "change", "operation": "egreater", "right": 2.0}, # 當日漲幅 > 2% 提前抓取
                {"left": "volume", "operation": "egreater", "right": 50000} 
            ], 
            "columns": ["name", "premarket_close", "close", "change", "premarket_volume", "market_cap_basic", "type"], 
            "sort": {"sortBy": "change", "sortOrder": "desc"}, 
            "range": [0, 40]
        }

        res = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        data = res.json()
        if data.get('data'):
            for x in data['data']:
                sym, p, c, pct, v, mc, t = x['d'][0], x['d'][1], x['d'][2], x['d'][3], x['d'][4], x['d'][5], x['d'][6]
                if sym not in new_watchlist: new_watchlist.append(sym)
                
                p_eff = p if p is not None else c
                float_m = (mc / p_eff) / 1_000_000 if mc and p_eff else 0.0
                prev_est = p_eff / (1 + ((pct or 0)/100)) if pct and pct != -100 else p_eff
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

            now_ny = datetime.now(TZ_NY)
            m_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
            m_shock_end = now_ny.replace(hour=9, minute=35, second=0, microsecond=0)
            is_open_shock = m_open <= now_ny < m_shock_end

            current_watchlist = DYNAMIC_WATCHLIST.copy()

            for ticker in current_watchlist:
                try:
                    time.sleep(2.0)
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=960, extended_session=True)
                    
                    if df is None or df.empty or len(df) < 5: 
                        consecutive_errors += 1
                        if consecutive_errors > 8:
                            tv = TvDatafeed()
                            try:
                                if TW_USERNAME != 'guest': tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
                            except: pass
                            consecutive_errors = 0
                        continue
                    consecutive_errors = 0 

                    last_date = df.index[-1].date()
                    today_df = df[df.index.date == last_date]

                    p_live = float(df['close'].iloc[-1])
                    p_prev = float(df['close'].iloc[-2]) if len(df) > 1 else p_live
                    v_live = float(df['volume'].iloc[-1])
                    v_prev = float(df['volume'].iloc[-2]) if len(df) > 1 else v_live
                    
                    time_lapsed_mins = len(today_df)
                    if time_lapsed_mins > 2:
                        avg_vol = float(today_df['volume'].iloc[:-1].mean())
                    else:
                        avg_vol = v_live
                    if avg_vol == 0: avg_vol = 1.0 
                    
                    rel_vol_live = float(round(v_live / avg_vol, 2))
                    rel_vol_prev = float(round(v_prev / avg_vol, 2))
                    rel_vol_display = max(rel_vol_live, rel_vol_prev)
                    daily_vol = int(today_df['volume'].sum())
                    
                    vol_3m = float(df['volume'].iloc[-3:].sum()) if len(df) >= 3 else v_live

                    if time_lapsed_mins > 0:
                        typical_price = (today_df['high'] + today_df['low'] + today_df['close']) / 3
                        cumulative_vol = today_df['volume'].cumsum()
                        cumulative_vp = (typical_price * today_df['volume']).cumsum()
                        vwap_series = cumulative_vp / cumulative_vol
                        current_vwap = float(vwap_series.iloc[-1])
                        vwap_dev = float(round(((p_live - current_vwap) / current_vwap) * 100, 2)) if current_vwap > 0 else 0.0
                    else:
                        current_vwap = p_live
                        vwap_dev = 0.0

                    vwap_rating = "High" if vwap_dev > 15.0 else ("Mid" if vwap_dev > 5.0 else "Low") 

                    df['tr'] = pd.concat([(df['high'] - df['low']), (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
                    atr5 = float(df['tr'].rolling(5, min_periods=3).mean().iloc[-1]) if len(df) >= 5 else 0.0
                    atr20 = float(df['tr'].rolling(20, min_periods=5).mean().iloc[-1]) if len(df) >= 5 else 0.0

                    stat_data = STATS_MAP.get(ticker, {'prev': p_live, 'float_str': '-', 'type': 'stock', 'float_comp': 0.0})
                    prev_close = float(stat_data['prev']) if stat_data['prev'] > 0 else float(df['low'].min())
                    float_str = stat_data['float_str'] if stat_data['prev'] > 0 else "未知"
                    real_pct = float(((p_live - prev_close) / prev_close) * 100)
                    
                    curr_ema10 = float(df['close'].ewm(span=10, adjust=False).mean().iloc[-1]) if len(df) >= 10 else p_live
                    curr_ema20 = float(df['close'].ewm(span=20, adjust=False).mean().iloc[-1]) if len(df) >= 20 else p_live
                    
                    past_high_for_live = float(df['high'].iloc[-11:-1].max()) if len(df) >= 11 else p_live
                    past_high_for_prev = float(df['high'].iloc[-12:-2].max()) if len(df) >= 12 else p_live
                    spark_live = bool((rel_vol_live >= 2.5) and (p_live >= past_high_for_live))
                    spark_prev = bool((rel_vol_prev >= 2.5) and (p_prev >= past_high_for_prev) and (p_live >= df['open'].iloc[-2] if len(df)>1 else True))
                    is_spark = bool((spark_live or spark_prev) and (real_pct > 3.0))

                    is_vcp_compression = bool((atr5 > 0) and (atr5 < atr20 * 0.95) and (rel_vol_prev < 0.85) and (curr_ema10 > curr_ema20))
                    is_ride = bool((p_live >= curr_ema20) and (abs(p_live - curr_ema20)/curr_ema20 < 0.012) and (rel_vol_prev < 0.8) and (real_pct > 1.0) and not is_spark)
                    is_grind = bool((curr_ema10 > curr_ema20) and (p_live >= curr_ema10) and (0.5 <= rel_vol_display < 2.5) and (real_pct > 2.0) and not is_spark and not is_ride)

                    ratio_3v3 = 1.0; z_score = 0.0; staircase = False; vol_acc_str = "-"
                    vr_acc = 0.0 
                    
                    if len(df) >= 6:
                        avg_v_60 = float(df['volume'].iloc[:-1].mean())
                        std_v_60 = float(df['volume'].iloc[:-1].std())
                        z_score = float((v_live - avg_v_60) / std_v_60) if std_v_60 > 0 else 0.0
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
                        
                        b1 = vol_C
                        staircase = bool((b1 < vol_B < vol_A) and (p_live > df['close'].iloc[-10]))

                    if is_open_shock: z_score = 0.0; ratio_3v3 = 1.0; staircase = False; vol_acc_str = "-"

                    base_score = stat_data.get('float_comp', 0) + real_pct
                    r_er = real_pct / rel_vol_display if rel_vol_display > 0 else real_pct
                    
                    vsa_state = 0
                    if rel_vol_display > 5 and r_er < 1.0: m_vsa = 0.2; vsa_state = 2
                    elif rel_vol_display > 3 and r_er < 1.0: m_vsa = 0.5; vsa_state = 1
                    else: m_vsa = 1.0 + (math.log(max(rel_vol_display, 1.0)) * 0.1)

                    a_m = ratio_3v3 if ratio_3v3 > 1.0 else ratio_3v3 * 0.5
                    sn_score = int(round(base_score * m_vsa * a_m))

                    tracker = STATE_TRACKER.get(ticker, {'state': 'None', 'duration': 0, 'vcp_low': float('inf'), 'shakeout_high': 0.0})
                    dynamic_stop = curr_ema20 * 0.99 
                    
                    if is_spark:
                        if tracker['state'] == 'VCP' and tracker['duration'] >= 3: dynamic_stop = tracker['vcp_low']
                        tracker['state'] = 'None'; tracker['duration'] = 0; tracker['vcp_low'] = float('inf')
                    elif is_vcp_compression:
                        if tracker['state'] != 'VCP': tracker['state'] = 'VCP'; tracker['duration'] = 1; tracker['vcp_low'] = float(min(df['low'].iloc[-1], df['low'].iloc[-2] if len(df)>1 else df['low'].iloc[-1]))
                        else: tracker['duration'] += 1; tracker['vcp_low'] = float(min(tracker['vcp_low'], df['low'].iloc[-1]))
                    else: tracker['state'] = 'None'; tracker['duration'] = 0; tracker['vcp_low'] = float('inf')

                    if dynamic_stop == float('inf'): dynamic_stop = curr_ema20 * 0.99
                    dynamic_stop = float(dynamic_stop)

                    vol_warn = "(⚠️量低)" if (p_live * max(v_live, v_prev, avg_vol)) < 50000 else ""
                    current_signal = None; current_level = 0; status_color = "green"; is_shakeout = False
                    
                    if (z_score > 2.5 or ratio_3v3 > 2.5) and p_live < p_prev:
                        is_shakeout = True; tracker['state'] = 'Shakeout'; tracker['shakeout_high'] = float(df['high'].iloc[-1])
                        current_signal = "💀 洗盤觀察中"; status_color = "border"; current_level = 1
                    elif tracker['state'] == 'Shakeout' and p_live > tracker['shakeout_high']:
                        current_signal = "🔥 絕地反擊(V轉)"; status_color = "yellow"; current_level = 5; tracker['state'] = 'None'
                    elif z_score > 3.0 or (ratio_3v3 > 2.0 and staircase):
                        current_signal = "🔥 極端加速"; status_color = "yellow"; current_level = 4
                    elif is_spark: current_signal = f"🔥強力點火 {vol_warn}"; current_level = 4; status_color = "yellow"
                    elif tracker['state'] == 'VCP' and tracker['duration'] >= 3: current_signal = f"⚡VCP壓縮鎖定 ({tracker['duration']}m) {vol_warn}"; current_level = 3; status_color = "vcp" 
                    elif is_grind: current_signal = f"🚜穩步推升 {vol_warn}"; current_level = 2; status_color = "blue"  
                    elif is_ride: current_signal = f"💎趨勢滑行 {vol_warn}"; current_level = 1; status_color = "purple"

                    STATE_TRACKER[ticker] = tracker

                    stats = {
                        "Code": ticker, "Price": f"${p_live:.2f}", "RelVol": f"{rel_vol_display}x", "Vol": format_vol(daily_vol),
                        "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p_live-prev_close):+.2f}", "Status": status_color, 
                        "Signal": current_signal if current_signal else "",
                        "PriceVal": float(p_live), "StopLoss": dynamic_stop, "Float": float_str if float_str != "未知" else "-", "Type": stat_data.get('type', 'stock'),
                        "VolAcc": vol_acc_str, "Is100K": False, 
                        "SN_Score": sn_score, "VsaState": vsa_state, "VR_Acc": vr_acc,             
                        "VWAP_Dev": vwap_dev, "VWAP_Rating": vwap_rating  
                    }
                    
                    last_record = cooldown_tracker.get(ticker, {'time': 0, 'level': 0})
                    push_signal = False; is_double_lock = False
                    
                    if current_signal:
                        if (now_ts - last_record['time']) > 45 or current_level > last_record['level']: push_signal = True 
                        
                    is_trap = False
                    if push_signal and not is_shakeout:
                        is_trap = bool(check_sec_fatal_traps(ticker))
                        if is_trap: current_signal += " 💀(SEC陷阱)"; stats["Signal"] = current_signal

                    with brain_lock:
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False, "Price": "-", "Pct": "-", "Vol": "-", "RelVol": "-", "Float": "-", "Signal": "", "SN_Score": 0, "VsaState": 0})
                        cell.update(stats)
                        if is_trap: cell["IsTrap"] = True
                        
                        if push_signal:
                            cooldown_tracker[ticker] = {'time': now_ts, 'level': current_level}
                            audio_target = None
                            
                            is_relvol_alert = bool(rel_vol_display >= MIN_RELVOL_LIMIT and p_live >= p_prev)
                            
                            if is_relvol_alert and not is_shakeout:
                                audio_target = "spark" 
                                is_double_lock = True
                                stats["Signal"] = f"🎯點火: {stats['Signal']}" if stats["Signal"] else f"🎯點火: 量比突破 {rel_vol_display}x"

                            if not is_shakeout:
                                MASTER_BRAIN["surge_log"].insert(0, {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": audio_target})
                                MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:1000]

                        all_items = list(MASTER_BRAIN["details"].values())
                        def get_pct(item):
                            try: return float(item.get('Pct', '0').replace('%', '').replace('+', ''))
                            except: return -9999.0
                        
                        active_items = [x for x in all_items if x.get("Code") in DYNAMIC_WATCHLIST]
                        if not active_items: active_items = all_items 
                        
                        MASTER_BRAIN["leaderboard"] = sorted(active_items, key=get_pct, reverse=True)[:20]
                        
                        vwap_items = [x for x in active_items if x.get("VR_Acc", 0) > 30.0 and x.get("VWAP_Dev", 0) > 0.0]
                        MASTER_BRAIN["vwap_list"] = sorted(vwap_items, key=lambda x: x.get("VWAP_Dev", 0), reverse=True)
                        
                        MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
                    
                    threading.Thread(target=fetch_and_score_news, args=(ticker, cell, is_double_lock), daemon=True).start()
                except Exception as e: continue
            time.sleep(5)
        except Exception as e: time.sleep(10)

def auto_trend_updater():
    pass

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
            price = ticker_data.get('Price', '-')
            pct = ticker_data.get('Pct', '-')
            rel_vol = ticker_data.get('RelVol', '-')
            
            for n in ticker_data.get('NewsList', []):
                is_echo = "[Echo]" in n.get('raw_title', '')
                rows.append({
                    "發布時間(發布當下)": n.get('time'),
                    "代碼": ticker,
                    "該股總分(CatScore)": ticker_data.get('CatScore', 0),
                    "單則評分": n.get('score'),
                    "標題(英文)": n.get('raw_title'),
                    "標題(中文)": n.get('title'),
                    "是否為重複回音": "是" if is_echo else "否",
                    "當下價格": price,
                    "當下漲幅": pct,
                    "當下量比": rel_vol,
                    "新聞來源": n.get('source'),
                    "原始連結": n.get('link')
                })
    
    if not rows: return "目前尚無新聞數據可供匯出", 404
        
    df = pd.DataFrame(rows)
    output = io.BytesIO()
    df.to_csv(output, index=False, encoding='utf-8-sig')
    output.seek(0)
    
    filename = f"Sniper_News_Analysis_{datetime.now(TZ_TW).strftime('%m%d_%H%M')}.csv"
    
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name=filename)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data():
    with brain_lock: 
        safe_brain = copy.deepcopy(MASTER_BRAIN)
        safe_brain["live_trends"] = get_live_trends()
        safe_brain["current_relvol"] = MIN_RELVOL_LIMIT 
        try: safe_brain["trends_date"] = datetime.fromtimestamp(os.path.getmtime(TRENDS_FILE_PATH), TZ_TW).strftime("%Y-%m-%d %H:%M:%S")
        except: safe_brain["trends_date"] = "尚未生成"
    return jsonify(safe_brain)

if __name__ == '__main__':
    load_intraday_state()
    if not os.path.exists(DISCOVERY_LOG_PATH) or os.path.getsize(DISCOVERY_LOG_PATH) == 0:
        with open(DISCOVERY_LOG_PATH, 'w') as f: json.dump([], f)
        
    threading.Thread(target=state_auto_save_worker, daemon=True).start()
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=finnhub_news_monitor_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)