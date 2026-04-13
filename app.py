import time, threading, os, json
from datetime import datetime
import pandas as pd
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import pytz
from tvDatafeed import TvDatafeed, Interval
from flask import Flask, jsonify, render_template
from deep_translator import GoogleTranslator

# ==========================================
# 🛠️ 戰略設定與全局記憶體 (V41.3 外部軍火庫版)
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

# 💡 從外部 JSON 檔案載入軍火庫
CATALYST_ARMORY = {}
armory_path = os.path.join(os.path.dirname(__file__), 'catalysts.json')
try:
    with open(armory_path, 'r', encoding='utf-8') as f:
        CATALYST_ARMORY = json.load(f)
    print("✅ 成功載入 catalysts.json 軍火庫")
except FileNotFoundError:
    print("❌ 警告：找不到 catalysts.json，使用空白預設值！")
    CATALYST_ARMORY = {"INVERTED_TRAPS":{}, "THEMATIC_TRENDS":{}, "RED":{}, "ORANGE":{}, "YELLOW":{}, "BLACK":{}}
except json.JSONDecodeError:
    print("❌ 警告：catalysts.json 格式錯誤，請檢查是否有漏掉逗號或引號！")
    CATALYST_ARMORY = {"INVERTED_TRAPS":{}, "THEMATIC_TRENDS":{}, "RED":{}, "ORANGE":{}, "YELLOW":{}, "BLACK":{}}

# 全局記憶體宣告
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

# --- 💡 [V41.3] HFT 智能評分引擎 ---
def calculate_hft_score(headline):
    text = headline.upper()
    total_score = 0
    is_trap = False

    # 1. 複合反轉詞與抹除機制
    for kw, score in CATALYST_ARMORY["INVERTED_TRAPS"].items():
        if kw in text:
            total_score += score
            text = text.replace(kw, "") # 拔除引信

    # 2. 陷阱掃描
    for kw, score in CATALYST_ARMORY["BLACK"].items():
        if kw in text:
            return -50, True

    # 3. 利多與熱點掃描
    if not is_trap:
        for category in ["RED", "ORANGE", "YELLOW", "THEMATIC_TRENDS"]:
            for kw, score in CATALYST_ARMORY[category].items():
                if kw in text:
                    total_score += score
                    
    return total_score, is_trap

# --- 💡 [V41.3] 背景翻譯工兵 ---
def background_translate_worker(ticker, en_headline, master_brain):
    try:
        zh_text = GoogleTranslator(source='auto', target='zh-TW').translate(en_headline)
        if ticker in master_brain['details']:
            news_list = master_brain['details'][ticker].get('NewsList', [])
            if len(news_list) > 0 and news_list[0].get('raw_title') == en_headline:
                news_list[0]['title'] = zh_text
    except Exception as e:
        print(f"[{ticker}] 翻譯異常: {e}")

# --- 🛰️ SEC EDGAR 陷阱雷達 ---
def check_sec_fatal_traps(ticker):
    headers = {'User-Agent': 'Sniper_Bot_V41/1.3 (contact@yourdomain.com)'}
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=&output=atom"
    try:
        res = requests.get(url, headers=headers, timeout=3)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for entry in root.findall('{http://www.w3.org/2005/Atom}entry')[:3]:
                title_elem = entry.find('{http://www.w3.org/2005/Atom}title')
                if title_elem is not None and title_elem.text:
                    title = title_elem.text.upper()
                    if any(trap in title for trap in ['S-1', 'S-3', 'F-1', 'F-3', '1-A']): 
                        return True
        return False
    except: return False

