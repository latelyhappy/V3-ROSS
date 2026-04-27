import time
import threading
import json
import os
import requests
import pandas as pd
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO
from tvDatafeed import TvDatafeed, Interval
import concurrent.futures  # V44.0 新增：異步併發處理套件

# 跨平台音效處理 (保留 winsound 呼叫，並更新檔案名)
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# ==========================================
# 1. 系統初始化與設定 (維持原樣)
# ==========================================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'sniper_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")

# 初始化 tvDatafeed
try:
    tv = TvDatafeed()
    print("tvDatafeed 初始化成功")
except Exception as e:
    print(f"tvDatafeed 初始化失敗: {e}")

# Finnhub API Key (從環境變數呼叫)
FINNHUB_TOKEN = os.environ.get("FINNHUB_TOKEN")

# 全局變數
scanner_running = False
scan_results = []
news_results = []

# 觀察名單 (維持您的設定)
DYNAMIC_WATCHLIST = [
    'OGN', 'TRT', 'TIVC', 'GME', 'AMC', 'TSLA', 'NVDA', 'AAPL',
    'FLYYQ', 'IHRT', 'MIGI', 'CAST', 'LIDR', 'ATOM', 'POET', 'AKAN'
]

# ==========================================
# 2. 輔助函式 (維持原樣)
# ==========================================
def get_real_float_finnhub(symbol):
    """盤前靜態 Float 鎖定與快取機制"""
    cache_file = "float_cache.json"
    float_cache = {}
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            float_cache = json.load(f)
            
    if symbol in float_cache:
        return float_cache[symbol]
        
    try:
        url = f"https://finnhub.io/api/v1/stock/profile2?symbol={symbol}&token={FINNHUB_TOKEN}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # shareOutstanding 為發行股數(百萬)
            real_float = data.get('shareOutstanding', 0) * 1e6 
            if real_float > 0:
                float_cache[symbol] = real_float
                with open(cache_file, 'w') as f:
                    json.dump(float_cache, f)
                return real_float
    except Exception as e:
        print(f"Finnhub Float 抓取失敗 {symbol}: {e}")
    return 0

