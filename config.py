import os
import pytz

# ==========================================
# 🔑 API 密鑰與環境變數設定
# ==========================================
FINNHUB_TOKEN = os.getenv('FINNHUB_TOKEN')
TW_USERNAME = os.getenv('TW_USERNAME', 'guest')
TW_PASSWORD = os.getenv('TW_PASSWORD', 'guest')
PORT = int(os.getenv('PORT', 8080))

# Alpaca API Keys (提供給 alpaca_worker 使用)
ALPACA_API_KEY = os.getenv('ALPACA_API_KEY')
ALPACA_SECRET_KEY = os.getenv('ALPACA_SECRET_KEY')

# ==========================================
# 📂 系統核心路徑設定 (絕對路徑防錯機制)
# ==========================================
BASE_DIR = os.path.dirname(__file__)

# 狀態與快取檔
SESSION_BACKUP_PATH = os.path.join(BASE_DIR, 'session_state.json')
SHARES_CACHE_FILE = os.path.join(BASE_DIR, 'float_cache.json')  # 對齊您的實際目錄
DISCOVERY_LOG_PATH = os.path.join(BASE_DIR, 'discovery_log.json')

# 戰術情報庫檔
CATALYST_ARMORY_PATH = os.path.join(BASE_DIR, 'catalysts.json')
LEARNED_CATALYSTS_PATH = os.path.join(BASE_DIR, 'learned_catalysts.json')
TRENDS_FILE_PATH = os.path.join(BASE_DIR, 'trends.json')

# 戰地資料庫
DB_PATH = os.path.join(BASE_DIR, 'sniper_intelligence.db')

# ==========================================
# 🌍 時間與時區設定
# ==========================================
TZ_TW = pytz.timezone('Asia/Taipei')
TZ_NY = pytz.timezone('America/New_York')

# 系統快取存活時間 (秒)
TRENDS_CACHE_TTL = 60