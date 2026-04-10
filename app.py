import time, threading, json, os, random
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
# 🛠️ 戰略設定與全局記憶體
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))

TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

CATALYST_ARMORY = {
    "LEVEL_RED": {"FDA": 10, "APPROVAL": 10, "批准": 10, "ACQUISITION": 10, "收購": 10, "合約": 10, "軋空": 9},
    "LEVEL_ORANGE": {"EARNINGS": 7, "超越預期": 7, "三期": 7, "AI": 6},
    "LEVEL_YELLOW": {"PATENT": 5, "專利": 5, "臨床": 5, "晶片": 5},
    "LEVEL_BLACK": {"OFFERING": -30, "增發": -30, "BANKRUPTCY": -50, "破產": -50, "DEFAULT": -20}
}

MASTER_BRAIN = {"surge_log": [], "details": {}, "leaderboard": [], "last_update": ""}
DYNAMIC_WATCHLIST = []
STATS_MAP = {} 
news_cache = {} 
cooldown_tracker = {}

# 💡 [V40 核心] 跨週期狀態記憶體
# 格式: {ticker: {'state': 'VCP'/'None', 'duration': 分鐘數, 'vcp_low': 最低點}}
STATE_TRACKER = {}

app = Flask(__name__)

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

# --- 🛰️ SEC EDGAR 陷阱掃描 ---
def check_sec_edgar_traps(ticker):
    headers = {'User-Agent': 'Sniper_Commander_Bot/40.0 (contact@yourdomain.com)'}
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=&output=atom"
    try:
        response = requests.get(url, headers=headers, timeout=3)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            for entry in root.findall('{http://www.w3.org/2005/Atom}entry')[:3]:
                title_elem = entry.find('{http://www.w3.org/2005/Atom}title')
                if title_elem is not None and title_elem.text:
                    title = title_elem.text.upper()
                    if any(trap in title for trap in ['S-1', 'S-3', '1-A', '8-K']):
                        return True
        return False
    except:
        return False

# --- 📰 新聞與催化劑掃描 ---
def fetch_and_score_news(ticker, cell, stats):
    now = time.time()
    if ticker in news_cache and (now - news_cache[ticker] < 900): return
    news_cache[ticker] = now
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            translator = GoogleTranslator(source='auto', target='zh-TW')
            articles = []; total_score = 0; has_black = False
            for item in root.findall('./channel/item')[:5]:
                raw_t = item.find('title').text.upper()
                try: zh_t = translator.translate(raw_t)
                except: zh_t = raw_t
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_NY)
                is_today = (dt.date() == datetime.now(TZ_NY).date())
                
                score = 0
                for lv, kw_dict in CATALYST_ARMORY.items():
                    for kw, val in kw_dict.items():
                        if kw in raw_t or kw in zh_t:
                            score += val
                            if lv == "LEVEL_BLACK": has_black = True
                total_score += score if is_today else 0
                articles.append({"ticker": ticker, "title": zh_t, "link": item.find('link').text, "time": dt.strftime("%H:%M"), "is_today": is_today, "score": score})
            cell["NewsList"] = articles; cell["CatScore"] = total_score
            cell["IsTrap"] = has_black 
            cell["HasNews"] = (total_score > 5)
    except: pass

