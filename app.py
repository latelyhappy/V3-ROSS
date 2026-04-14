import time, threading, os, json, copy, re
from datetime import datetime, timedelta
import pandas as pd
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import pytz
from tvDatafeed import TvDatafeed, Interval
from flask import Flask, jsonify, render_template
from deep_translator import GoogleTranslator
from collections import Counter

# ==========================================
# 🛡️ 系統防護與戰術設定 (V41.13 解除死鎖版)
# ==========================================
# 💡 核心修復：改為 RLock (可重入鎖)，允許同一個執行緒多次進出，徹底消滅死鎖！
brain_lock = threading.RLock() 

TRENDS_FILE_PATH = os.path.join(os.path.dirname(__file__), 'trends.json')
_cached_trends = {}
_last_trends_update = 0
TRENDS_CACHE_TTL = 60

TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 8080))
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

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
    headers = {'User-Agent': 'SniperTrader/1.13 (contact@yourdomain.com)'}
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
                    if art['title'] == "⏳ 翻譯中...":
                        threading.Thread(target=background_translate_worker, args=(ticker, art['raw_title'], MASTER_BRAIN), daemon=True).start()
                        time.sleep(0.5) 
    except: pass

def extract_top_catalysts(master_brain):
    top_list = []
    with brain_lock:
        for ticker, data in master_brain.get('details', {}).items():
            if data.get('NewsList', []):
                top_list.append(data)
    try:
        return sorted(top_list, key=lambda x: (x.get('CatScore', 0), x['NewsList'][0].get('time', '00:00')), reverse=True)
    except: return top_list

