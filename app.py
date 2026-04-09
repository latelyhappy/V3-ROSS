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

# --- 🛰️ V24.0 雙引擎動態名單 (爬蟲 + API 備援) ---
def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, STATS_MAP
    full_pool = []
    
    # 🕷️ 引擎 A：StockAnalysis 盤前隱形爬蟲
    try:
        print("🕷️ 嘗試啟動隱形爬蟲 (StockAnalysis)...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        }
        url = "https://stockanalysis.com/markets/premarket/gainers/"
        res = requests.get(url, headers=headers, timeout=8)
        
        dfs = pd.read_html(res.text)
        df = dfs[0]
        
        for _, row in df.iterrows():
            sym = str(row.get('Symbol', '')).strip()
            if not sym or sym == 'nan': continue
            
            p_str = str(row.get('Premkt. Price', '0')).replace('$','').replace(',','')
            pct_str = str(row.get('% Change', '0')).replace('%','').replace(',','')
            vol_str = str(row.get('Pre. Volume', '0')).replace(',','')
            mc_str = str(row.get('Market Cap', '0'))
            
            price = float(p_str) if p_str.replace('.','').isdigit() else 0.0
            pct = float(pct_str) if pct_str.replace('.','').replace('-','').isdigit() else 0.0
            vol = int(float(vol_str)) if vol_str.replace('.','').isdigit() else 0
            
            mc_m = 0.0
            if 'B' in mc_str: mc_m = float(mc_str.replace('B','')) * 1000
            elif 'M' in mc_str: mc_m = float(mc_str.replace('M',''))
            elif 'K' in mc_str: mc_m = float(mc_str.replace('K','')) / 1000
            
            float_m = (mc_m / price) if price > 0 else 0.0
            
            if 1.0 <= price <= 30.0:
                prev_est = price / (1 + (pct/100)) if pct != -100 else price
                full_pool.append({"sym": sym, "price": price, "prev": prev_est, "pct": pct, "vol": vol, "float": float_m})
                
        if full_pool: print(f"✅ 爬蟲成功！抓取到 {len(full_pool)} 檔 $1-$30 盤前妖股。")
    except Exception as e:
        print(f"⚠️ 爬蟲受阻，可能被 Cloudflare 阻擋。啟動備援機制...")

    # 🛰️ 引擎 B：Yahoo API (備援系統)
    if not full_pool:
        try:
            print("🛰️ 啟動 Yahoo API 盤前掃描...")
            url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers&count=100"
            res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            quotes = res.json()['finance']['result'][0]['quotes']
            for q in quotes:
                symbol = q['symbol']
                pre_price = q.get('preMarketPrice')
                reg_price = q.get('regularMarketPrice')
                price = pre_price if (pre_price is not None and pre_price > 0) else reg_price
                if price is None or price == 0: continue
                
                raw_mc = q.get('marketCap')
                market_cap = float(raw_mc) if raw_mc is not None else 0.0
                float_shares_m = (market_cap / price) / 1_000_000
                
                if 1.0 <= price <= 30.0:
                    pre_pct = q.get('preMarketChangePercent')
                    reg_pct = q.get('regularMarketChangePercent')
                    pct = float(pre_pct) if (pre_pct is not None and pre_pct != 0) else (float(reg_pct) if reg_pct is not None else 0.0)
                    prev = q.get('regularMarketPreviousClose', price)
                    raw_vol = q.get('regularMarketVolume', 0)
                    full_pool.append({"sym": symbol, "price": price, "prev": prev, "pct": pct, "vol": int(raw_vol), "float": float_shares_m})
            print(f"✅ Yahoo 備援成功！鎖定 {len(full_pool)} 檔標的。")
        except Exception as e: print(f"❌ 雙引擎皆失效: {e}")

    # --- 排序與資料綁定 ---
    if full_pool:
        full_pool = sorted(full_pool, key=lambda x: x['pct'], reverse=True)
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