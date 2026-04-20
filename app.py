import time, threading, os, json, re, base64, copy
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import pytz
from tvDatafeed import TvDatafeed, Interval
from flask import Flask, jsonify, render_template, request # 💡 新增 request
from deep_translator import GoogleTranslator
from collections import Counter
import logging

logging.getLogger('tvDatafeed').setLevel(logging.CRITICAL)

brain_lock = threading.RLock() 
TRENDS_FILE_PATH = os.path.join(os.path.dirname(__file__), 'trends.json')
DISCOVERY_LOG_PATH = os.path.join(os.path.dirname(__file__), 'discovery_log.json') 

_cached_trends = {}
_last_trends_update = 0
TRENDS_CACHE_TTL = 60

_discovery_buffer = []
_last_flush_time = time.time()

# 💡 雷達掃描計時器改為全域變數，以便手動強制重置
_last_list_update = 0

TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 8080))
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

# 💡 V42.9 將預設跳空幅度調整為 5%
MIN_GAP_PCT = 5.0 

CATALYST_ARMORY = {}
armory_path = os.path.join(os.path.dirname(__file__), 'catalysts.json')
try:
    with open(armory_path, 'r', encoding='utf-8') as f: CATALYST_ARMORY = json.load(f)
except: CATALYST_ARMORY = {"INVERTED_TRAPS":{}, "RED":{}, "ORANGE":{}, "YELLOW":{}, "BLACK":{}}

MASTER_BRAIN = {"surge_log": [], "details": {}, "leaderboard": [], "top_catalysts": [], "last_update": "", "elite_words": []}
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

def flush_discovery_buffer():
    global _discovery_buffer
    with brain_lock:
        if not _discovery_buffer: return
        try:
            logs = []
            if os.path.exists(DISCOVERY_LOG_PATH):
                try:
                    with open(DISCOVERY_LOG_PATH, 'r', encoding='utf-8') as f: logs = json.load(f)
                except: pass 
            
            logs.extend(_discovery_buffer)
            with open(DISCOVERY_LOG_PATH, 'w', encoding='utf-8') as f: json.dump(logs, f, ensure_ascii=False, indent=4)
            _discovery_buffer = []
        except Exception as e: print(f"🚨 [NLP引擎] 寫入日誌失敗: {e}")

def settle_discovery_logs():
    try:
        if not os.path.exists(DISCOVERY_LOG_PATH): return
        with open(DISCOVERY_LOG_PATH, 'r', encoding='utf-8') as f: logs = json.load(f)
        
        unsettled = [l for l in logs if not l.get("Settled", True)]
        if not unsettled: return
        
        print("🔄 [NLP引擎] 啟動戰果回補程序，結算昨日遺失數據...")
        trends = get_live_trends()
        updated_trends = False
        
        for log in unsettled:
            word = log["Word"]
            if word in trends:
                old_impact = trends[word].get("avg_impact_pct", 0.0)
                trends[word]["avg_impact_pct"] = round(old_impact * 1.05, 2) 
                updated_trends = True
            log["Settled"] = True
            
        with open(DISCOVERY_LOG_PATH, 'w', encoding='utf-8') as f: json.dump(logs, f, ensure_ascii=False, indent=4)
        if updated_trends:
            with open(TRENDS_FILE_PATH, 'w', encoding='utf-8') as f: json.dump(trends, f, ensure_ascii=False, indent=4)
        print("✅ [NLP引擎] 戰果回補完成！")
    except Exception as e: print(f"🚨 [NLP引擎] 戰果回補異常: {e}")

def calculate_hft_score(headline):
    text = (headline or "").upper()
    total_score = 0
    elite_hits = []
    
    for kw, score in CATALYST_ARMORY.get("INVERTED_TRAPS", {}).items():
        if kw in text: total_score += score; text = text.replace(kw, "") 
    for kw, score in CATALYST_ARMORY.get("BLACK", {}).items():
        if kw in text: return -50, True, []
        
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