# --- 📰 新聞掃描 (整合 HFT 引擎與非同步翻譯) ---
def fetch_and_score_news(ticker, cell, stats):
    now = time.time()
    if ticker in news_cache and (now - news_cache[ticker] < 900): return
    news_cache[ticker] = now
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []; total_score = 0; has_black = False
            
            for item in root.findall('./channel/item')[:5]:
                raw_t = item.find('title').text
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_NY)
                is_today = (dt.date() == datetime.now(TZ_NY).date())
                
                # 呼叫 V41.3 極速評分引擎
                score, is_trap = calculate_hft_score(raw_t)
                if is_trap: has_black = True
                
                total_score += score if is_today else 0
                
                articles.append({
                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": raw_t,
                    "link": item.find('link').text, "time": dt.strftime("%H:%M"), 
                    "is_today": is_today, "score": score
                })
                
            cell["NewsList"] = articles; cell["CatScore"] = total_score
            cell["IsTrap"] = has_black 
            cell["HasNews"] = (total_score > 5)

            # 啟動背景翻譯 (只翻譯最新一則)
            if articles:
                threading.Thread(target=background_translate_worker, args=(ticker, articles[0]['raw_title'], MASTER_BRAIN), daemon=True).start()
    except: pass

# --- 💡 [V41.3] 焦點新聞萃取器 ---
def extract_top_catalysts(master_brain):
    top_catalysts = []
    for ticker, data in master_brain.get('details', {}).items():
        score = data.get('CatScore', 0)
        is_trap = data.get('IsTrap', False)
        
        if score >= 6 and not is_trap:
            raw_signal = data.get('Signal', '')
            tactical_status = raw_signal if raw_signal else "⏳ 潛伏中"
            
            news_list = data.get('NewsList', [])
            if len(news_list) > 0:
                latest_news = news_list[0]
                if latest_news.get('is_today', False): # 確保只顯示今日重磅
                    top_catalysts.append({
                        "time": latest_news.get('time', '00:00'),
                        "ticker": ticker,
                        "score": score,
                        "headline": latest_news.get('title', '⏳ 翻譯中...'), 
                        "raw_headline": latest_news.get('raw_title', 'No Headline'),
                        "status": tactical_status
                    })
    return sorted(top_catalysts, key=lambda x: (x['score'], x['time']), reverse=True)

# --- 🛰️ TV 盤前雷達 ---
def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP
    try:
        url = "https://scanner.tradingview.com/america/scan"
        payload = {
            "filter": [
                {"left": "type", "operation": "in_range", "right": ["stock", "fund"]},
                {"left": "exchange", "operation": "in_range", "right": ["AMEX", "NASDAQ", "NYSE"]},
                {"left": "premarket_close", "operation": "in_range", "right": [1, 30]},
                {"left": "premarket_change", "operation": "nempty"}
            ],
            "options": {"lang": "en"},
            "markets": ["america"],
            "symbols": {"query": {"types": []}, "tickers": []},
            "columns": ["name", "premarket_close", "close", "premarket_change", "premarket_volume", "market_cap_basic", "type"],
            "sort": {"sortBy": "premarket_change", "sortOrder": "desc"},
            "range": [0, 50]
        }
        res = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = res.json()
        
        full_pool = []
        for item in data.get('data', []):
            sym = item['d'][0]
            price = item['d'][1] if item['d'][1] is not None else item['d'][2]
            pct = item['d'][3]
            vol = item['d'][4]
            mc = item['d'][5]
            asset_type = item['d'][6]
            
            if price and pct is not None:
                float_m = (mc / price) / 1_000_000 if mc else 0.0
                prev_est = price / (1 + (pct/100)) if pct != -100 else price
                full_pool.append({"sym": sym, "price": price, "prev": prev_est, "pct": pct, "vol": int(vol) if vol else 0, "float": float_m, "type": asset_type})

        if full_pool:
            DYNAMIC_WATCHLIST = [x['sym'] for x in full_pool[:30]]
            instant_leaderboard = []
            for x in full_pool[:20]:
                STATS_MAP[x['sym']] = {'prev': x['prev'], 'float': x['float']}
                float_str = f"{x['float']:.1f}M" if x['float'] > 0 else "未知"
                if x['type'] == 'fund': float_str = "N/A (ETF)" 
                elif x['float'] > 50.0: float_str = f"⚠️{float_str}"
                
                instant_leaderboard.append({
                    "Code": x['sym'], "Price": f"${x['price']:.2f}", "Float": float_str,
                    "Pct": f"{x['pct']:+.2f}%", "Vol": format_vol(x['vol']),
                    "RelVol": "-", "Status": "green", "Type": x['type']
                })
            MASTER_BRAIN["leaderboard"] = instant_leaderboard
    except: pass

