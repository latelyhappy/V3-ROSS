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
# 🛠️ 雲端環境變數
# ==========================================
TW_USERNAME = os.getenv('TW_USERNAME', 'guest') 
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 5000))

# 格式嚴格對齊：Surge 清單包含完整格式資料
MASTER_BRAIN = {
    "surge": [], 
    "details": {}, 
    "leaderboard": [], 
    "good_news": [],
    "last_update": ""
}

WATCHLIST = ["TSLA", "NVDA", "AAPL", "AMD", "META", "MARA", "RIOT", "COIN", "PLTR", "NNE", "OKLO", "SOXL"]
cooldown_tracker = {}

app = Flask(__name__)

# --- 📰 翻譯與獨立好消息篩選 ---
GOOD_KEYWORDS = ["成長", "獲利", "升評", "收購", "大漲", "新高", "盈餘", "亮眼", "突破", "買入", "優於", "合作"]

def fetch_news_bg(ticker, cell):
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []
            translator = GoogleTranslator(source='auto', target='zh-TW')
            
            # 清理該股票的舊好消息，避免重複顯示
            MASTER_BRAIN["good_news"] = [n for n in MASTER_BRAIN["good_news"] if n['ticker'] != ticker]
            
            for item in root.findall('./channel/item')[:5]:
                raw_t = item.find('title').text
                try: zh_title = translator.translate(raw_t)
                except: zh_title = raw_t 
                
                dt_raw = item.find('pubDate').text
                dt = parsedate_to_datetime(dt_raw).astimezone(pytz.timezone('America/New_York'))
                is_today = (dt.date() == datetime.now(pytz.timezone('America/New_York')).date())
                t_str = dt.strftime("%H:%M") if is_today else dt.strftime("%m/%d %H:%M")
                
                news_obj = {"ticker": ticker, "title": zh_title, "link": item.find('link').text, "time": t_str, "is_today": is_today}
                articles.append(news_obj)
                
                # AI 篩選好消息並推入全局列表
                if any(k in zh_title for k in GOOD_KEYWORDS) and is_today:
                    if not any(n['link'] == news_obj['link'] for n in MASTER_BRAIN["good_news"]):
                        MASTER_BRAIN["good_news"].insert(0, news_obj)
            
            cell["NewsList"] = articles
            MASTER_BRAIN["good_news"] = MASTER_BRAIN["good_news"][:12] # 保持 12 條
    except: pass

# --- 🔌 數據獲取 ---
def safe_get_tw_data(tv, symbol):
    try:
        return tv.get_hist(symbol=symbol, exchange='', interval=Interval.in_1_minute, n_bars=100, extended_session=True)
    except: return None

# --- 🧠 戰術雷達主引擎 ---
def scanner_engine():
    global cooldown_tracker
    tv = TvDatafeed(TW_USERNAME, TW_PASSWORD) if TW_USERNAME != 'guest' else TvDatafeed()
    
    while True:
        try:
            now_ts = time.time()
            current_leaderboard = []
            new_surge_list = []
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_ticker = {executor.submit(safe_get_tw_data, tv, ticker): ticker for ticker in WATCHLIST}
                for future in future_to_ticker:
                    ticker = future_to_ticker[future]
                    df = future.result()
                    if df is not None and not df.empty:
                        p = float(df['close'].iloc[-1])
                        o = float(df['open'].iloc[0]) 
                        
                        change_amt = p - o
                        change_pct = (change_amt / o) * 100
                        curr_vol = df['volume'].iloc[-1]
                        avg_vol_5m = df['volume'].iloc[-6:-1].mean()
                        rel_vol = round(curr_vol / avg_vol_5m, 2) if avg_vol_5m > 0 else 1.0
                        
                        # 訊號判定
                        is_ross_price = 1.0 <= p <= 300.0
                        is_spark = is_ross_price and rel_vol >= 2.2 and p >= df['high'].iloc[-16:-1].max()
                        is_diamond = is_ross_price and p > df['close'].tail(10).mean() and not is_spark
                        
                        status_color = "yellow" if is_spark else ("purple" if is_diamond else ("green" if change_amt > 0 else "red"))
                        tag = "🔥強力點火" if is_spark else ("💎支撐回踩" if is_diamond else "")
                        
                        mock_float = f"{random.uniform(2, 15):.1f}M"

                        cell = MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                        cell.update({
                            "Ticker": ticker, "PriceVal": p, "PriceStr": f"${p:.2f}", 
                            "ChangeAmt": f"{change_amt:+.2f}", "ChangePct": f"{change_pct:+.2f}%", 
                            "RelVol": rel_vol, "Volume": f"{curr_vol:,.0f}", 
                            "Float": mock_float, "Signal": tag, "StatusColor": status_color
                        })

                        current_leaderboard.append({"Code": ticker, "Price": p, "Pct": change_pct})

                        # 不論是否有訊號，都要放入實彈清單顯示目前的狀態
                        new_surge_list.append({
                            "Code": ticker, "Price": f"${p:.2f}", "Amt": f"{change_amt:+.2f}", "Pct": f"{change_pct:+.2f}%",
                            "Streak": tag, "RelVol": rel_vol, "Float": mock_float,
                            "StatusColor": status_color, "SignalTS": now_ts,
                            "AudioTrigger": "nova" if is_spark else "spark" if is_diamond else ""
                        })
                        
                        threading.Thread(target=fetch_news_bg, args=(ticker, cell)).start()
            
            MASTER_BRAIN["leaderboard"] = sorted(current_leaderboard, key=lambda x: x['Pct'], reverse=True)[:10]
            MASTER_BRAIN["surge"] = sorted(new_surge_list, key=lambda x: x['Code']) # 依代碼排序方便查看
            MASTER_BRAIN["last_update"] = datetime.now().strftime('%H:%M:%S')
            time.sleep(15)
        except: time.sleep(5)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def data(): return jsonify(MASTER_BRAIN)

if __name__ == '__main__':
    threading.Thread(target=scanner_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)