import sqlite3
import time
import json
import os
from datetime import datetime
import pytz

# 💡 定義台北時區[cite: 6]
tpe_tz = pytz.timezone('Asia/Taipei')

# 💡 資料庫檔案名稱[cite: 6]
DB_PATH = os.path.join(os.path.dirname(__file__), 'sniper_intelligence.db')

def init_db():
    """
    初始化戰地數據庫。若檔案與資料表不存在則自動建立。[cite: 6]
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 建立動能事件表 (Momentum Events)[cite: 6]
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
    
    # 啟動時順便執行資料庫減肥 (清理 7 天前舊資料)[cite: 6]
    cleanup_old_records()

def cleanup_old_records():
    """
    [新增功能：資料庫減肥] 
    僅保留最近 7 天的實戰數據，維持系統高速運作。[cite: 6]
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        # 計算 7 天前的時間戳[cite: 6]
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
    記錄一次新的動能爆發事件 (起漲點)。[cite: 6]
    [升級]：加入 50K 成交量紅線、漲幅 < 2.0% 過濾、以及情報垃圾桶防波堤。
    """
    # 🚫 物理紅線 1：拒絕紀錄超級大盤股的雜訊 (除非漲幅 > 5% 展現極端異動)[cite: 6]
    if float_m and float_m > 100.0 and change_percent < 5.0:
        return
        
    # 🚫 物理紅線 2：成交量低於 50K 不予紀錄 (流動性陷阱，直接捨棄)[cite: 6]
    if volume < 50000:
        return
        
    # 🚫 物理紅線 3：漲幅過小不予紀錄 (低迷震盪盤整)[cite: 6]
    if change_percent < 2.0:
        return

    # 🚫 物理紅線 4：情報垃圾防波堤 (資料庫層級攔截)
    # 確保即使 app.py 漏掉，資料庫也不會寫入這些馬後炮與法律廣告
    HARD_TRASH = ["WHY IT'S MOVING", "INVESTOR ALERT", "LAWSUIT", "CLASS ACTION", "INVESTIGATION", "DEADLINE", "TOP STOCK TO BUY", "HAGENS BERMAN", "POMERANTZ"]
    if headline and any(trash in headline.upper() for trash in HARD_TRASH):
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        
        now_sec = time.time()
        # 強制使用台北時間寫入[cite: 6]
        dt_str = datetime.now(tpe_tz).strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute('''
            INSERT INTO momentum_events 
            (timestamp_sec, date_time, ticker, headline, float_m, price_initial, price_max_15m, is_closed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (now_sec, dt_str, ticker, headline, float_m, price_initial, price_initial, 0)) #[cite: 6]
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Collector 寫入失敗: {e}")

def evaluate_sniper_tags(ticker, float_m, volume, rvol, change_percent, is_above_vwap=True):
    """
    [新增功能：UI 提醒標籤引擎][cite: 6]
    判斷該標的目前屬於哪個戰術狀態，供前端 index.html 渲染。[cite: 6]
    """
    # 1. 💀 陷阱區過濾 (最高優先級)[cite: 6]
    if volume < 50000 or (float_m and float_m > 100.0):
        return "💀 陷阱/無量"
        
    # 2. 🎯 狙擊點判定 (ROSS 級高勝率特徵)[cite: 6]
    # 條件：量比>2、Float<5M、漲幅>2%、且在VWAP之上[cite: 6]
    if rvol > 2.0 and (float_m and float_m < 5.0) and change_percent > 2.0 and is_above_vwap:
        return "🎯 建議狙擊"
        
    # 3. 🔥 發酵中判定 (標準動能)[cite: 6]
    if rvol > 1.0 and change_percent > 2.0:
        return "🔥 發酵中"
        
    # 4. 其他狀態[cite: 6]
    if float_m and float_m > 55.0:
        return "⚠️ 低動能區"
        
    return "⌛ 監測中"

def update_max_price(ticker, current_price):
    """
    盤中持續呼叫。檢查該股票在過去 15 分鐘內是否有記錄，[cite: 6]
    若有，且當前價格更高，則更新其 15 分鐘最高價 (price_max_15m)。[cite: 6]
    若超過 15 分鐘，則將該紀錄結案 (is_closed = 1)。[cite: 6]
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        
        now_sec = time.time()
        fifteen_mins_ago = now_sec - (15 * 60)
        
        # 1. 將超過 15 分鐘的紀錄結案[cite: 6]
        cursor.execute('''
            UPDATE momentum_events 
            SET is_closed = 1 
            WHERE is_closed = 0 AND timestamp_sec < ?
        ''', (fifteen_mins_ago,)) #[cite: 6]
        
        # 2. 找出該代碼目前仍在 15 分鐘監控期內的紀錄[cite: 6]
        cursor.execute('''
            SELECT id, price_max_15m FROM momentum_events 
            WHERE ticker = ? AND is_closed = 0
        ''', (ticker,)) #[cite: 6]
        
        active_records = cursor.fetchall()
        
        # 3. 更新最高價[cite: 6]
        for record_id, max_price in active_records:
            if current_price > max_price:
                cursor.execute('''
                    UPDATE momentum_events 
                    SET price_max_15m = ? 
                    WHERE id = ?
                ''', (current_price, record_id)) #[cite: 6]
                
        conn.commit()
        conn.close()
    except Exception as e:
        pass # 靜默錯誤，不干擾主程式掃描[cite: 6]

