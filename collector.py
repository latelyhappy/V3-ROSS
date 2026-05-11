import sqlite3
import time
import json
import os
import csv
import re
from datetime import datetime, timedelta
import pytz

# 💡 定義時區
tpe_tz = pytz.timezone('Asia/Taipei')
ny_tz = pytz.timezone('America/New_York')

# 💡 檔案路徑
DB_PATH = os.path.join(os.path.dirname(__file__), 'sniper_intelligence.db')
CATALYST_PATH = os.path.join(os.path.dirname(__file__), 'catalysts.json')

def init_db():
    """初始化戰地數據庫。"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
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
    print("✅ Sniper Intelligence DB (NLP 進化版) 初始化完成。")
    
    cleanup_old_records()
    # 💡 啟動時執行一次 NLP 自動進化
    auto_evolve_nlp()

def cleanup_old_records():
    """清理超過 7 天的舊數據，避免 DB 過於肥大"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        seven_days_ago = time.time() - (7 * 24 * 60 * 60)
        
        cursor.execute('DELETE FROM momentum_events WHERE timestamp_sec < ?', (seven_days_ago,))
        
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted_count > 0:
            print(f"🧹 資料庫清理：已刪除 {deleted_count} 筆過期戰報。")
    except Exception as e:
        print(f"⚠️ 清理舊資料失敗: {e}")

def log_event(ticker, headline, float_m, price_initial, volume=0, change_percent=0.0):
    """記錄一次新的動能爆發事件 (起漲點)。"""
    if float_m and float_m > 100.0 and change_percent < 5.0: return
    if volume < 5000: return
    if change_percent < 2.0: return

    HARD_TRASH = ["WHY IT'S MOVING", "INVESTOR ALERT", "LAWSUIT", "CLASS ACTION", "INVESTIGATION", "DEADLINE", "TOP STOCK TO BUY", "HAGENS BERMAN", "POMERANTZ"]
    if headline and any(trash in headline.upper() for trash in HARD_TRASH): return

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
        pass

def evaluate_sniper_tags(ticker, float_m, volume, rvol, change_percent, is_above_vwap=True):
    """前端狀態標籤引擎"""
    if volume < 5000 or (float_m and float_m > 100.0): return "💀 陷阱/無量"
    if rvol > 2.0 and (float_m and float_m < 5.0) and change_percent > 2.0 and is_above_vwap: return "🎯 建議狙擊"
    if rvol > 1.0 and change_percent > 2.0: return "🔥 發酵中"
    if float_m and float_m > 55.0: return "⚠️ 低動能區"
    return "⌛ 監測中"

def update_max_price(ticker, current_price):
    """更新 15 分鐘內的最高價"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        
        now_sec = time.time()
        fifteen_mins_ago = now_sec - (15 * 60)
        
        cursor.execute('UPDATE momentum_events SET is_closed = 1 WHERE is_closed = 0 AND timestamp_sec < ?', (fifteen_mins_ago,))
        cursor.execute('SELECT id, price_max_15m FROM momentum_events WHERE ticker = ? AND is_closed = 0', (ticker,))
        
        active_records = cursor.fetchall()
        for record_id, max_price in active_records:
            if current_price > max_price:
                cursor.execute('UPDATE momentum_events SET price_max_15m = ? WHERE id = ?', (current_price, record_id))
                
        conn.commit()
        conn.close()
    except: pass 

# 💡 新增：輸出人類與 AI 皆可讀的 CSV 語料庫
def export_corpus_csv(output_filename="sniper_corpus_export.csv", limit=None, today_only=False):
    """將戰報輸出為 NLP 校正專用的 CSV 檔案"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        query = 'SELECT * FROM momentum_events WHERE is_closed = 1'
        params = []

        if today_only:
            now_ny = datetime.now(ny_tz)
            session_start_ny = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
            if now_ny.hour < 4: session_start_ny -= timedelta(days=1)
            query += ' AND timestamp_sec >= ?'
            params.append(session_start_ny.timestamp())

        query += ' ORDER BY id DESC'
        if limit:
            query += ' LIMIT ?'
            params.append(limit)
            
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        export_path = os.path.join(os.path.dirname(__file__), output_filename)
        with open(export_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['紀錄時間', '代碼', '流通股(M)', '情報標題', '起漲價', '最高價', '最高漲幅(%)'])
            for row in rows:
                record = dict(row)
                initial = record['price_initial']
                max_p = record['price_max_15m']
                gain_pct = round(((max_p - initial) / initial) * 100, 2) if initial > 0 else 0.0
                writer.writerow([
                    record['date_time'], record['ticker'], record['float_m'],
                    record['headline'], initial, max_p, gain_pct
                ])
                
        return export_path
    except Exception as e:
        print(f"⚠️ CSV 匯出失敗: {e}")
        return None