# --- 🛰️ TradingView 盤前雷達 ---
def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP
    try:
        url = "https://scanner.tradingview.com/america/scan"
        payload = {
            "filter": [
                {"left": "type", "operation": "in_range", "right": ["stock"]},
                {"left": "exchange", "operation": "in_range", "right": ["AMEX", "NASDAQ", "NYSE"]},
                {"left": "premarket_close", "operation": "in_range", "right": [1, 30]},
                {"left": "premarket_change", "operation": "nempty"}
            ],
            "options": {"lang": "en"},
            "markets": ["america"],
            "symbols": {"query": {"types": []}, "tickers": []},
            "columns": ["name", "premarket_close", "close", "premarket_change", "premarket_volume", "market_cap_basic"],
            "sort": {"sortBy": "premarket_change", "sortOrder": "desc"},
            "range": [0, 50]
        }
        res = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = res.json()
        
        full_pool = []
        for item in data.get('data', []):
            sym = item['d'][0]
            pre_price = item['d'][1] 
            reg_price = item['d'][2]
            price = pre_price if pre_price is not None else reg_price
            pct = item['d'][3]
            vol = item['d'][4]
            mc = item['d'][5]
            
            if price and pct is not None:
                float_m = (mc / price) / 1_000_000 if mc else 0.0
                prev_est = price / (1 + (pct/100)) if pct != -100 else price
                full_pool.append({"sym": sym, "price": price, "prev": prev_est, "pct": pct, "vol": int(vol) if vol else 0, "float": float_m})

        if full_pool:
            DYNAMIC_WATCHLIST = [x['sym'] for x in full_pool[:30]]
            instant_leaderboard = []
            for x in full_pool[:20]:
                STATS_MAP[x['sym']] = {'prev': x['prev'], 'float': x['float']}
                float_str = f"{x['float']:.1f}M" if x['float'] > 0 else "未知"
                if x['float'] > 50.0: float_str = f"⚠️{float_str}"
                instant_leaderboard.append({
                    "Code": x['sym'], "Price": f"${x['price']:.2f}", "Float": float_str,
                    "Pct": f"{x['pct']:+.2f}%", "Vol": format_vol(x['vol']),
                    "RelVol": "-", "Status": "green"
                })
            MASTER_BRAIN["leaderboard"] = instant_leaderboard
            print(f"✅ TV 掃描成功！鎖定 {len(DYNAMIC_WATCHLIST)} 檔標的。")
    except Exception as e:
        print(f"❌ TV 掃描異常: {e}")