def export_to_json(output_filename="sniper_intelligence_export.json", limit=None, today_only=False):
    """
    將結案的紀錄匯出為 JSON。支援數量限制與當日過濾。[cite: 6]
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
            json.dump(data, f, ensure_ascii=False, indent=4) #[cite: 6]
            
        return export_path
        
    except Exception as e:
        print(f"⚠️ 匯出失敗: {e}")
        return None

def generate_intelligence_summary():
    """
    [新增功能]：後端運算摘要引擎。[cite: 6]
    直接在資料庫中統計並回傳前 50 大高勝率、高漲幅詞彙的 JSON 報表。[cite: 6]
    減輕 AI 分析負擔。[cite: 6]
    """
    import re
    from collections import Counter
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        # 只抓取過去 3 天內的結案資料，避免歷史包袱[cite: 6]
        three_days_ago = time.time() - (3 * 24 * 60 * 60)
        cursor.execute('''
            SELECT headline, price_initial, price_max_15m 
            FROM momentum_events 
            WHERE is_closed = 1 AND timestamp_sec > ?
        ''', (three_days_ago,)) #[cite: 6]
        
        rows = cursor.fetchall()
        conn.close()

        # 💡 V46 擴充 Stop Words：將廣告與馬後炮詞彙加入，避免干擾戰報結果
        stop_words = {'WITH', 'THAT', 'THIS', 'FROM', 'THEIR', 'WILL', 'HAVE', 'BEEN', 'WERE', 'AFTER', 'OVER', 'MORE', 'THAN', 'ABOUT', 'INC', 'CORP', 'LTD', 'THE', 'AND', 'FOR', 'WHY', 'MOVING', 'ALERT', 'INVESTOR', 'TODAY', 'STOCK', 'SHARES', 'ANNOUNCES', 'REPORTS'} #[cite: 6]
        stats = {}

        for row in rows:
            headline = re.sub(r'\[Echo\]|\[💎 核心情報\]', '', row['headline']).upper()
            words = re.findall(r'\b[A-Z]{3,}\b', headline)
            initial = row['price_initial']
            max_p = row['price_max_15m']
            gain = round(((max_p - initial) / initial) * 100, 2) if initial > 0 else 0.0
            
            # 提取三字組 Trigrams[cite: 6]
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
            if data['occurrences'] >= 2: # 至少出現過2次才具備統計意義[cite: 6]
                avg_gain = data['total_gain'] / data['occurrences']
                win_rate = data['win_count'] / data['occurrences']
                if avg_gain > 0:
                    summary.append({
                        "phrase": gram,
                        "avg_gain_pct": round(avg_gain, 2),
                        "win_rate": round(win_rate * 100, 2),
                        "occurrences": data['occurrences']
                    })
        
        # 依據平均漲幅排序，取前 50 名[cite: 6]
        summary = sorted(summary, key=lambda x: x['avg_gain_pct'], reverse=True)[:50] #[cite: 6]
        return {"status": "success", "data": summary}

    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == '__main__':
    init_db()