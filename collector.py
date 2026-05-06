import sqlite3
import time
import json
import os
from datetime import datetime
import pytz

# 💡 定義台北時區
tpe_tz = pytz.timezone('Asia/Taipei')

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
    print("✅ Sniper Intelligence DB (collector) 初始化完成 (時區：台北)。")

def log_event(ticker, headline, float_m, price_initial):
    """
    記錄一次新的動能爆發事件 (起漲點)。
    [新增]：過濾流通股 > 100M 的無效雜訊。
    """
    # 物理紅線：拒絕紀錄超級大盤股的雜訊
    if float_m and float_m > 100.0:
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        
        now_sec = time.time()
        # [修改]：強制使用台北時間寫入
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

def update_max_price(ticker, current_price):
    """
    盤中持續呼叫。檢查該股票在過去 15 分鐘內是否有記錄，
    若有，且當前價格更高，則更新其 15 分鐘最高價 (price_max_15m)。
    若超過 15 分鐘，則將該紀錄結案 (is_closed = 1)。
    """
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
        
        # 2. 找出該代碼目前仍在 15 分鐘監控期內的紀錄
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
        pass # 靜默錯誤，不干擾主程式掃描

def export_to_json(output_filename="sniper_intelligence_export.json", limit=None, today_only=False):
    """
    將結案的紀錄匯出為 JSON。支援數量限制與當日過濾。
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        query = 'SELECT * FROM momentum_events WHERE is_closed = 1'
        params = []

        if today_only:
            today_str = datetime.now(tpe_tz).strftime("%Y-%m-%d")
            query += ' AND date_time LIKE ?'
            params.append(f"{today_str}%")

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
    """
    [新增功能]：後端運算摘要引擎。
    直接在資料庫中統計並回傳前 50 大高勝率、高漲幅詞彙的 JSON 報表。
    減輕 AI 分析負擔。
    """
    import re
    from collections import Counter
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        # 只抓取過去 3 天內的結案資料，避免歷史包袱
        three_days_ago = time.time() - (3 * 24 * 60 * 60)
        cursor.execute('''
            SELECT headline, price_initial, price_max_15m 
            FROM momentum_events 
            WHERE is_closed = 1 AND timestamp_sec > ?
        ''', (three_days_ago,))
        
        rows = cursor.fetchall()
        conn.close()

        stop_words = {'WITH', 'THAT', 'THIS', 'FROM', 'THEIR', 'WILL', 'HAVE', 'BEEN', 'WERE', 'AFTER', 'OVER', 'MORE', 'THAN', 'ABOUT', 'INC', 'CORP', 'LTD', 'THE', 'AND', 'FOR'}
        stats = {}

        for row in rows:
            headline = re.sub(r'\[Echo\]', '', row['headline']).upper()
            words = re.findall(r'\b[A-Z]{3,}\b', headline)
            initial = row['price_initial']
            max_p = row['price_max_15m']
            gain = round(((max_p - initial) / initial) * 100, 2) if initial > 0 else 0.0
            
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

    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == '__main__':
    init_db()