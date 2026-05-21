import os
import time
import json
import threading
from github import Github

# 🚀 匯入 V59.0 中央設定
from config import LEARNED_CATALYSTS_PATH

# 讀取環境變數
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO') # 格式例如: "your-username/V3-ROSS"

def auto_push_to_github():
    """定期將 AI 學習到的軍火庫推送到 GitHub，避免 Railway 重啟後資料遺失"""
    print("☁️ [NLP WORKER] 啟動 GitHub 雲端記憶同步程序...")
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("⚠️ [NLP WORKER] 未設定 GITHUB_TOKEN 或 GITHUB_REPO，跳過推播。")
        return

    try:
        # 確認學習檔案是否存在
        if not os.path.exists(LEARNED_CATALYSTS_PATH):
            print("💤 [NLP WORKER] 尚未產生學習庫，跳過同步。")
            return
            
        with open(LEARNED_CATALYSTS_PATH, 'r', encoding='utf-8') as f:
            learned_data = json.load(f)
            
        if not learned_data:
            return

        # 連線至 GitHub
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO)
        file_path = "learned_catalysts.json"
        commit_message = f"🤖 Auto-NLP Update: System learned {len(learned_data)} elite catalysts"
        
        try:
            # 嘗試更新現有檔案
            contents = repo.get_contents(file_path)
            repo.update_file(contents.path, commit_message, json.dumps(learned_data, indent=4, ensure_ascii=False), contents.sha)
            print("✅ [NLP WORKER] 成功更新 GitHub 上的 learned_catalysts.json！")
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                # 如果 GitHub 上還沒有這個檔案，就建立它
                repo.create_file(file_path, commit_message, json.dumps(learned_data, indent=4, ensure_ascii=False))
                print("✅ [NLP WORKER] 成功創建並推送 learned_catalysts.json 至 GitHub！")
            else:
                # 檔案內容可能沒有變動 (No changes to commit)
                print("💤 [NLP WORKER] 雲端記憶已是最新，無需更新。")
                pass
    except Exception as e:
        print(f"❌ [NLP WORKER] 學習推播失敗: {e}")

def start_memory_loop():
    # 啟動時先等 5 分鐘，確保資料庫與情報網都初始化完畢
    time.sleep(300)
    while True:
        auto_push_to_github()
        # 每 12 小時 (43200 秒) 自動備份一次到 GitHub
        time.sleep(43200)

# 👇 app.py 就是在找這個函式！
def init_worker():
    threading.Thread(target=start_memory_loop, daemon=True).start()