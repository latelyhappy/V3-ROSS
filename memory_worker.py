import os
import time
import sqlite3
import json
import threading
from datetime import datetime, timedelta
import re
from collections import Counter
from github import Github

# 讀取環境變數
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO') # 格式例如: "your-username/V3-ROSS"
DB_PATH = os.path.join(os.path.dirname(__file__), 'collector.db')
LEARNED_FILE = os.path.join(os.path.dirname(__file__), 'learned_catalysts.json')

def extract_elite_words(text):
    # 提取長度大於 3 的大寫單字或組合
    words = re.findall(r'\b[A-Z]{3,}\b', text.upper())
    return [w for w in words if w not in ["THE", "AND", "FOR", "WITH", "INC", "CORP", "LTD"]]

def auto_learn_and_push():
    print("🧠 [NLP WORKER] 啟動自動學習與 GitHub 推播程序...")
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("⚠️ [NLP WORKER] 未設定 GITHUB_TOKEN 或 GITHUB_REPO，跳過推播。")
        return

    try:
        # 1. 從資料庫提取過去 24 小時內漲幅 > 10% 的致勝標題
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute('''
            SELECT headline FROM event_logs 
            WHERE timestamp > ? AND change_percent >= 10.0
        ''', (yesterday,))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            print("💤 [NLP WORKER] 今日無足夠的暴漲語料，暫不更新。")
            return

        # 2. NLP 詞頻統計
        all_words = []
        for row in rows:
            all_words.extend(extract_elite_words(row[0]))
        
        word_counts = Counter(all_words)
        new_catalysts = {}
        for word, count in word_counts.items():
            if count >= 2: # 出現兩次以上的暴漲詞彙，賦予初始分數 10
                new_catalysts[word] = 10

        if not new_catalysts: return

        # 3. 讀取並合併舊有的 learned_catalysts.json
        existing_data = {}
        if os.path.exists(LEARNED_FILE):
            try:
                with open(LEARNED_FILE, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            except: pass
        
        existing_data.update(new_catalysts)

        # 存入本地端
        with open(LEARNED_FILE, 'w', encoding='utf-8') as f:
            json.dump(existing_data, f, ensure_ascii=False, indent=2)

        # 4. GitHub 自動推播 (Git Push)
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO)
        
        file_path = "learned_catalysts.json"
        commit_message = f"🤖 Auto-NLP Update: Learned {len(new_catalysts)} new catalysts"
        
        try:
            contents = repo.get_contents(file_path)
            repo.update_file(contents.path, commit_message, json.dumps(existing_data, indent=2, ensure_ascii=False), contents.sha)
            print("✅ [NLP WORKER] 成功更新 GitHub 上的 learned_catalysts.json！")
        except:
            repo.create_file(file_path, commit_message, json.dumps(existing_data, indent=2, ensure_ascii=False))
            print("✅ [NLP WORKER] 成功創建並推送 learned_catalysts.json 至 GitHub！")

    except Exception as e:
        print(f"❌ [NLP WORKER] 學習推播失敗: {e}")

def start_memory_loop():
    while True:
        # 每天檢查一次 (86400 秒)
        time.sleep(86400)
        auto_learn_and_push()

# 👇 app.py 就是在找這個函式！請確保有複製到這一段
def init_worker():
    threading.Thread(target=start_memory_loop, daemon=True).start()