def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP
    try:
        url = "https://scanner.tradingview.com/america/scan"
        # 💡 核心修復：補回 "close" 價格過濾器 (限制在 $1 以上)，排除反向分割造成的四百萬趴異常數據
        payload = {
            "filter": [
                {"left": "type", "operation": "in_range", "right": ["stock", "fund"]}, 
                {"left": "close", "operation": "in_range", "right": [1, 50]},
                {"left": "premarket_change", "operation": "nempty"}
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
                STATS_MAP[sym] = {'prev': prev_est, 'float_str': f_str, 'type': t}
    except: pass

# --- 🧠 全自動 NLP 熱點收索引擎 ---
def auto_trend_updater():
    STOP_WORDS = {"THE", "TO", "OF", "IN", "FOR", "A", "AND", "IS", "ON", "WITH", "BY", "AS", "AT", "FROM", "IT", "THAT", "THIS", "AN", "BE", "NEW", "UP", "OUT", "ITS", "ARE", "HAS"}
    
    # 💡 核心修復 1：開機先休眠 60 秒，讓主掃描器有充足時間去 TradingView 建立名單
    print("⏳ [NLP引擎] 開機預熱中，等待主雷達提供目標名單...")
    time.sleep(60)
    
    while True:
        try:
            with brain_lock:
                target_tickers = DYNAMIC_WATCHLIST.copy()
            
            # 💡 核心修復 2：如果名單還是空的，不要去睡 24 小時，等 10 秒再檢查一次
            if not target_tickers:
                time.sleep(10)
                continue

            print("🔍 [NLP引擎] 開始自動收索全網近期熱點新聞...")
            all_words = []
            
            for ticker in target_tickers:
                try:
                    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
                    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    if res.status_code == 200:
                        root = ET.fromstring(res.text)
                        for item in root.findall('./channel/item')[:3]:
                            title = item.find('title').text.upper()
                            words = re.findall(r'\b[A-Z]{3,}\b', title) 
                            for w in words:
                                if w not in STOP_WORDS and w != ticker:
                                    all_words.append(w)
                except: continue
                time.sleep(0.5) 

            if all_words:
                word_counts = Counter(all_words)
                top_trends = word_counts.most_common(15)
                
                new_trends = {}
                print("🔥 [NLP引擎] 本期提煉出的熱門催化劑單字：")
                for word, count in top_trends:
                    if count >= 2:
                        new_trends[word] = 5
                        print(f"   - {word}: 出現 {count} 次 (權重 +5)")
                
                with open(TRENDS_FILE_PATH, 'w', encoding='utf-8') as f:
                    json.dump(new_trends, f, ensure_ascii=False, indent=4)
                
                print(f"✅ [NLP引擎] 成功更新 trends.json，自動休眠 24 小時。")
                
        except Exception as e:
            print(f"🚨 [NLP引擎] 掃描發生異常: {e}")
            
        time.sleep(86400) # 休眠 24 小時 

def scanner_engine():
    tv = TvDatafeed()
    try:
        if TW_USERNAME != 'guest': tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
    except: pass

    last_list_update = 0
    consecutive_errors = 0 

    while True:
        try:
            now_ts = time.time()
            if now_ts - last_list_update > 300: 
                update_dynamic_watchlist()
                last_list_update = now_ts

            if not DYNAMIC_WATCHLIST:
                time.sleep(5); continue

            for ticker in DYNAMIC_WATCHLIST:
                try:
                    time.sleep(1.2) 
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
                    
                    if df is None or df.empty or len(df) < 10: 
                        consecutive_errors += 1
                        if consecutive_errors > 10:
                            print("🔄 偵測到連線疲乏，正在重新啟動 TradingView 引擎...")
                            tv = TvDatafeed()
                            try:
                                if TW_USERNAME != 'guest': tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
                            except: pass
                            consecutive_errors = 0
                        continue
                    consecutive_errors = 0 

                    p_live = float(df['close'].iloc[-1])
                    p_prev = float(df['close'].iloc[-2])
                    v_live = float(df['volume'].iloc[-1])
                    v_prev = float(df['volume'].iloc[-2])
                    
                    lookback = min(12, len(df))
                    avg_vol = df['volume'].iloc[-lookback:-2].mean() if lookback > 2 else df['volume'].iloc[-2]
                    
                    rel_vol_live = round(v_live / avg_vol, 2) if avg_vol > 0 else 1.0
                    rel_vol_prev = round(v_prev / avg_vol, 2) if avg_vol > 0 else 1.0
                    rel_vol_display = max(rel_vol_live, rel_vol_prev) 
                    daily_vol = int(df['volume'].sum())
                    
                    df['tr'] = pd.concat([(df['high'] - df['low']), (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
                    df['atr5'] = df['tr'].rolling(5, min_periods=3).mean()
                    df['atr20'] = df['tr'].rolling(20, min_periods=5).mean()
                    atr5 = df['atr5'].iloc[-1]
                    atr20 = df['atr20'].iloc[-1]

                    stat_data = STATS_MAP.get(ticker, {'prev': p_live, 'float_str': '-', 'type': 'stock'})
                    if stat_data['prev'] > 0:
                        prev_close = stat_data['prev']
                        float_str = stat_data['float_str']
                    else:
                        prev_close = float(df['low'].min()) 
                        float_str = "未知"
                    
                    real_pct = ((p_live - prev_close) / prev_close) * 100
                    
                    ema10 = df['close'].ewm(span=10, adjust=False).mean()
                    ema20 = df['close'].ewm(span=20, adjust=False).mean()
                    curr_ema10 = ema10.iloc[-1]
                    curr_ema20 = ema20.iloc[-1]
                    
                    past_high_for_live = df['high'].iloc[-11:-1].max() 
                    past_high_for_prev = df['high'].iloc[-12:-2].max() 
                    
                    spark_live = (rel_vol_live >= 2.5) and (p_live >= past_high_for_live)
                    spark_prev = (rel_vol_prev >= 2.5) and (p_prev >= past_high_for_prev) and (p_live >= df['open'].iloc[-2])
                    is_spark = (spark_live or spark_prev) and (real_pct > 3.0)

                    is_vcp_compression = (atr5 < atr20 * 0.95) and (rel_vol_prev < 0.85) and (curr_ema10 > curr_ema20)
                    is_ride = (p_live >= curr_ema20) and (abs(p_live - curr_ema20)/curr_ema20 < 0.012) and (rel_vol_prev < 0.8) and (real_pct > 1.0) and not is_spark
                    is_grind = (curr_ema10 > curr_ema20) and (p_live >= curr_ema10) and (0.5 <= rel_vol_display < 2.5) and (real_pct > 2.0) and not is_spark and not is_ride

                    tracker = STATE_TRACKER.get(ticker, {'state': 'None', 'duration': 0, 'vcp_low': float('inf')})
                    dynamic_stop = curr_ema20 * 0.99 
                    
                    if is_spark:
                        if tracker['state'] == 'VCP' and tracker['duration'] >= 3: dynamic_stop = tracker['vcp_low']
                        tracker['state'] = 'None'; tracker['duration'] = 0; tracker['vcp_low'] = float('inf')
                    elif is_vcp_compression:
                        if tracker['state'] != 'VCP':
                            tracker['state'] = 'VCP'; tracker['duration'] = 1; tracker['vcp_low'] = min(df['low'].iloc[-1], df['low'].iloc[-2])
                        else:
                            tracker['duration'] += 1; tracker['vcp_low'] = min(tracker['vcp_low'], df['low'].iloc[-1])
                    else:
                        tracker['state'] = 'None'; tracker['duration'] = 0; tracker['vcp_low'] = float('inf')
                    STATE_TRACKER[ticker] = tracker 

                    dollar_vol = p_live * max(v_live, v_prev, avg_vol)
                    vol_warn = "(⚠️量低)" if dollar_vol < 50000 else ""

                    current_signal = None; current_level = 0; status_color = "green"
                    if is_spark: current_signal = f"🔥強力點火 {vol_warn}"; current_level = 4; status_color = "yellow"
                    elif tracker['state'] == 'VCP' and tracker['duration'] >= 3: current_signal = f"⚡VCP壓縮鎖定 {vol_warn}"; current_level = 3; status_color = "vcp" 
                    elif is_grind: current_signal = f"🚜穩步推升 {vol_warn}"; current_level = 2; status_color = "blue"  
                    elif is_ride: current_signal = f"💎趨勢滑行 {vol_warn}"; current_level = 1; status_color = "purple"

                    stats = {
                        "Code": ticker, "Price": f"${p_live:.2f}", "RelVol": f"{rel_vol_display}x", "Vol": format_vol(daily_vol),
                        "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p_live-prev_close):+.2f}", "Status": status_color, 
                        "Signal": current_signal if current_signal else "",
                        "PriceVal": p_live, "StopLoss": dynamic_stop, "Float": float_str, "Type": stat_data.get('type', 'stock')
                    }
                    
                    with brain_lock:
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {
                            "NewsList": [], "CatScore": 0, "IsTrap": False, 
                            "Price": "-", "Pct": "-", "Vol": "-", "RelVol": "-", "Float": "-", "Signal": ""
                        })
                        cell.update(stats)
                        
                        for rank_item in MASTER_BRAIN["leaderboard"]:
                            if rank_item["Code"] == ticker:
                                rank_item["Price"] = f"${p_live:.2f}"
                                rank_item["Pct"] = f"{real_pct:+.2f}%"
                                rank_item["Status"] = status_color if status_color != "green" else rank_item.get("Status", "green")
                                break

                        if current_signal:
                            last_record = cooldown_tracker.get(ticker, {'time': 0, 'level': 0})
                            time_elapsed = now_ts - last_record['time']
                            
                            push_signal = False
                            if time_elapsed > 45: push_signal = True 
                            elif current_level > last_record['level']: push_signal = True 
                            
                            if push_signal:
                                if check_sec_fatal_traps(ticker):
                                    current_signal += " 💀(SEC陷阱)"; cell["IsTrap"] = True; stats["Signal"] = current_signal
                                cooldown_tracker[ticker] = {'time': now_ts, 'level': current_level}
                                audio_target = "nova" if current_level == 4 else ("spark" if current_level == 1 else None)
                                log_entry = {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": audio_target}
                                MASTER_BRAIN["surge_log"].insert(0, log_entry)
                                MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:500]

                    threading.Thread(target=fetch_and_score_news, args=(ticker, cell), daemon=True).start()
                except Exception as e: continue
            
            with brain_lock:
                MASTER_BRAIN["leaderboard"] = [MASTER_BRAIN["details"][t] for t in DYNAMIC_WATCHLIST if t in MASTER_BRAIN["details"]][:20]
                MASTER_BRAIN["top_catalysts"] = extract_top_catalysts(MASTER_BRAIN)
                MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
            time.sleep(5)
        except Exception as e: time.sleep(10)

@app.route('/')
def index(): return render_template('index.html')
@app.route('/data')
def data():
    with brain_lock: 
        safe_brain = copy.deepcopy(MASTER_BRAIN)
        safe_brain["live_trends"] = get_live_trends()
        
        # 💡 新增：抓取 trends.json 的最後學習/修改時間
        try:
            mtime = os.path.getmtime(TRENDS_FILE_PATH)
            dt = datetime.fromtimestamp(mtime, TZ_TW)
            safe_brain["trends_date"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            safe_brain["trends_date"] = "尚未生成"
            
    return jsonify(safe_brain)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=auto_trend_updater, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)