def background_translate_worker(ticker, en_headline, master_brain):
    try:
        zh_text = GoogleTranslator(source='auto', target='zh-TW').translate(en_headline)
        with brain_lock:
            if ticker in master_brain['details']:
                for article in master_brain['details'][ticker].get('NewsList', []):
                    if article.get('raw_title') == en_headline: article['title'] = zh_text; break 
    except: pass

def check_sec_fatal_traps(ticker):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []; total_score = 0; has_black = False; all_elites = set()
            now_ny = datetime.now(TZ_NY); three_days_ago = now_ny.date() - timedelta(days=3)
            
            for item in root.findall('./channel/item')[:5]:
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_NY)
                if dt.date() < three_days_ago: continue
                raw_t = item.find('title').text
                score, is_trap, elites = calculate_hft_score(raw_t)
                if is_trap: has_black = True
                total_score += score 
                all_elites.update(elites)
                articles.append({
                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": raw_t,
                    "link": item.find('link').text, "time": dt.strftime("%Y-%m-%d %H:%M"), 
                    "is_today": (dt.date() == now_ny.date()), "score": score, "elites": list(elites)
                })
            
            with brain_lock:
                cell["NewsList"] = articles; cell["CatScore"] = total_score
                cell["IsTrap"] = has_black; cell["HasNews"] = len(articles) > 0 
                MASTER_BRAIN["top_catalysts"] = extract_top_catalysts(MASTER_BRAIN)
                for e in all_elites:
                    if e not in MASTER_BRAIN["elite_words"]: MASTER_BRAIN["elite_words"].append(e)

            if articles:
                for art in articles:
                    if art['title'] == "⏳ 翻譯中...":
                        threading.Thread(target=background_translate_worker, args=(ticker, art['raw_title'], MASTER_BRAIN), daemon=True).start(); time.sleep(0.5) 
    except: pass

def extract_top_catalysts(master_brain):
    top_list = []
    for ticker, data in master_brain.get('details', {}).items():
        if data.get('NewsList', []): top_list.append(data)
    try: return sorted(top_list, key=lambda x: (x.get('CatScore', 0), x['NewsList'][0].get('time', '00:00')), reverse=True)
    except: return top_list

def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP, MIN_GAP_PCT
    try:
        url = "https://scanner.tradingview.com/america/scan"
        # 💡 使用全域的 MIN_GAP_PCT 變數進行動態掃描
        payload = {
            "filter": [
                {"left": "type", "operation": "in_range", "right": ["stock", "fund"]}, 
                {"left": "close", "operation": "in_range", "right": [1, 50]}, 
                {"left": "premarket_change", "operation": "egreater", "right": MIN_GAP_PCT} 
            ], 
            "columns": ["name", "premarket_close", "close", "premarket_change", "premarket_volume", "market_cap_basic", "type"], 
            "sort": {"sortBy": "premarket_change", "sortOrder": "desc"}, 
            "range": [0, 40]
        }
        res = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = res.json()
        if data.get('data'):
            DYNAMIC_WATCHLIST = [x['d'][0] for x in data['data']]
            for x in data['data']:
                sym, p, c, pct, v, mc, t = x['d'][0], x['d'][1], x['d'][2], x['d'][3], x['d'][4], x['d'][5], x['d'][6]
                p_eff = p if p is not None else c
                float_m = (mc / p_eff) / 1_000_000 if mc and p_eff else 0.0
                prev_est = p_eff / (1 + (pct/100)) if pct and pct != -100 else p_eff
                f_str = "N/A (ETF)" if t == 'fund' else (f"{float_m:.1f}M" if float_m > 0 else "未知")
                if t != 'fund' and float_m > 50.0: f_str = f"⚠️{f_str}"
                
                STATS_MAP[sym] = {'prev': prev_est, 'float_str': f_str, 'type': t, 'gap_pct': float(pct) if pct is not None else 0.0}
    except: pass

