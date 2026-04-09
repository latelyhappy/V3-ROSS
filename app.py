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
# 🛠️ 戰略設定
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))

TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

CATALYST_ARMORY = {
    "LEVEL_RED": {"FDA": 10, "APPROVAL": 10, "批准": 10, "ACQUISITION": 10, "收購": 10, "DOD": 10, "合約": 10, "SQUEEZE": 9, "軋空": 9},
    "LEVEL_ORANGE": {"EARNINGS": 7, "超越預期": 7, "PHASE 3": 7, "三期": 7, "DRONE": 7, "無人機": 7, "AI": 6},
    "LEVEL_YELLOW": {"PATENT": 5, "專利": 5, "CLINICAL": 5, "臨床": 5, "CHIP": 5, "晶片": 5},
    "LEVEL_BLACK": {"OFFERING": -30, "增發": -30, "BANKRUPTCY": -50, "破產": -50, "DEFAULT": -20}
}

MASTER_BRAIN = {"surge_log": [], "details": {}, "leaderboard": [], "last_update": ""}
DYNAMIC_WATCHLIST = []
STATS_MAP = {} 
news_cache = {} 
cooldown_tracker = {}
app = Flask(__name__)

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

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
            cell["HasNews"] = (total_score > 5); cell["IsTrap"] = has_black
    except: pass

# --- 🛰️ V24.3 王牌特務版 (直連 TradingView 原生雷達) ---
def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP
    try:
        print("👁️ 啟動 TradingView 原生盤前掃描雷達...")
        
        # 💡 TV 隱藏版掃描伺服器
        url = "https://scanner.tradingview.com/america/scan"
        
        # 💡 精準戰術配置：$1-$30、純股票、依盤前漲幅排序
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
            print(f"✅ TV 原生雷達掃描成功！完美鎖定 {len(DYNAMIC_WATCHLIST)} 檔盤前妖股。")
        else:
            print("⚠️ TV 掃描未回傳數據，盤前可能尚無交易。")

    except Exception as e:
        print(f"❌ TV 雷達掃描異常: {e}")

# --- 🧠 戰術雷達主引擎 ---
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
            # 每 5 分鐘呼叫一次 TV 掃描器更新大名單
            if now_ts - last_list_update > 300: 
                update_dynamic_watchlist()
                last_list_update = now_ts

            if not DYNAMIC_WATCHLIST:
                time.sleep(5); continue

            for ticker in DYNAMIC_WATCHLIST:
                try:
                    time.sleep(1.0)
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
                    if df is None or df.empty: continue

                    p = float(df['close'].iloc[-1]); o = float(df['open'].iloc[0])
                    stat_data = STATS_MAP.get(ticker, {'prev': o, 'float': 0})
                    prev_close = stat_data['prev']
                    float_str = f"{stat_data['float']:.1f}M"
                    
                    real_pct = ((p - prev_close) / prev_close) * 100
                    daily_vol = int(df['volume'].sum())
                    avg_vol = df['volume'].iloc[-11:-1].mean()
                    rel_vol = round(df['volume'].iloc[-1] / avg_vol, 2) if avg_vol > 0 else 1.0
                    
                    ema20 = df['close'].ewm(span=20, adjust=False).mean()
                    curr_ema20 = ema20.iloc[-1]
                    is_ema20_up = curr_ema20 > ema20.iloc[-2]
                    
                    vol_warn = "(⚠️量低)" if daily_vol < 200000 else ""
                    
                    # 💡 戰術訊號判定
                    is_spark = (rel_vol >= 1.5) and (p >= df['high'].iloc[-11:-1].max()) and (real_pct > 1.5)
                    is_ride = is_ema20_up and (abs(p - curr_ema20)/curr_ema20 < 0.012) and (p >= curr_ema20) and (real_pct > 1.0) and not is_spark
                    is_weak = (p < o) or (real_pct < -2.0)
                    
                    tag = f"🔥強力點火 {vol_warn}" if is_spark else (f"💎趨勢滑行 {vol_warn}" if is_ride else "")
                    status = "yellow" if is_spark else ("purple" if is_ride else ("red" if is_weak else "green"))

                    stats = {
                        "Code": ticker, "Price": f"${p:.2f}", "RelVol": f"{rel_vol}x", "Vol": format_vol(daily_vol),
                        "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p-prev_close):+.2f}", "Status": status, "Signal": tag,
                        "PriceVal": p, "StopLoss": curr_ema20*0.99, "Float": float_str
                    }
                    
                    cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False})
                    cell.update(stats)

                    # 紀錄到日誌 (僅顯示點火與滑行)
                    if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 45):
                        cooldown_tracker[ticker] = now_ts
                        log_entry = {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova" if is_spark else "spark"}
                        MASTER_BRAIN["surge_log"].insert(0, log_entry)
                        MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:500]
                    
                    threading.Thread(target=fetch_and_score_news, args=(ticker, cell, stats)).start()
                except: continue
            
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