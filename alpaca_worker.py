import time
import threading
from datetime import datetime
import shared_state
from config import TZ_NY, TZ_TW

def init_alpaca():
    print("🚀 [FIRE-CONTROL] 啟動湧浪動態日誌引擎 (追加量比數據版)...")
    threading.Thread(target=surge_monitor_loop, daemon=True).start()

def surge_monitor_loop():
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
                    
                if sym not in price_memory:
                    price_memory[sym] = {"price": current_price, "ts": now_ts}
                    continue
                    
                old_price = price_memory[sym]["price"]
                old_ts = price_memory[sym]["ts"]
                
                # 取得大腦記憶體中的量比
                rel_vol = info.get("RelVol", "-")
                
                if old_price > 0:
                    change_pct = ((current_price - old_price) / old_price) * 100
                    
                    if change_pct >= 0.3 and (now_ts - old_ts) >= 2: 
                        log_entry = {
                            "SignalTS": int(now_ts),
                            "Time": time_str,
                            "Code": sym,
                            "Price": f"${current_price:.2f}",
                            "Vol": info.get("Vol", "-"),
                            "RelVol": rel_vol,  # 🚀 新增：推播量比
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
                            shared_state.MASTER_BRAIN["surge_log"] = log[:50] 
                        
                        price_memory[sym] = {"price": current_price, "ts": now_ts}
                        
                    elif change_pct <= -0.5 and (now_ts - old_ts) >= 2:
                        log_entry = {
                            "SignalTS": int(now_ts),
                            "Time": time_str,
                            "Code": sym,
                            "Price": f"${current_price:.2f}",
                            "Vol": info.get("Vol", "-"),
                            "RelVol": rel_vol,  # 🚀 新增：推播量比
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
                        
                if (now_ts - price_memory[sym]["ts"]) > 60:
                    price_memory[sym] = {"price": current_price, "ts": now_ts}

        except Exception as e:
            print(f"⚠️ [FIRE-CONTROL] 湧浪引擎發生錯誤: {e}")
            
        time.sleep(2)