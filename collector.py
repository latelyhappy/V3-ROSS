import sqlite3
import time
import json
import os
from datetime import datetime

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
    print("✅ Sniper Intelligence DB (collector) 初始化完成。")

def log_event(ticker, headline, float_m, price_initial):
    """
    記錄一次新的動能爆發事件 (起漲點)。
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        
        now_sec = time.time()
        dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
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

def export_to_json(output_filename="sniper_intelligence_export.json"):
    """
    將所有結案的紀錄匯出為 JSON 格式，供指揮官傳送給 AI 參謀進行大數據分析。
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        # 設定回傳格式為 dict 以利轉換 JSON
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        # 只匯出已結案（經過15分鐘驗證）的完整紀錄
        cursor.execute('SELECT * FROM momentum_events WHERE is_closed = 1 ORDER BY id DESC')
        rows = cursor.fetchall()
        
        data = []
        for row in rows:
            record = dict(row)
            # 計算 15 分鐘內的最終真實漲幅百分比
            initial = record['price_initial']
            max_p = record['price_max_15m']
            if initial > 0:
                record['max_gain_pct'] = round(((max_p - initial) / initial) * 100, 2)
            else:
                record['max_gain_pct'] = 0.0
            data.append(record)
            
        conn.close()
        
        # 寫入 JSON 檔案
        export_path = os.path.join(os.path.dirname(__file__), output_filename)
        with open(export_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            
        print(f"✅ 情報數據已成功匯出至: {output_filename}")
        return export_path
        
    except Exception as e:
        print(f"⚠️ 匯出失敗: {e}")
        return None

# 當直接執行此檔案時，會自動初始化資料庫
if __name__ == '__main__':
    init_db()