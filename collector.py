import sqlite3
import time
import json
import os
from datetime import datetime, timedelta
import pytz

# 💡 定義時區
tpe_tz = pytz.timezone('Asia/Taipei')
ny_tz = pytz.timezone('America/New_York')

# 💡 資料庫檔案名稱
DB_PATH = os.path.join(os.path.dirname(__file__), 'sniper_intelligence.db')

def init_db():
    """
    初始化戰地數據庫。若檔案與資料表不存在則自動建立。
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 建立動能事件表 (Momentum Events)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS momentum_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_sec REAL,
            date_time TEXT,
            ticker TEXT,
            headline TEXT,
            float_m REAL,
            price_initial REAL,
            price_max_15m REAL,
            is_closed INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()
    print("✅ Sniper Intelligence DB (collector) 初始化完成 (跨時區支援)。")
    
    # 啟動時順便執行資料庫減肥 (清理 7 天前舊資料)
    cleanup_old_records()

def cleanup_old_records():
    """清理超過 7 天的舊數據，避免 DB 過於肥大"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    seven_days_ago_ts = time.time() - (7 * 24 * 3600)
    cursor.execute("DELETE FROM momentum_events WHERE timestamp_sec < ?", (seven_days_ago_ts,))
    conn.commit()
    conn.close()

def log_event(ticker, headline, float_m, price_initial, volume, change_percent):
    """
    當偵測到有效情報與動能爆發時，寫入 DB。
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    now_ts = time.time()
    now_dt_str = datetime.now(tpe_tz).strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute('''
        INSERT INTO momentum_events 
        (timestamp_sec, date_time, ticker, headline, float_m, price_initial, price_max_15m, is_closed)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    ''', (now_ts, now_dt_str, ticker, headline, float_m, price_initial, price_initial))
    
    conn.commit()
    conn.close()

def update_max_price(ticker, current_price):
    """
    在股價更新時呼叫，更新過去 15 分鐘內觸發之事件的最高價。
    超過 15 分鐘的事件則標記為已結算 (is_closed=1)。
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    now_ts = time.time()
    fifteen_mins_ago = now_ts - 900
    
    # 更新尚未結算，且在 15 分鐘內的事件最高價
    cursor.execute('''
        UPDATE momentum_events 
        SET price_max_15m = MAX(price_max_15m, ?)
        WHERE ticker = ? AND is_closed = 0 AND timestamp_sec >= ?
    ''', (current_price, ticker, fifteen_mins_ago))
    
    # 結算超過 15 分鐘的事件
    cursor.execute('''
        UPDATE momentum_events 
        SET is_closed = 1
        WHERE is_closed = 0 AND timestamp_sec < ?
    ''', (fifteen_mins_ago,))
    
    conn.commit()
    conn.close()

def export_to_json(limit=None, today_only=False):
    """
    將 DB 數據匯出為 JSON。
    支援 `today_only` (僅限美股當前交易日) 以及 `limit` 筆數限制。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM momentum_events"
    params = ()
    
    # 💡 核心修正：使用紐約時間 04:00 AM 作為今日切割點，取代原本的台北時間切換
    if today_only:
        now_ny = datetime.now(ny_tz)
        if now_ny.hour < 4:
            session_start_ny = now_ny.replace(hour=4, minute=0, second=0, microsecond=0) - timedelta(days=1)
        else:
            session_start_ny = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
        
        session_start_ts = session_start_ny.timestamp()
        
        query += " WHERE timestamp_sec >= ?"
        params = (session_start_ts,)
        
    query += " ORDER BY timestamp_sec DESC"
    
    if limit is not None:
        query += f" LIMIT {limit}"
        
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    result_list = []
    for row in rows:
        data = dict(row)
        # 計算最大漲幅 %
        p_init = data['price_initial']
        p_max = data['price_max_15m']
        gain_pct = round(((p_max - p_init) / p_init) * 100, 2) if p_init and p_init > 0 else 0.0
        data['max_gain_pct'] = gain_pct
        result_list.append(data)
        
    if not result_list:
        return None
        
    export_filename = "sniper_intel_export.json"
    export_path = os.path.join(os.path.dirname(__file__), export_filename)
    
    with open(export_path, 'w', encoding='utf-8') as f:
        json.dump(result_list, f, ensure_ascii=False, indent=4)
        
    return export_path

def evaluate_sniper_tags(ticker, float_m, daily_vol, rel_vol, real_pct, price_above_vwap):
    """前線評估雷達標籤"""
    if daily_vol < 50000:
        return ["💀 無量陷阱"]
        
    tags = []
    if float_m < 20.0:
        tags.append("🐎 輕量級")
    elif float_m > 100.0:
        tags.append("🐢 重型車")
        
    if rel_vol > 3.0:
        tags.append("🔥 爆量")
        
    if real_pct > 10.0 and price_above_vwap:
        tags.append("🚀 強勢多頭")
        
    if rel_vol > 2.0 and float_m < 50.0 and price_above_vwap:
        tags.append("🎯 適合狙擊")
        
    return tags

def generate_intelligence_summary():
    """
    分析歷史情報，計算哪些關鍵字組合 (N-Grams) 帶來的平均漲幅最高。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 僅分析已結算且有漲幅數據的事件
    cursor.execute('''
        SELECT headline, price_initial, price_max_15m 
        FROM momentum_events 
        WHERE is_closed = 1 AND price_max_15m IS NOT NULL
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return {"status": "error", "message": "資料庫資料不足以生成戰報。"}
        
    stats = {}
    stop_words = {"THE", "A", "AN", "IN", "ON", "FOR", "OF", "TO", "AND", "WITH", "AT", "BY", "FROM", "IS", "ARE"}
    
    import re
    for row in rows:
        hl = row['headline']
        p_init = row['price_initial']
        p_max = row['price_max_15m']
        
        # 清除 ECHO 標籤與特珠符號，轉大寫
        clean_hl = re.sub(r'\[.*?\]', '', hl)
        clean_hl = re.sub(r'[^A-Za-z0-9\s]', '', clean_hl).upper()
        words = clean_hl.split()
        
        gain = round(((p_max - p_init) / p_init) * 100, 2) if p_init > 0 else 0.0
        
        # 提取三字組 Trigrams
        ngrams = [" ".join(words[i:i+3]) for i in range(len(words)-2)]
        for gram in ngrams:
            if any(word in stop_words for word in gram.split()): continue
            if gram not in stats:
                stats[gram] = {'total_gain': 0.0, 'win_count': 0, 'occurrences': 0}
            
            stats[gram]['occurrences'] += 1
            stats[gram]['total_gain'] += gain
            if gain > 1.0:
                stats[gram]['win_count'] += 1

    summary = []
    for gram, data in stats.items():
        if data['occurrences'] >= 2: # 至少出現過2次才具備統計意義
            avg_gain = data['total_gain'] / data['occurrences']
            win_rate = data['win_count'] / data['occurrences']
            if avg_gain > 0:
                summary.append({
                    "phrase": gram,
                    "avg_gain_pct": round(avg_gain, 2),
                    "win_rate": round(win_rate * 100, 2),
                    "occurrences": data['occurrences']
                })
    
    # 依據平均漲幅排序，取前 50 名
    summary = sorted(summary, key=lambda x: x['avg_gain_pct'], reverse=True)[:50]
    
    return {"status": "success", "data": summary}