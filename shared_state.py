import threading

# 執行緒鎖
brain_lock = threading.RLock()

# 全域狀態 (所有模組共享)
MASTER_BRAIN = {
    "surge_log": [], "details": {}, "leaderboard": [], 
    "top_catalysts": [], "last_update": "", "elite_words": []
}
DYNAMIC_WATCHLIST = []
STATS_MAP = {}
news_cache = {}
cooldown_tracker = {}
STATE_TRACKER = {}
SHARES_CACHE = {}

# 掃描器狀態
TV_LOGIN_STATUS = "檢查中..."
MIN_RELVOL_LIMIT = 3.0
SPY_real_pct = 0.0
_last_list_update = 0
_last_spy_update = 0
_current_session_date = ""

# 情報資料庫
CATALYST_ARMORY = {}
_cached_trends = {}
_last_trends_update = 0