import os
import sys
import warnings

os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")

class CleanStderr:
    def __init__(self, original_stderr):
        self.original_stderr = original_stderr
    def write(self, msg):
        if "Pandas4Warning" in msg or "Timestamp.utcnow" in msg or "FutureWarning" in msg or "deprecated" in msg: return
        self.original_stderr.write(msg)
    def flush(self):
        self.original_stderr.flush()

sys.stderr = CleanStderr(sys.stderr)

import logging
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
logging.getLogger('tvDatafeed').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

from flask import Flask, jsonify, render_template, request, send_file
import io, time, threading, json, copy
import pandas as pd
from datetime import datetime, timedelta

# ==========================================
# 匯入解耦模組
# ==========================================
from config import PORT, SESSION_BACKUP_PATH, TZ_NY
import shared_state
from engine_shares import load_shares_cache
from engine_scanner import scanner_engine
from engine_news import finnhub_news_monitor_worker, get_live_trends
import collector
import memory_worker
from alpaca_worker import init_alpaca

app = Flask(__name__)

def get_current_trading_date():
    now_ny = datetime.now(TZ_NY)
    return (now_ny - timedelta(days=1)).strftime("%Y-%m-%d") if now_ny.hour < 4 else now_ny.strftime("%Y-%m-%d")

def load_intraday_state():
    try:
        if os.path.exists(SESSION_BACKUP_PATH):
            with open(SESSION_BACKUP_PATH, 'r', encoding='utf-8') as f: data = json.load(f)
            if data.get("date") == get_current_trading_date():
                with shared_state.brain_lock:
                    shared_state.MASTER_BRAIN = data.get("MASTER_BRAIN", shared_state.MASTER_BRAIN)
                    shared_state.DYNAMIC_WATCHLIST = data.get("DYNAMIC_WATCHLIST", [])
                    shared_state.STATS_MAP = data.get("STATS_MAP", {})
    except: pass

def state_auto_save_worker():
    while True:
        time.sleep(15)
        try:
            with shared_state.brain_lock:
                backup_data = {
                    "date": get_current_trading_date(), 
                    "MASTER_BRAIN": shared_state.MASTER_BRAIN, 
                    "DYNAMIC_WATCHLIST": shared_state.DYNAMIC_WATCHLIST,
                    "STATS_MAP": shared_state.STATS_MAP
                }
            with open(SESSION_BACKUP_PATH, 'w', encoding='utf-8') as f: 
                json.dump(backup_data, f, ensure_ascii=False)
        except: pass

def daily_flush_worker():
    while True:
        time.sleep(10)
        new_session_date = get_current_trading_date()
        if new_session_date != shared_state._current_session_date:
            with shared_state.brain_lock:
                shared_state.MASTER_BRAIN["surge_log"] = []
                shared_state.MASTER_BRAIN["details"] = {}
                shared_state.MASTER_BRAIN["leaderboard"] = []
                shared_state.DYNAMIC_WATCHLIST.clear()
                shared_state.STATS_MAP.clear()
                shared_state._current_session_date = new_session_date

@app.route('/api/config', methods=['POST'])
def update_config():
    data = request.json
    if 'relvol_limit' in data:
        try: shared_state.MIN_RELVOL_LIMIT = float(data['relvol_limit'])
        except: pass
    shared_state._last_list_update = 0 
    return jsonify({"status": "success", "relvol_limit": shared_state.MIN_RELVOL_LIMIT})

@app.route('/api/export_news')
def export_news():
    rows = []
    with shared_state.brain_lock:
        for ticker_data in shared_state.MASTER_BRAIN.get('top_catalysts', []):
            ticker = ticker_data.get('Code') or ticker_data.get('ticker')
            for n in ticker_data.get('NewsList', []):
                rows.append({
                    "發布時間": n.get('time'), "代碼": ticker, "總分": ticker_data.get('CatScore', 0),
                    "標題": n.get('title'), "價格": ticker_data.get('Price', '-'), "漲幅": ticker_data.get('Pct', '-')
                })
    if not rows: return "無數據", 404
    df = pd.DataFrame(rows); output = io.BytesIO(); df.to_csv(output, index=False, encoding='utf-8-sig'); output.seek(0)
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name="news.csv")

@app.route('/api/export_intelligence')
def export_intelligence():
    limit = request.args.get('limit', default=None, type=int)
    today = request.args.get('today', default='false').lower() == 'true'
    export_path = collector.export_corpus_csv(limit=limit, today_only=today) 
    if export_path and os.path.exists(export_path):
        filename = f"sniper_corpus_{datetime.now().strftime('%m%d_%H%M')}.csv"
        return send_file(export_path, mimetype='text/csv', as_attachment=True, download_name=filename)
    return jsonify({"status": "error", "message": "無符合條件之資料，或資料庫為空"}), 404

@app.route('/api/intelligence_summary')
def intelligence_summary():
    result = collector.generate_intelligence_summary()
    return jsonify(result), (200 if result.get("status") == "success" else 500)

@app.route('/')
def index(): 
    return render_template('index.html')

@app.route('/data')
def get_data():
    """中央大腦即時數據廣播站"""
    with shared_state.brain_lock:
        # 強制打包
        payload = {
            "tv_status": shared_state.TV_LOGIN_STATUS,
            "last_update": shared_state.MASTER_BRAIN.get("last_update", "--:--:--"),
            "leaderboard": shared_state.MASTER_BRAIN.get("leaderboard", []),
            "surge_log": shared_state.MASTER_BRAIN.get("surge_log", []),
            "top_catalysts": shared_state.MASTER_BRAIN.get("top_catalysts", []),
            "details": shared_state.MASTER_BRAIN.get("details", {})
        }
    return jsonify(payload)

if __name__ == '__main__':
    # 啟動附屬資料庫與記憶體 (加入防護罩)
    try: collector.init_db()
    except: pass
    
    load_shares_cache()
    load_intraday_state()
    
    try: memory_worker.init_worker()
    except: pass
    
    # 啟動四大核心執行緒
    threading.Thread(target=state_auto_save_worker, daemon=True).start()
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=finnhub_news_monitor_worker, daemon=True).start()
    threading.Thread(target=daily_flush_worker, daemon=True).start()
    
    # 🚀 V59.0 呼叫更新：直接啟動，不需要再塞入變數參數
    init_alpaca()
    
    app.run(host='0.0.0.0', port=PORT, use_reloader=False)