# ==========================================
# 3. V44.0 核心異步掃描引擎
# ==========================================
def scanner_engine():
    global scanner_running, scan_results, tv, news_results

    def process_symbol(symbol):
        try:
            # 判斷主要交易所
            exchange = 'NASDAQ'
            if symbol in ['GME', 'AMC', 'OGN', 'TRT', 'RDDT', 'DJT', 'FLYYQ']:
                exchange = 'NYSE'
                
            # --- 1. 抓取 1Min K 線用於指標與當前價 ---
            # 抓 60 根確保 EMA52 穩定
            hist_df = tv.get_hist(symbol, exchange, interval=Interval.in_1_minute, n_bars=60)
            if hist_df is None or hist_df.empty:
                # 備用交易所嘗試
                exchange = 'NYSE' if exchange == 'NASDAQ' else 'NASDAQ'
                hist_df = tv.get_hist(symbol, exchange, interval=Interval.in_1_minute, n_bars=60)
            
            if hist_df is None or hist_df.empty:
                return None

            # 計算 EMA9, EMA52, VWAP
            hist_df['EMA9'] = hist_df['close'].ewm(span=9, adjust=False).mean()
            hist_df['EMA52'] = hist_df['close'].ewm(span=52, adjust=False).mean()
            typical_price = (hist_df['high'] + hist_df['low'] + hist_df['close']) / 3
            hist_df['VWAP'] = (typical_price * hist_df['volume']).cumsum() / hist_df['volume'].cumsum()
            
            p_live = float(hist_df['close'].iloc[-1])
            vwap_live = float(hist_df['VWAP'].iloc[-1])
            
            # --- 2. 抓取 Daily K 線取得官方成交總量與昨收 ---
            daily_df = tv.get_hist(symbol, exchange, interval=Interval.in_daily, n_bars=2)
            if daily_df is not None and not daily_df.empty:
                prev_close = float(daily_df['close'].iloc[-2]) if len(daily_df) >= 2 else float(daily_df['open'].iloc[0])
                true_total_vol = float(daily_df['volume'].iloc[-1]) # 官方當日累積總量
            else:
                prev_close = float(hist_df['open'].iloc[0])
                true_total_vol = float(hist_df['volume'].sum())

            pct_change = ((p_live - prev_close) / prev_close) * 100 if prev_close else 0

            # --- 3. 籌碼面計算 (HP%) ---
            real_float = get_real_float_finnhub(symbol)
            if real_float > 0:
                hp_ratio = (true_total_vol / real_float) * 100
                float_str = f"{real_float/1e6:.1f}M"
            else:
                hp_ratio = 0.0
                float_str = "N/A"

            # --- 4. 量能梳子物理判斷 ---
            recent_vol = hist_df['volume'].tail(10)
            v_max = recent_vol.max()
            comb_msg = "量能不均"
            if v_max > 0:
                teeth = recent_vol[recent_vol < (v_max * 0.05)]
                if len(teeth) > 0:
                    comb_msg = "⚠️流動性斷層"
                elif recent_vol.mean() > (v_max * 0.25):
                    comb_msg = "🪮健康梳子"

            # --- 5. 突破條件判斷 ---
            cat_score = 0
            breakout_status = "⏳等待確認"
            news_data = next((item for item in news_results if item['代碼'] == symbol), None)
            
            if news_data:
                cat_score = news_data.get('該股總分(CatScore)', 0)
                try:
                    p_news = float(news_data.get('新聞當刻高點', 0))
                    if p_news > 0:
                        # 核心邏輯：實體突破新聞高點 且 站穩 VWAP
                        if p_live > p_news and p_live > vwap_live:
                            breakout_status = "✅ 雙重突破"
                        elif p_live > p_news:
                            breakout_status = "✅ 實體突破"
                        else:
                            breakout_status = "⏳ 尚未突破"
                except:
                    pass

            return {
                '代碼': symbol,
                '當前價格': round(p_live, 3),
                '漲幅%': round(pct_change, 2),
                '成交量': true_total_vol,
                'HP%': round(hp_ratio, 2),
                'Float': float_str,
                'Comb': comb_msg,
                'Breakout': breakout_status,
                '總分': cat_score,
                'VWAP': round(vwap_live, 3),
                'EMA9': round(float(hist_df['EMA9'].iloc[-1]), 3)
            }
        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            return None

    print("V44.0 併發引擎啟動...")
    while scanner_running:
        start_time = time.time()
        
        # 使用 ThreadPoolExecutor 同時抓取多檔數據
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(process_symbol, DYNAMIC_WATCHLIST))
            
        updated_results = [r for r in results if r is not None]
        scan_results = updated_results

        # 警報判斷 (修正音效檔名為 nova.mp3)
        for r in scan_results:
            if r['總分'] >= 50 and r['HP%'] >= 5 and "突破" in r['Breakout']:
                if HAS_WINSOUND:
                    try:
                        # 注意：winsound 播放 mp3 可能視系統解碼器而定，若失敗建議改回 .wav
                        winsound.PlaySound("nova.mp3", winsound.SND_ASYNC)
                        print(f"🚨 警報發射: {r['代碼']}")
                    except Exception as e:
                        print(f"音效播放錯誤: {e}")
                else:
                    print(f"🔔 觸發條件: {r['代碼']} (Linux 環境無音效)")

        # 透過 WebSocket 推送到 index.html
        socketio.emit('update_data', scan_results)
        
        # 維持 5 秒刷新節奏
        elapsed = time.time() - start_time
        sleep_time = max(1.0, 5.0 - elapsed)
        time.sleep(sleep_time)

# ==========================================
# 4. 新聞抓取迴圈 (原封不動)
# ==========================================
def fetch_news_loop():
    global news_results
    while True:
        try:
            # 這裡應銜接您的新聞處理邏輯，保持原本的運行頻率
            time.sleep(60)
        except Exception as e:
            print(f"新聞抓取異常: {e}")
            time.sleep(60)

# ==========================================
# 5. Flask 路由 (原封不動)
# ==========================================
@app.route('/')
def index():
    try:
        # 直接讀取您的 index.txt 內容
        with open('index.txt', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return render_template_string(html_content)
    except Exception as e:
        return f"讀取 index.txt 失敗: {e}"

@app.route('/start', methods=['POST'])
def start_scanner():
    global scanner_running
    if not scanner_running:
        scanner_running = True
        threading.Thread(target=scanner_engine, daemon=True).start()
        return jsonify({"status": "started"})
    return jsonify({"status": "already running"})

@app.route('/stop', methods=['POST'])
def stop_scanner():
    global scanner_running
    scanner_running = False
    return jsonify({"status": "stopped"})

@app.route('/api/news', methods=['GET'])
def get_news():
    global news_results
    return jsonify(news_results)

# ==========================================
# 6. 啟動入口
# ==========================================
if __name__ == '__main__':
    # 背景啟動新聞監控
    threading.Thread(target=fetch_news_loop, daemon=True).start()
    
    # 啟動主伺服器
    print("Sniper V44.0 準備就緒...")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