def auto_trend_updater():
    global _discovery_buffer
    STOP_WORDS = {"THE", "TO", "OF", "IN", "FOR", "A", "AND", "IS", "ON", "WITH", "BY", "AS", "AT", "FROM", "IT", "THAT", "THIS", "AN", "BE", "NEW", "UP", "OUT", "ITS", "ARE", "HAS"}
    time.sleep(60)
    while True:
        try:
            with brain_lock: target_tickers = DYNAMIC_WATCHLIST.copy()
            if not target_tickers: time.sleep(10); continue

            pcts = [float(MASTER_BRAIN['details'][t].get('Pct','0').replace('%','').replace('+','')) for t in target_tickers if t in MASTER_BRAIN['details'] and MASTER_BRAIN['details'][t].get('Pct') != '-']
            current_avg_pct = sum(pcts)/len(pcts) if pcts else 0

            all_words = []
            word_sources = {}
            for ticker in target_tickers:
                pct_val = 0.0
                if ticker in MASTER_BRAIN['details']:
                    try: pct_val = float(MASTER_BRAIN['details'][ticker].get('Pct','0').replace('%','').replace('+',''))
                    except: pass
                
                try:
                    res = requests.get(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US", headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    if res.status_code == 200:
                        for item in ET.fromstring(res.text).findall('./channel/item')[:3]:
                            words = re.findall(r'\b[A-Z]{3,}\b', item.find('title').text.upper()) 
                            for w in words:
                                if w not in STOP_WORDS and w != ticker: 
                                    all_words.append(w)
                                    if w not in word_sources: word_sources[w] = []
                                    word_sources[w].append((ticker, pct_val))
                except: continue
                time.sleep(0.5) 

            if all_words:
                top_trends = Counter(all_words).most_common(15)
                current_content = get_live_trends()
                added_words = []; requires_github_sync = False
                
                for w, count in top_trends:
                    if count >= 2:
                        max_pct = float(max([p for t, p in word_sources.get(w, [])])) if w in word_sources and word_sources[w] else 0.0
                        
                        if w not in current_content:
                            impact_score = min(5 + (max_pct / 10.0), 20)
                            current_content[w] = {"score": round(impact_score), "count": 1, "avg_impact_pct": max_pct, "last_seen": datetime.now(TZ_NY).strftime("%Y-%m-%d")}
                            added_words.append(w); requires_github_sync = True
                            
                            best_ticker = max(word_sources[w], key=lambda x: x[1])[0] if w in word_sources else "UNKNOWN"
                            _discovery_buffer.append({"Ticker": best_ticker, "Time": datetime.now(TZ_NY).strftime("%Y-%m-%d %H:%M:%S"), "Word": w, "Entry_Pct": max_pct, "Settled": False})
                        else:
                            old_data = current_content[w]
                            old_score = old_data.get("score", 5)
                            old_data["count"] = old_data.get("count", 1) + 1
                            old_data["last_seen"] = datetime.now(TZ_NY).strftime("%Y-%m-%d")
                            old_data["avg_impact_pct"] = max(old_data.get("avg_impact_pct", 0.0), max_pct)
                            
                            if old_data["count"] >= 3 and old_score < 15: 
                                old_data["score"] += 1; requires_github_sync = True
                            current_content[w] = old_data
                            added_words.append(w)

                if added_words:
                    with open(TRENDS_FILE_PATH, 'w', encoding='utf-8') as f: json.dump(current_content, f, ensure_ascii=False, indent=4)
                    global _cached_trends, _last_trends_update
                    _cached_trends = current_content; _last_trends_update = time.time()
                    
                    if requires_github_sync:
                        github_token = os.getenv('GITHUB_TOKEN'); github_repo = os.getenv('GITHUB_REPO')
                        if github_token and github_repo:
                            url = f"https://api.github.com/repos/{github_repo}/contents/trends.json"
                            headers = {"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github.v3+json"}
                            res = requests.get(url, headers=headers)
                            sha = res.json().get('sha') if res.status_code == 200 else None
                            payload = {"message": "🤖 AI V42: 戰果權重升級 [skip ci]", "content": base64.b64encode(json.dumps(current_content, indent=4).encode('utf-8')).decode('utf-8')}
                            if sha: payload["sha"] = sha
                            requests.put(url, headers=headers, json=payload)
            
            if not os.path.exists(DISCOVERY_LOG_PATH) or os.path.getsize(DISCOVERY_LOG_PATH) == 0:
                with open(DISCOVERY_LOG_PATH, 'w') as f:
                    json.dump([], f)
        except: pass
        time.sleep(86400) 

def scanner_engine():
    global _last_list_update
    tv = TvDatafeed()
    try:
        if TW_USERNAME != 'guest': tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
    except: pass

    consecutive_errors = 0 
    while True:
        try:
            now_ts = time.time()
            if now_ts - _last_list_update > 300: 
                update_dynamic_watchlist()
                _last_list_update = now_ts
                
            if not DYNAMIC_WATCHLIST: time.sleep(5); continue

            now_ny = datetime.now(TZ_NY)
            m_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
            m_shock_end = now_ny.replace(hour=9, minute=35, second=0, microsecond=0)
            is_open_shock = m_open <= now_ny < m_shock_end

            for ticker in DYNAMIC_WATCHLIST:
                try:
                    time.sleep(2.0)
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
                    
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

                    p_live = float(df['close'].iloc[-1]); p_prev = float(df['close'].iloc[-2]) if len(df) > 1 else p_live
                    v_live = float(df['volume'].iloc[-1]); v_prev = float(df['volume'].iloc[-2]) if len(df) > 1 else v_live
                    lookback = min(12, len(df)); avg_vol = float(df['volume'].iloc[-lookback:-2].mean()) if lookback > 2 else float(df['volume'].iloc[-2]) if len(df) > 1 else v_live
                    
                    rel_vol_live = float(round(v_live / avg_vol, 2)) if avg_vol > 0 else 1.0
                    rel_vol_prev = float(round(v_prev / avg_vol, 2)) if avg_vol > 0 else 1.0
                    rel_vol_display = max(rel_vol_live, rel_vol_prev); daily_vol = int(df['volume'].sum())
                    
                    vol_3m = float(df['volume'].iloc[-3:].sum()) if len(df) >= 3 else v_live
                    is_100k = bool(vol_3m >= 100000)

                    df['tr'] = pd.concat([(df['high'] - df['low']), (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
                    atr5 = float(df['tr'].rolling(5, min_periods=3).mean().iloc[-1]) if len(df) >= 5 else 0.0
                    atr20 = float(df['tr'].rolling(20, min_periods=5).mean().iloc[-1]) if len(df) >= 5 else 0.0

                    stat_data = STATS_MAP.get(ticker, {'prev': p_live, 'float_str': '-', 'type': 'stock', 'gap_pct': 0.0})
                    prev_close = float(stat_data['prev']) if stat_data['prev'] > 0 else float(df['low'].min())
                    float_str = stat_data['float_str'] if stat_data['prev'] > 0 else "未知"
                    real_pct = float(((p_live - prev_close) / prev_close) * 100)
                    gap_pct = float(stat_data['gap_pct']) 
                    
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
                    
                    if len(df) >= 6:
                        avg_v_60 = float(df['volume'].iloc[:-1].mean())
                        std_v_60 = float(df['volume'].iloc[:-1].std())
                        z_score = float((v_live - avg_v_60) / std_v_60) if std_v_60 > 0 else 0.0
                        
                        vol_A = vol_3m
                        vol_B = float(df['volume'].iloc[-6:-3].sum())
                        vol_C = float(df['volume'].iloc[-9:-6].sum()) if len(df) >= 9 else 0.0
                        
                        if vol_B > 0:
                            ratio_3v3 = float(vol_A / vol_B)
                            if len(df) >= 9:
                                if vol_A > vol_B > vol_C: vol_acc_str = f"🔥 {ratio_3v3:.2f}x"
                                elif vol_A > vol_B and vol_B <= vol_C: vol_acc_str = f"↗ {ratio_3v3:.2f}x"
                                elif vol_A < vol_B < vol_C: vol_acc_str = f"🧊 {ratio_3v3:.2f}x"
                                elif vol_A < vol_B and vol_B >= vol_C: vol_acc_str = f"↘ {ratio_3v3:.2f}x"
                                else: vol_acc_str = f"- {ratio_3v3:.2f}x"
                            else:
                                vol_acc_str = f"↗ {ratio_3v3:.2f}x" if vol_A > vol_B else f"↘ {ratio_3v3:.2f}x"
                        
                        b1 = vol_C
                        staircase = bool((b1 < vol_B < vol_A) and (p_live > df['close'].iloc[-10]))

                    if is_open_shock: z_score = 0.0; ratio_3v3 = 1.0; staircase = False; vol_acc_str = "-"

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
                    elif tracker['state'] == 'VCP' and tracker['duration'] >= 3: current_signal = f"⚡VCP壓縮鎖定 {vol_warn}"; current_level = 3; status_color = "vcp" 
                    elif is_grind: current_signal = f"🚜穩步推升 {vol_warn}"; current_level = 2; status_color = "blue"  
                    elif is_ride: current_signal = f"💎趨勢滑行 {vol_warn}"; current_level = 1; status_color = "purple"

                    STATE_TRACKER[ticker] = tracker

                    stats = {
                        "Code": ticker, "Price": f"${p_live:.2f}", "RelVol": f"{rel_vol_display}x", "Vol": format_vol(daily_vol),
                        "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p_live-prev_close):+.2f}", "Status": status_color, 
                        "Signal": current_signal if current_signal else "",
                        "PriceVal": float(p_live), "StopLoss": dynamic_stop, "Float": float_str, "Type": stat_data.get('type', 'stock'),
                        "VolAcc": vol_acc_str, 
                        "Is100K": is_100k,
                        "Gap": f"{gap_pct:+.2f}%" 
                    }
                    
                    last_record = cooldown_tracker.get(ticker, {'time': 0, 'level': 0})
                    push_signal = False
                    if current_signal:
                        if (now_ts - last_record['time']) > 45 or current_level > last_record['level']: push_signal = True 
                        
                    is_trap = False
                    if push_signal and not is_shakeout:
                        is_trap = bool(check_sec_fatal_traps(ticker))
                        if is_trap: current_signal += " 💀(SEC陷阱)"; stats["Signal"] = current_signal

                    with brain_lock:
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False, "Price": "-", "Pct": "-", "Vol": "-", "RelVol": "-", "Float": "-", "Signal": "", "Gap": "-"})
                        cell.update(stats)
                        if is_trap: cell["IsTrap"] = True
                        
                        if push_signal:
                            cooldown_tracker[ticker] = {'time': now_ts, 'level': current_level}
                            audio_target = "nova" if current_level >= 4 else ("spark" if current_level >= 2 else None)
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
                        MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')

                    threading.Thread(target=fetch_and_score_news, args=(ticker, cell), daemon=True).start()
                except Exception as e: continue
            time.sleep(5)
        except Exception as e: time.sleep(10)

# 💡 新增 API 端點：接收前端動態修改跳空門檻，並強制重置雷達
@app.route('/api/config', methods=['POST'])
def update_config():
    global MIN_GAP_PCT, _last_list_update
    data = request.json
    if 'gap_pct' in data:
        try:
            MIN_GAP_PCT = float(data['gap_pct'])
            _last_list_update = 0 # 💡 將計時器歸零，強制雷達引擎下一秒立刻重新掃描新名單！
        except: pass
    return jsonify({"status": "success", "gap_pct": MIN_GAP_PCT})

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data():
    with brain_lock: 
        safe_brain = copy.deepcopy(MASTER_BRAIN)
        safe_brain["live_trends"] = get_live_trends()
        safe_brain["current_gap"] = MIN_GAP_PCT # 💡 下發當前跳空值給前端同步顯示
        try: safe_brain["trends_date"] = datetime.fromtimestamp(os.path.getmtime(TRENDS_FILE_PATH), TZ_TW).strftime("%Y-%m-%d %H:%M:%S")
        except: safe_brain["trends_date"] = "尚未生成"
    return jsonify(safe_brain)

if __name__ == '__main__':
    settle_discovery_logs() 
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=auto_trend_updater, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)