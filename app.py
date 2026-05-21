import os
import threading
import time
from datetime import datetime
from flask import Flask, render_template, jsonify, request

# 🚀 匯入 V59.0 中央時區設定與執行緒鎖
import shared_state
from config import TZ_NY, TZ_TW

# 🚀 匯入四大核心解耦 Worker（精準口徑對齊）
from engine_scanner import init_scanner  # 👈 核心盤前選股雷達
from engine_news import finnhub_news_monitor_worker, get_live_trends # 👈 情報監控網
from alpaca_worker import init_alpaca  # 👈 毫秒級火控報價
from memory_worker import init_worker  # 👈 GitHub 雲端備份記憶體

app = Flask(__name__)

# 全域啟動計數器，防範 Flask 在 Debug 模式下啟動兩次背景線程
_threads_initialized = False

def get_current_trading_date():
    """動態計算當前美股交易日（美東時間換算）"""
    now_ny = datetime.now(TZ_NY)
    # 如果是週末，自動倒退回週五
    if now_ny.weekday() == 5: # 週六
        now_ny = now_ny - timedelta(days=1)
    elif now_ny.weekday() == 6: # 週日
        now_ny = now_ny - timedelta(days=2)
    return now_ny.strftime('%Y-%m-%d')

@app.route('/')
def index():
    """戰術矩陣前端大廳入口"""
    return render_template('index.html')

@app.route('/data')
def get_data():
    """中央大腦即時數據廣播站（每秒廣播）"""
    with shared_state.brain_lock:
        payload = {
            "tv_status": shared_state.TV_LOGIN_STATUS,
            "last_update": shared_state.MASTER_BRAIN.get("last_update", "--:--:--"),
            "leaderboard": shared_state.MASTER_BRAIN.get("leaderboard", []),
            "surge_log": shared_state.MASTER_BRAIN.get("surge_log", []),
            "top_catalysts": shared_state.MASTER_BRAIN.get("top_catalysts", []),
            "details": shared_state.MASTER_BRAIN.get("details", {})
        }
    return jsonify(payload)

@app.route('/api/intelligence_summary')
def get_intelligence_summary():
    """高勝率戰報語料生成 API"""
    try:
        trends = get_live_trends()
        return jsonify({"status": "success", "data": trends})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/export_intelligence')
def export_intelligence():
    """即時下載 AI 語料庫"""
    with shared_state.brain_lock:
        data_str = json.dumps(shared_state.MASTER_BRAIN, indent=4, ensure_ascii=False)
    return app.response_class(
        data_str,
        mimetype='application/json',
        headers={"Content-Disposition": "attachment;filename=sniper_brain_dump.json"}
    )

def start_global_services():
    """安全啟動後端四大核心引擎線程（對齊 V59.0 架構）"""
    global _threads_initialized
    if _threads_initialized:
        return
    _threads_initialized = True

    print("⚡ [SYSTEM] 正在打通後端四象限數據引擎流水線...")
    
    # 1. 啟動 TradingView 全域盤前選股雷達 (強制無成交量阻攔放行版)
    init_scanner()
    
    # 2. 啟動 Finnhub/Yahoo 情報監控網
    threading.Thread(target=finnhub_news_monitor_worker, daemon=True).start()
    
    # 3. 啟動 Alpaca 毫秒級即時報價火控系統
    init_alpaca()
    
    # 4. 啟動 GitHub 雲端記憶備份中樞
    init_worker()
    
    print("✅ [SYSTEM] 後端四大核心解耦模組已成功並行運作！")

# 覆寫 Flask 啟動點
if __name__ == '__main__':
    # 啟動伺服器前，先發動四大底層 Worker
    start_global_services()
    
    # 讀取環境變數 Port（Railway 部署專用）
    port = int(os.getenv("PORT", 5000))
    
    # 強制關閉 debug 模式防範雙重啟動，並綁定 0.0.0.0
    app.run(host='0.0.0.0', port=port, debug=False)