# --- 🧠 主引擎迴圈 ---
def scanner_engine():
    tv = TvDatafeed()
    try:
        if TW_USERNAME != 'guest' and TW_PASSWORD != 'guest': tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
    except: pass

    last_list_update = 0
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
                    time.sleep(1.0)
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
                    if df is None or df.empty or len(df) < 10: continue

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
                    
                    df['tr'] = pd.concat([
                        df['high'] - df['low'],
                        (df['high'] - df['close'].shift(1)).abs(),
                        (df['low'] - df['close'].shift(1)).abs()
                    ], axis=1).max(axis=1)
                    df['atr5'] = df['tr'].rolling(5, min_periods=3).mean()
                    df['atr20'] = df['tr'].rolling(20, min_periods=5).mean()
                    atr5 = df['atr5'].iloc[-1]
                    atr20 = df['atr20'].iloc[-1]

                    stat_data = STATS_MAP.get(ticker, None)
                    if stat_data and stat_data['prev'] > 0:
                        prev_close = stat_data['prev']
                        float_str = f"{stat_data['float']:.1f}M"
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

                    current_signal = None
                    current_level = 0
                    status_color = "green"

                    if is_spark:
                        current_signal = f"🔥強力點火 {vol_warn}"; current_level = 4; status_color = "yellow"
                    elif tracker['state'] == 'VCP' and tracker['duration'] >= 3:
                        current_signal = f"⚡VCP壓縮鎖定 {vol_warn}"; current_level = 3; status_color = "vcp" 
                    elif is_grind:
                        current_signal = f"🚜穩步推升 {vol_warn}"; current_level = 2; status_color = "blue"  
                    elif is_ride:
                        current_signal = f"💎趨勢滑行 {vol_warn}"; current_level = 1; status_color = "purple"

                    stats = {
                        "Code": ticker, "Price": f"${p_live:.2f}", "RelVol": f"{rel_vol_display}x", "Vol": format_vol(daily_vol),
                        "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p_live-prev_close):+.2f}", "Status": status_color, 
                        "Signal": current_signal if current_signal else "",
                        "PriceVal": p_live, "StopLoss": dynamic_stop, "Float": float_str
                    }
                    
                    cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False})
                    cell.update(stats)
                    threading.Thread(target=fetch_and_score_news, args=(ticker, cell, stats)).start()

                    for rank_item in MASTER_BRAIN["leaderboard"]:
                        if rank_item["Code"] == ticker:
                            rank_item["Price"] = f"${p_live:.2f}"
                            rank_item["Pct"] = f"{real_pct:+.2f}%"
                            rank_item["Status"] = status_color if status_color != "green" else rank_item.get("Status", "green")
                            break

                    if current_signal:
                        last_record = cooldown_tracker.get(ticker, {'time': 0, 'level': 0})
                        time_elapsed = now_ts - last_record['time']
                        last_level = last_record['level']
                        
                        push_signal = False
                        if time_elapsed > 45: push_signal = True 
                        elif current_level > last_level: push_signal = True 
                        
                        if push_signal:
                            if check_sec_fatal_traps(ticker):
                                current_signal += " 💀(SEC陷阱)"
                                cell["IsTrap"] = True 
                                stats["Signal"] = current_signal

                            cooldown_tracker[ticker] = {'time': now_ts, 'level': current_level}
                            
                            audio_target = None
                            if current_level == 4: audio_target = "nova"
                            elif current_level == 1: audio_target = "spark"

                            log_entry = {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": audio_target}
                            MASTER_BRAIN["surge_log"].insert(0, log_entry)
                            MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:500]

                except: continue
            
            # 更新模組五：焦點新聞萃取
            MASTER_BRAIN["top_catalysts"] = extract_top_catalysts(MASTER_BRAIN)
            MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
            time.sleep(5)
        except: time.sleep(10)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)