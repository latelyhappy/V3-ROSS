import time, threading, json, math, os, random
from datetime import datetime
import pandas as pd
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import pytz
from tvDatafeed import TvDatafeed, Interval
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template
from deep_translator import GoogleTranslator

# ==========================================
# 🛠️ 雲端環境變數與設定
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
SCAN_INTERVAL = 15
PORT = int(os.getenv('PORT', 5000))

MASTER_BRAIN = {
    "surge": [], "details": {}, "last_update": "", 
    "system_alert": "", "system_alert_ts": 0
}

# 🦅 盤前熱門觀察名單 (建議加入波動大的標的)
WATCHLIST = ["TSLA", "NVDA", "AAPL", "AMD", "META", "MARA", "RIOT", "COIN", "PLTR", "NNE", "OKLO", "SOXL"]
cooldown_tracker = {}

app = Flask(__name__)

# --- 📰 新聞獲取 + 繁體翻譯 ---
def fetch_news_bg(ticker, cell):
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []
            translator = GoogleTranslator(source='auto', target='zh-TW')
            for item in root.findall('./channel/item')[:5]:
                raw_t = item.find('title').text
                try:
                    zh_title = translator.translate(raw_t)
                except:
                    zh_title = raw_t 
                link = item.find('link').text
                articles.append({"title": zh_title, "link": link, "time": "News"})
            cell["NewsList"] = articles
    except: pass

# --- 🔌 數據獲取 (盤前強化偵錯版) ---
def safe_get_tw_data(tv, symbol):
    for i in range(3):
        try:
            # 💡 關鍵：必須開 extended_session 才能抓盤前
            df = tv.get_hist(symbol=symbol, exchange='', interval=Interval.in_1_minute, n_bars=100, extended_session=True)
            if df is not None and not df.empty:
                last_time = df.index[-1]
                if last_time.tz is None: last_time = last_time.tz_localize('UTC')
                delay = (datetime.now(last_time.tzinfo) - last_time).total_seconds()
                
                # 📢 日誌回報：讓指揮官在 Railway Logs 看到進度
                print(f"🔎 [雷達掃描] {symbol} | 最新價格: {df['close'].iloc[-1]} | 數據延遲: {int(delay)}秒")
                
                # 盤前交易稀疏，若超過 30 分鐘 (1800秒) 沒成交則視為殭屍股
                if delay > 1800: return None
                return df
        except Exception as e:
            print(f"❌ {symbol} 抓取異常: {str(e)}")
            time.sleep(1)
    return None

def analyze_liquidity(df, threshold=2000):
    if df is None or df.empty: return False, 0
    if df.index.tz is None: df.index = df.index.tz_localize('UTC')
    # 觀測窗口：美東 04:00 - 20:00 (盤前到盤後)
    df_market = df.tz_convert('US/Eastern').between_time('04:00', '20:00')
    if df_market.empty or df_market['volume'].iloc[-1] <= 0: return False, 0
    
    df_market['dollar_vol'] = df_market['close'] * df_market['volume']
    avg_vol = df_market['dollar_vol'].rolling(window=5, min_periods=1).mean().iloc[-1]
    return avg_vol >= threshold, avg_vol

# --- 🧠 戰術雷達 (ROSS 策略實裝) ---
def scanner_engine():
    global cooldown_tracker
    print("🔄 啟動 Sniper V19.8 (ROSS 策略實彈版)...")
    try:
        tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
        print("✅ TradingView 連線成功，開始監控名單...")
    except Exception as e:
        print(f"❌ TV 連線失敗: {e}")
        return

    while True:
        try:
            now_ts = time.time()
            batch_success = False
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_ticker = {executor.submit(safe_get_tw_data, tv, ticker): ticker for ticker in WATCHLIST}
                
                for future in future_to_ticker:
                    ticker = future_to_ticker[future]
                    df = future.result()
                    if df is not None:
                        batch_success = True
                        is_liquid, avg_vol = analyze_liquidity(df)
                        p = float(df['close'].iloc[-1])
                        
                        # 🦅 ROSS 策略核心邏輯
                        # 1. 價格區間過濾
                        is_ross_price = 1.0 <= p <= 50.0 
                        # 2. 相對爆量 (當前分鐘量 > 過去5分鐘均量 2.2倍)
                        recent_vol_mean = df['volume'].iloc[-6:-1].mean()
                        vol_spike = df['volume'].iloc[-1] > (recent_vol_mean * 2.2) if recent_vol_mean > 0 else False
                        # 3. 突破前高 (過去 15 分鐘最高點)
                        recent_high = df['high'].iloc[-16:-1].max()
                        is_breakout = p >= recent_high
                        
                        is_spark = is_ross_price and vol_spike and is_breakout and is_liquid
                        # 4. 鑽石回踩 (回調至 9 EMA 附近，此處簡化為過去 10 分鐘均價)
                        ma10 = df['close'].tail(10).mean()
                        is_diamond = is_ross_price and is_liquid and (df['low'].iloc[-1] <= ma10 <= p) and not is_spark
                        
                        mock_float = random.uniform(1.0, 15.0) 
                        tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else "")
                        audio_trigger = "nova" if (is_spark and mock_float < 3.0) else ("spark" if is_spark else ("diamond" if is_diamond else ""))

                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                        cell.update({
                            "PriceStr": f"${p:.2f}", "PriceVal": p, "StopLossVal": p * 0.98,
                            "Volume": f"{avg_vol/1000:.1f}K", "FloatStr": f"{mock_float:.1f}M",
                            "Change": "偵測中", "Signal": tag
                        })

                        if tag and (now_ts - cooldown_tracker.get(ticker, 0) > 30):
                            cooldown_tracker[ticker] = now_ts
                            MASTER_BRAIN["surge"].insert(0, {
                                "Code": ticker, "Price": f"${p:.2f}", "Streak": tag, 
                                "FloatStr": f"{mock_float:.1f}M", "AudioTrigger": audio_trigger, "SignalTS": now_ts
                            })
                            print(f"🎯 [訊號發報] {ticker} 觸發 {tag}！")
                        
                        threading.Thread(target=fetch_news_bg, args=(ticker, cell)).start()

            MASTER_BRAIN["surge"] = MASTER_BRAIN["surge"][:50]
            MASTER_BRAIN["last_update"] = datetime.now().strftime('%H:%M:%S')
            time.sleep(max(5, SCAN_INTERVAL + random.uniform(-2, 2)))
        except Exception as e:
            print(f"⚠️ 引擎迴圈異常: {e}")
            time.sleep(5)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)