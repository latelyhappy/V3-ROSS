import sqlite3
import time
import json
import os
from datetime import datetime, timedelta
import pytz

# 💡 定義時區
tpe_tz = pytz.timezone('Asia/Taipei')
ny_tz = pytz.timezone('America/New_York') # 💡 導入美東時區

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
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        seven_days_ago = time.time() - (7 * 24 * 60 * 60)
        
        cursor.execute('''
            DELETE FROM momentum_events 
            WHERE timestamp_sec < ?
        ''', (seven_days_ago,))
        
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted_count > 0:
            print(f"🧹 資料庫清理：已刪除 {deleted_count} 筆 7 天前的過期戰報。")
    except Exception as e:
        print(f"⚠️ 清理舊資料失敗: {e}")

def log_event(ticker, headline, float_m, price_initial, volume=0, change_percent=0.0):
    """
    記錄一次新的動能爆發事件 (起漲點)。
    """
    # 🚫 物理紅線 1：拒絕紀錄超級大盤股的雜訊 (除非漲幅 > 5% 展現極端異動)
    if float_m and float_m > 100.0 and change_percent < 5.0:
        return
        
    # 🚫 物理紅線 2：成交量低於 5K 不予紀錄 (配合最新解封參數)
    if volume < 5000:
        return
        
    # 🚫 物理紅線 3：漲幅過小不予紀錄 (低迷震盪盤整)
    if change_percent < 2.0:
        return

    # 🚫 物理紅線 4：情報垃圾防波堤
    HARD_TRASH = ["WHY IT'S MOVING", "INVESTOR ALERT", "LAWSUIT", "CLASS ACTION", "INVESTIGATION", "DEADLINE", "TOP STOCK TO BUY", "HAGENS BERMAN", "POMERANTZ"]
    if headline and any(trash in headline.upper() for trash in HARD_TRASH):
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        
        now_sec = time.time()
        dt_str = datetime.now(tpe_tz).strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute('''
            INSERT INTO momentum_events 
            (timestamp_sec, date_time, ticker, headline, float_m, price_initial, price_max_15m, is_closed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (now_sec, dt_str, ticker, headline, float_m, price_initial, price_initial, 0))
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Collector 寫入失敗: {e}")

def evaluate_sniper_tags(ticker, float_m, volume, rvol, change_percent, is_above_vwap=True):
    """前端狀態標籤引擎"""
    # 1. 💀 陷阱區過濾 (門檻降為 5000 股)
    if volume < 5000 or (float_m and float_m > 100.0):
        return "💀 陷阱/無量"
        
    # 2. 🎯 狙擊點判定
    if rvol > 2.0 and (float_m and float_m < 5.0) and change_percent > 2.0 and is_above_vwap:
        return "🎯 建議狙擊"
        
    # 3. 🔥 發酵中判定
    if rvol > 1.0 and change_percent > 2.0:
        return "🔥 發酵中"
        
    # 4. 其他狀態
    if float_m and float_m > 55.0:
        return "⚠️ 低動能區"
        
    return "⌛ 監測中"

def update_max_price(ticker, current_price):
    """更新 15 分鐘內的最高價"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        
        now_sec = time.time()
        fifteen_mins_ago = now_sec - (15 * 60)
        
        # 1. 將超過 15 分鐘的紀錄結案
        cursor.execute('''
            UPDATE momentum_events 
            SET is_closed = 1 
            WHERE is_closed = 0 AND timestamp_sec < ?
        ''', (fifteen_mins_ago,))
        
        # 2. 找出該代碼目前仍在監控期內的紀錄
        cursor.execute('''
            SELECT id, price_max_15m FROM momentum_events 
            WHERE ticker = ? AND is_closed = 0
        ''', (ticker,))
        
        active_records = cursor.fetchall()
        
        # 3. 更新最高價
        for record_id, max_price in active_records:
            if current_price > max_price:
                cursor.execute('''
                    UPDATE momentum_events 
                    SET price_max_15m = ? 
                    WHERE id = ?
                ''', (current_price, record_id))
                
        conn.commit()
        conn.close()
    except Exception as e:
        pass 

def export_to_json(output_filename="sniper_intelligence_export.json", limit=None, today_only=False):
    """
    將結案的紀錄匯出為 JSON。
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        query = 'SELECT * FROM momentum_events WHERE is_closed = 1'
        params = []

        if today_only:
            # 💡 核心修正：使用紐約時間 04:00 AM 作為今日切割點
            now_ny = datetime.now(ny_tz)
            if now_ny.hour < 4:
                session_start_ny = now_ny.replace(hour=4, minute=0, second=0, microsecond=0) - timedelta(days=1)
            else:
                session_start_ny = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
            
            session_start_ts = session_start_ny.timestamp()
            
            query += ' AND timestamp_sec >= ?'
            params.append(session_start_ts)

        query += ' ORDER BY id DESC'

        if limit:
            query += ' LIMIT ?'
            params.append(limit)
            
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        data = []
        for row in rows:
            record = dict(row)
            initial = record['price_initial']
            max_p = record['price_max_15m']
            record['max_gain_pct'] = round(((max_p - initial) / initial) * 100, 2) if initial > 0 else 0.0
            data.append(record)
            
        conn.close()
        
        export_path = os.path.join(os.path.dirname(__file__), output_filename)
        with open(export_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            
        return export_path
        
    except Exception as e:
        print(f"⚠️ 匯出失敗: {e}")
        return None

def generate_intelligence_summary():
    import re
    from collections import Counter
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        three_days_ago = time.time() - (3 * 24 * 60 * 60)
        cursor.execute('''
            SELECT headline, price_initial, price_max_15m 
            FROM momentum_events 
            WHERE is_closed = 1 AND timestamp_sec > ?
        ''', (three_days_ago,))
        
        rows = cursor.fetchall()
        conn.close()

        stop_words = {'WITH', 'THAT', 'THIS', 'FROM', 'THEIR', 'WILL', 'HAVE', 'BEEN', 'WERE', 'AFTER', 'OVER', 'MORE', 'THAN', 'ABOUT', 'INC', 'CORP', 'LTD', 'THE', 'AND', 'FOR', 'WHY', 'MOVING', 'ALERT', 'INVESTOR', 'TODAY', 'STOCK', 'SHARES', 'ANNOUNCES', 'REPORTS'}
        stats = {}

        for row in rows:
            headline = re.sub(r'\[Echo\]|\[💎 核心情報\]', '', row['headline']).upper()
            words = re.findall(r'\b[A-Z]{3,}\b', headline)
            initial = row['price_initial']
            max_p = row['price_max_15m']
            gain = round(((max_p - initial) / initial) * 100, 2) if initial > 0 else 0.0
            
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
            if data['occurrences'] >= 2: 
                avg_gain = data['total_gain'] / data['occurrences']
                win_rate = data['win_count'] / data['occurrences']
                if avg_gain > 0:
                    summary.append({
                        "phrase": gram,
                        "avg_gain_pct": round(avg_gain, 2),
                        "win_rate": round(win_rate * 100, 2),
                        "occurrences": data['occurrences']
                    })
        
        summary = sorted(summary, key=lambda x: x['avg_gain_pct'], reverse=True)[:50]
        return {"status": "success", "data": summary}

    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == '__main__':
    init_db()