# --- 🧠 V40 量化引擎主循環 ---
def scanner_engine():
    tv = TvDatafeed()
    try:
        if TW_USERNAME != 'guest' and TW_PASSWORD != 'guest':
            tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
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
                    
                    # 💡 放寬安全鎖：只要有 10 根 K 線就可以開始掃描 (適合凌晨盤前)
                    if df is None or df.empty or len(df) < 10: continue

                    p_live = float(df['close'].iloc[-1])
                    p_prev = float(df['close'].iloc[-2])
                    v_live = float(df['volume'].iloc[-1])
                    v_prev = float(df['volume'].iloc[-2])
                    
                    # 💡 動態回溯：如果 K 線不夠 12 根，就用現有長度算平均量
                    lookback = min(12, len(df))
                    avg_vol = df['volume'].iloc[-lookback:-2].mean() if lookback > 2 else df['volume'].iloc[-2]
                    
                    rel_vol_live = round(v_live / avg_vol, 2) if avg_vol > 0 else 1.0
                    rel_vol_prev = round(v_prev / avg_vol, 2) if avg_vol > 0 else 1.0
                    rel_vol_display = max(rel_vol_live, rel_vol_prev) 
                    
                    daily_vol = int(df['volume'].sum())
                    
                    # 💡 [V40] 波動率 (ATR) 計算 (加入 min_periods 防止 K 線不足時當機)
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
                    
                    # 核心條件判斷
                    past_high_for_live = df['high'].iloc[-11:-1].max() 
                    past_high_for_prev = df['high'].iloc[-12:-2].max() 
                    
                    spark_live = (rel_vol_live >= 2.5) and (p_live >= past_high_for_live)
                    spark_prev = (rel_vol_prev >= 2.5) and (p_prev >= past_high_for_prev) and (p_live >= df['open'].iloc[-2])
                    is_spark = (spark_live or spark_prev) and (real_pct > 3.0)

                    # 💡 [V40] VCP 壓縮掃描: ATR 收斂 且 量縮 且 均線多頭
                    is_vcp_compression = (atr5 < atr20 * 0.95) and (rel_vol_prev < 0.85) and (curr_ema10 > curr_ema20)
                    
                    is_ride = (p_live >= curr_ema20) and (abs(p_live - curr_ema20)/curr_ema20 < 0.012) and (rel_vol_prev < 0.8) and (real_pct > 1.0) and not is_spark
                    is_grind = (curr_ema10 > curr_ema20) and (p_live >= curr_ema10) and (0.5 <= rel_vol_display < 2.5) and (real_pct > 2.0) and not is_spark and not is_ride

                    # 💡 [V40] 狀態記憶體更新與動態停損
                    tracker = STATE_TRACKER.get(ticker, {'state': 'None', 'duration': 0, 'vcp_low': float('inf')})
                    dynamic_stop = curr_ema20 * 0.99 # 預設常規停損
                    
                    if is_spark:
                        # 如果是從 VCP 爆發出來，套用超級窄損！
                        if tracker['state'] == 'VCP' and tracker['duration'] >= 3:
                            dynamic_stop = tracker['vcp_low']
                        # 爆發後重置記憶體
                        tracker['state'] = 'None'
                        tracker['duration'] = 0
                        tracker['vcp_low'] = float('inf')
                    elif is_vcp_compression:
                        if tracker['state'] != 'VCP':
                            tracker['state'] = 'VCP'
                            tracker['duration'] = 1
                            tracker['vcp_low'] = min(df['low'].iloc[-1], df['low'].iloc[-2])
                        else:
                            tracker['duration'] += 1
                            tracker['vcp_low'] = min(tracker['vcp_low'], df['low'].iloc[-1])
                    else:
                        # 既沒點火也沒壓縮，重置
                        tracker['state'] = 'None'
                        tracker['duration'] = 0
                        tracker['vcp_low'] = float('inf')
                    
                    STATE_TRACKER[ticker] = tracker # 存回全局記憶

                    # 資金流警告
                    dollar_vol = p_live * max(v_live, v_prev, avg_vol)
                    vol_warn = "(⚠️量低)" if dollar_vol < 50000 else ""

                    # 💡 [V40] 最終信號判定與分級 (優先級: 🔥4 > ⚡3 > 🚜2 > 💎1)
                    current_signal = None
                    current_level = 0
                    status_color = "green"

                    if is_spark:
                        current_signal = f"🔥強力點火 {vol_warn}"
                        current_level = 4
                        status_color = "yellow"
                    elif tracker['state'] == 'VCP' and tracker['duration'] >= 3:
                        current_signal = f"⚡VCP壓縮鎖定 {vol_warn}"
                        current_level = 3
                        status_color = "vcp" # 前端會解析為黃色光暈
                    elif is_grind:
                        current_signal = f"🚜穩步推升 {vol_warn}"
                        current_level = 2
                        status_color = "blue"  
                    elif is_ride:
                        current_signal = f"💎趨勢滑行 {vol_warn}"
                        current_level = 1
                        status_color = "purple"

                    stats = {
                        "Code": ticker, "Price": f"${p_live:.2f}", "RelVol": f"{rel_vol_display}x", "Vol": format_vol(daily_vol),
                        "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p_live-prev_close):+.2f}", "Status": status_color, 
                        "Signal": current_signal if current_signal else "",
                        "PriceVal": p_live, "StopLoss": dynamic_stop, "Float": float_str
                    }
                    
                    cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False})
                    cell.update(stats)
                    threading.Thread(target=fetch_and_score_news, args=(ticker, cell, stats)).start()

                    if current_signal:
                        last_record = cooldown_tracker.get(ticker, {'time': 0, 'level': 0})
                        time_elapsed = now_ts - last_record['time']
                        last_level = last_record['level']
                        
                        push_signal = False
                        if time_elapsed > 45:
                            push_signal = True 
                        elif current_level > last_level:
                            push_signal = True 
                        
                        if push_signal:
                            if check_sec_edgar_traps(ticker):
                                current_signal += " 💀(SEC陷阱)"
                                cell["IsTrap"] = True 
                                stats["Signal"] = current_signal

                            cooldown_tracker[ticker] = {'time': now_ts, 'level': current_level}
                            
                            # 💡 [V40] 靜默追蹤控制：只有 🔥(4) 和 💎(1) 會有音效，VCP與推土機完全靜音
                            audio_target = None
                            if current_level == 4: audio_target = "nova"
                            elif current_level == 1: audio_target = "spark"

                            log_entry = {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": audio_target}
                            MASTER_BRAIN["surge_log"].insert(0, log_entry)
                            MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:500]

                except Exception as ex: 
                    continue
            
            MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
            time.sleep(5)
        except Exception as e: time.sleep(10)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)