# 💡 保留 JSON 匯出相容原有的 app.py
def export_to_json(output_filename="sniper_intelligence_export.json", limit=None, today_only=False):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        query = 'SELECT * FROM momentum_events WHERE is_closed = 1'
        params = []
        if today_only:
            now_ny = datetime.now(ny_tz)
            session_start_ny = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
            if now_ny.hour < 4: session_start_ny -= timedelta(days=1)
            query += ' AND timestamp_sec >= ?'
            params.append(session_start_ny.timestamp())
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
            record['max_gain_pct'] = round(((record['price_max_15m'] - initial) / initial) * 100, 2) if initial > 0 else 0.0
            data.append(record)
        conn.close()
        export_path = os.path.join(os.path.dirname(__file__), output_filename)
        with open(export_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return export_path
    except: return None

def get_nlp_stats(days_back=7):
    """核心語意統計引擎 (提取 1-gram 與 2-gram)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        target_time = time.time() - (days_back * 24 * 60 * 60)
        cursor.execute('SELECT headline, price_initial, price_max_15m FROM momentum_events WHERE is_closed = 1 AND timestamp_sec > ?', (target_time,))
        rows = cursor.fetchall()
        conn.close()

        stop_words = {'WITH', 'THAT', 'THIS', 'FROM', 'THEIR', 'WILL', 'HAVE', 'BEEN', 'WERE', 'AFTER', 'OVER', 'MORE', 'THAN', 'ABOUT', 'INC', 'CORP', 'LTD', 'THE', 'AND', 'FOR', 'WHY', 'MOVING', 'ALERT', 'INVESTOR', 'TODAY', 'STOCK', 'SHARES', 'ANNOUNCES', 'REPORTS', 'PR', 'NEWSWIRE', 'GLOBE', 'GLOBENEWSWIRE', 'COMPANY'}
        stats = {}

        for row in rows:
            if not row['headline']: continue
            headline = re.sub(r'\[Echo\]|\[💎 核心情報\]', '', row['headline']).upper()
            words = re.findall(r'\b[A-Z]{3,}\b', headline)
            initial = row['price_initial']
            max_p = row['price_max_15m']
            gain = round(((max_p - initial) / initial) * 100, 2) if initial > 0 else 0.0
            
            # 💡 提取 1-Gram (單字) 與 2-Gram (雙字組)
            ngrams = words.copy()
            ngrams += [" ".join(words[i:i+2]) for i in range(len(words)-1)]
            
            for gram in ngrams:
                if any(word in stop_words for word in gram.split()): continue
                if gram not in stats:
                    stats[gram] = {'total_gain': 0.0, 'win_count': 0, 'occurrences': 0}
                
                stats[gram]['occurrences'] += 1
                stats[gram]['total_gain'] += gain
                if gain > 5.0: # 💡 勝率標準：漲幅大於 5% 才算有效攻擊
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
        
        return sorted(summary, key=lambda x: x['win_rate'] * x['avg_gain_pct'], reverse=True)
    except: return []

def generate_intelligence_summary():
    """提供給前端儀表板的摘要 API"""
    summary = get_nlp_stats(days_back=3)
    if summary:
        return {"status": "success", "data": summary[:50]}
    return {"status": "error", "message": "無數據"}

# 💡 終極武器：自動進化機制 (覆寫軍火庫)
def auto_evolve_nlp():
    """自動將高勝率詞彙寫入 catalysts.json 的 THEMATIC_TRENDS 中"""
    print("🧠 啟動 NLP 自動進化引擎...")
    stats = get_nlp_stats(days_back=7)
    
    # 篩選標準：出現 3 次以上、勝率 > 70%、平均漲幅 > 5%
    elite_phrases = {item['phrase']: 8 for item in stats if item['occurrences'] >= 3 and item['win_rate'] >= 70.0 and item['avg_gain_pct'] >= 5.0}
    
    if not elite_phrases:
        print("   -> 今日無新增高勝率詞彙。")
        return

    try:
        # 讀取現有軍火庫
        if os.path.exists(CATALYST_PATH):
            with open(CATALYST_PATH, 'r', encoding='utf-8') as f:
                armory = json.load(f)
        else:
            armory = {}
            
        if "THEMATIC_TRENDS" not in armory:
            armory["THEMATIC_TRENDS"] = {}
            
        # 寫入新發現的詞彙 (不覆蓋人工設定的更高分數)
        added_count = 0
        for phrase, score in elite_phrases.items():
            if phrase not in armory["THEMATIC_TRENDS"]:
                armory["THEMATIC_TRENDS"][phrase] = score
                added_count += 1
                
        if added_count > 0:
            with open(CATALYST_PATH, 'w', encoding='utf-8') as f:
                json.dump(armory, f, ensure_ascii=False, indent=4)
            print(f"✅ NLP 進化完成！自動學習了 {added_count} 個全新暴漲關鍵字。")
        else:
            print("   -> 詞彙已存在於軍火庫中，維持現狀。")
            
    except Exception as e:
        print(f"⚠️ NLP 進化失敗: {e}")

if __name__ == '__main__':
    init_db()