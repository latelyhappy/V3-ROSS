import time
import threading
from datetime import datetime
import shared_state
from config import TZ_NY, TZ_TW

def init_alpaca():
    print("🚀 [FIRE-CONTROL] 啟動湧浪動態日誌引擎 (盤前解除封印版)...")
    threading.Thread(target=surge_monitor_loop, daemon=True).start()

def surge_monitor_loop():
    # 紀錄上一秒的價格，用來比對瞬間拉升
    price_memory = {}
    
    while True:
        try:
            now_ts = time.time()
            time_str = datetime.now(TZ_TW).strftime("%H:%M:%S")
            
            with shared_state.brain_lock:
                details = shared_state.MASTER_BRAIN.get("details", {})
                
            for sym, info in details.items():
                current_price = info.get("PriceVal", 0.0)
                if current_price <= 0:
                    continue
                    
                # 初始化記憶體
                if sym not in price_memory:
                    price_memory[sym] = {"price": current_price, "ts": now_ts}
                    continue
                    
                old_price = price_memory[sym]["price"]
                old_ts = price_memory[sym]["ts"]
                
                # 計算價格變化率
                if old_price > 0:
                    change_pct = ((current_price - old_price) / old_price) * 100
                    
                    # 🚀 盲區一修復：解除盤前封印！只要瞬間拉升 > 0.3%，立刻觸發警報寫入日誌！
                    if change_pct >= 0.3 and (now_ts - old_ts) >= 2: 
                        log_entry = {
                            "SignalTS": int(now_ts),
                            "Time": time_str,
                            "Code": sym,
                            "Price": f"${current_price:.2f}",
                            "Vol": info.get("Vol", "-"),
                            "Signal": f"🚀 湧浪急拉 +{change_pct:.2f}%",
                            "Status": "green",
                            "Row_Status": "flash-green",
                            "Type": "stock",
                            "VolTier": 3,
                            "Audio": True
                        }
                        
                        with shared_state.brain_lock:
                            log = shared_state.MASTER_BRAIN.get("surge_log", [])
                            log.insert(0, log_entry)
                            shared_state.MASTER_BRAIN["surge_log"] = log[:50] # 保留前 50 筆
                        
                        # 更新記憶體基準價，避免重複觸發
                        price_memory[sym] = {"price": current_price, "ts": now_ts}
                        
                    # 偵測遭遇倒貨 (瞬間下跌 > 0.5%)
                    elif change_pct <= -0.5 and (now_ts - old_ts) >= 2:
                        log_entry = {
                            "SignalTS": int(now_ts),
                            "Time": time_str,
                            "Code": sym,
                            "Price": f"${current_price:.2f}",
                            "Vol": info.get("Vol", "-"),
                            "Signal": f"🩸 遭遇倒貨 {change_pct:.2f}%",
                            "Status": "red",
                            "Row_Status": "",
                            "Type": "stock",
                            "VolTier": 3,
                            "Audio": False
                        }
                        with shared_state.brain_lock:
                            log = shared_state.MASTER_BRAIN.get("surge_log", [])
                            log.insert(0, log_entry)
                            shared_state.MASTER_BRAIN["surge_log"] = log[:50]
                        
                        price_memory[sym] = {"price": current_price, "ts": now_ts}
                        
                # 定期重置基準價 (每 60 秒)，讓動能運算始終保持「極短線」
                if (now_ts - price_memory[sym]["ts"]) > 60:
                    price_memory[sym] = {"price": current_price, "ts": now_ts}

        except Exception as e:
            print(f"⚠️ [FIRE-CONTROL] 湧浪引擎發生錯誤: {e}")
            
        time.sleep(2) # 每 2 秒偵測一次瞬間價差