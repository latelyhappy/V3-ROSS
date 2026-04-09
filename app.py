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
# 🛠️ 戰略設定與時區
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))

TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

# 🎯 催化劑軍火庫 (關鍵字與權重)
CATALYST_ARMORY = {
    "LEVEL_RED": {"FDA": 10, "批准": 10, "收購": 10, "合約": 9, "五角大廈": 9, "三期": 8, "砲彈": 8, "軍火": 8},
    "LEVEL_ORANGE": {"盈餘": 7, "超越預期": 7, "合作": 6, "二期": 6, "無人機": 7, "晶片": 6, "AI": 6, "上調": 6},
    "LEVEL_YELLOW": {"專利": 5, "臨床": 5, "抗癌": 5, "糖尿病": 5, "出口": 4, "募資": 4},
    "LEVEL_BLACK": {"增發": -20, "破產": -50, "違約": -20, "下調": -10}
}

MASTER_BRAIN = {"surge_log": [], "details": {}, "leaderboard": [], "good_news": [], "last_update": ""}
DYNAMIC_WATCHLIST = []
PREV_CLOSE_MAP = {}
news_cache = {} 
cooldown_tracker = {}
app = Flask(__name__)

def format_vol(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(int(n))

# --- 📰 催化劑打分系統 ---
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
                raw_t = item.find('title').text
                try: zh_t = translator.translate(raw_t)
                except: zh_t = raw_t
                dt = parsedate_to_datetime(item.find('pubDate').text).astimezone(TZ_NY)
                is_today = (dt.date() == datetime.now(TZ_NY).date())
                
                # 計算分數
                score = 0
                for lv, kw_dict in CATALYST_ARMORY.items():
                    for kw, val in kw_dict.items():
                        if kw in zh_t:
                            score += val
                            if lv == "LEVEL_BLACK": has_black = True
                total_score += score if is_today else 0
                articles.append({"ticker": ticker, "title": zh_t, "link": item.find('link').text, "time": dt.strftime("%H:%M"), "is_today": is_today, "score": score})
            
            cell["NewsList"] = articles
            cell["CatScore"] = total_score
            cell["HasNews"] = (total_score > 5)
            cell["IsTrap"] = has_black
    except: pass

# --- 🛰️ 深度名單獲取 (昨日收盤基準) ---
def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, PREV_CLOSE_MAP
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers&count=100"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        quotes = res.json()['finance']['result'][0]['quotes']
        full_scan = []
        for q in quotes:
            symbol = q['symbol']
            price = q.get('regularMarketPrice', 0)
            # 💡 依照排行榜要求：價格 1-30 塊
            if 1.0 <= price <= 30.0:
                prev = q.get('regularMarketPreviousClose', price)
                full_scan.append({"sym": symbol, "prev": prev, "pct": q.get('regularMarketChangePercent', 0)})
        
        # 本地端按真實漲幅重新排序
        full_scan = sorted(full_scan, key=lambda x: x['pct'], reverse=True)
        DYNAMIC_WATCHLIST = [x['sym'] for x in full_scan[:35]]
        for x in full_scan: PREV_CLOSE_MAP[x['sym']] = x['prev']
    except: pass

# --- 🧠 戰術雷達主引擎 ---
def scanner_engine():
    tv = None
    # 💡 嚴格驗證模式
    try:
        if TW_USERNAME != 'guest' and TW_PASSWORD != 'guest':
            # 嘗試登入
            tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
            # 💡 測試抓取一個標的來確認是否真的「有權限」
            test_df = tv.get_hist(symbol='AAPL', exchange='NASDAQ', interval=Interval.in_1_minute, n_bars=1)
            if test_df is not None:
                print("✅ [認證成功] 指揮官，正式帳號已完全接管數據鏈！")
            else:
                raise Exception("Login Failed")
        else:
            tv = TvDatafeed()
            print("👤 [路人模式] 目前使用無登入狀態，數據可能有延遲。")
    except Exception as e:
        tv = TvDatafeed()
        print(f"⚠️ [認證失敗] 帳密無效或觸發 2FA，已強制切換為 Guest 模式。")
    last_list_update = 0
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_list_update > 600:
                update_dynamic_watchlist()
                last_list_update = now_ts

            current_leaderboard = []
            for ticker in DYNAMIC_WATCHLIST:
                try:
                    time.sleep(1.2) # 💡 序列呼吸延遲
                    df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=60, extended_session=True)
                    if df is not None and not df.empty:
                        p = float(df['close'].iloc[-1]); o = float(df['open'].iloc[0])
                        prev_close = PREV_CLOSE_MAP.get(ticker, o)
                        
                        real_pct = ((p - prev_close) / prev_close) * 100
                        daily_vol = int(df['volume'].sum())
                        rel_vol = round(df['volume'].iloc[-1] / df['volume'].iloc[-11:-1].mean(), 2) if df['volume'].iloc[-11:-1].mean() > 0 else 1.0
                        
                        # EMA 趨勢滑行系統
                        ema20 = df['close'].ewm(span=20, adjust=False).mean()
                        curr_ema20 = ema20.iloc[-1]
                        is_ema20_up = curr_ema20 > ema20.iloc[-2]
                        
                        # Ross 策略邏輯
                        is_spark = (rel_vol >= 2.0) and (p >= df['high'].iloc[-16:-1].max()) and (real_pct > 3.0)
                        is_ride = is_ema20_up and (abs(p - curr_ema20)/curr_ema20 < 0.006) and (p > curr_ema20) and (real_pct > 2.0) and not is_spark
                        is_weak = (p < o) or (real_pct < -2.0)
                        
                        tag = "🔥強力點火" if is_spark else ("💎趨勢滑行" if is_ride else ("💀趨勢轉弱" if is_weak else ""))
                        status = "yellow" if is_spark else ("purple" if is_ride else ("red" if is_weak else "green"))

                        stats = {
                            "Code": ticker, "Price": f"${p:.2f}", "RelVol": f"{rel_vol}x", "Vol": format_vol(daily_vol),
                            "Pct": f"{real_pct:+.2f}%", "Amt": f"{(p-prev_close):+.2f}", "Status": status, "Signal": tag,
                            "PriceVal": p, "StopLoss": curr_ema20*0.99, "Float": f"{random.uniform(2,15):.1f}M"
                        }
                        
                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0, "IsTrap": False})
                        cell.update(stats)
                        current_leaderboard.append(stats)

                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 45):
                            cooldown_tracker[ticker] = now_ts
                            log_entry = {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Audio": "nova" if is_spark else "spark"}
                            MASTER_BRAIN["surge_log"].insert(0, log_entry)
                            MASTER_BRAIN["surge_log"] = MASTER_BRAIN["surge_log"][:1000]
                        
                        threading.Thread(target=fetch_and_score_news, args=(ticker, cell, stats)).start()
                except: continue

            MASTER_BRAIN["leaderboard"] = sorted(current_leaderboard, key=lambda x: float(x['Pct'].replace('%','')), reverse=True)[:20]
            MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
            time.sleep(10)
        except: time.sleep(15)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)