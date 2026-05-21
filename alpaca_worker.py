import time
import threading
from datetime import datetime
import shared_state
from config import TZ_NY, TZ_TW

def init_alpaca():
    print("🚀 [FIRE-CONTROL] 啟動湧浪動態日誌引擎 (巨鯨偵測與戰術提示版)...")
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
                current_vol = info.get("Daily_Vol_Raw", 0.0)
                if current_price <= 0:
                    continue
                    
                if sym not in price_memory:
                    price_memory[sym] = {"price": current_price, "vol": current_vol, "high": current_price, "ts": now_ts}
                    continue
                    
                old_price = price_memory[sym]["price"]
                old_vol = price_memory[sym].get("vol", 0.0)
                old_high = price_memory[sym].get("high", old_price)
                old_ts = price_memory[sym]["ts"]
                
                # 計算即時變數
                gap_pct = info.get("GapPct", 0.0)
                vol_diff = current_vol - old_vol
                rel_vol = info.get("RelVol", "-")
                
                # 判斷特殊徽章
                badges = ""
                if gap_pct >= 5.0: badges += "⚡" # ⚡ 跳空開高標誌
                if vol_diff >= 50000: badges += "🐳" # 🐳 巨鯨大單買入標誌 (短時間湧入5萬股)
                if vol_diff >= 200000: badges += "🌊" # 🌊 狂暴巨浪
                
                # 更新今日最高價
                new_high = max(old_high, current_price)
                
                if old_price > 0:
                    change_pct = ((current_price - old_price) / old_price) * 100
                    
                    if change_pct >= 0.3 and (now_ts - old_ts) >= 2: 
                        
                        # 🚀 戰術情境分析：判斷是否突破整數大關
                        crossed_whole = None
                        for level in [1.0, 2.0, 3.0, 4.0, 5.0, 10.0]:
                            if old_price < level <= current_price:
                                crossed_whole = level
                                break
                        
                        # 產生高含金量提示文字
                        signal_text = f"🚀 急拉 +{change_pct:.2f}%"
                        if crossed_whole:
                            signal_text += f" (突破 ${crossed_whole:.2f} 心理關卡)"
                        elif current_price > old_high:
                            signal_text += " (👑 突破盤前新高)"
                            
                        log_entry = {
                            "SignalTS": int(now_ts),
                            "Time": time_str,
                            "Code": sym,
                            "Price": f"${current_price:.2f}",
                            "Vol": info.get("Vol", "-"),
                            "RelVol": rel_vol,
                            "Badges": badges, # 🚀 傳送圖示到前端
                            "Signal": signal_text,
                            "Status": "green",
                            "Row_Status": "flash-green",
                            "Type": "stock",
                            "Audio": True
                        }
                        
                        with shared_state.brain_lock:
                            log = shared_state.MASTER_BRAIN.get("surge_log", [])
                            log.insert(0, log_entry)
                            shared_state.MASTER_BRAIN["surge_log"] = log[:50] 
                        
                        price_memory[sym] = {"price": current_price, "vol": current_vol, "high": new_high, "ts": now_ts}
                        
                    elif change_pct <= -0.5 and (now_ts - old_ts) >= 2:
                        log_entry = {
                            "SignalTS": int(now_ts),
                            "Time": time_str,
                            "Code": sym,
                            "Price": f"${current_price:.2f}",
                            "Vol": info.get("Vol", "-"),
                            "RelVol": rel_vol,
                            "Badges": badges,
                            "Signal": f"🩸 遭遇倒貨 {change_pct:.2f}%",
                            "Status": "red",
                            "Row_Status": "",
                            "Type": "stock",
                            "Audio": False
                        }
                        with shared_state.brain_lock:
                            log = shared_state.MASTER_BRAIN.get("surge_log", [])
                            log.insert(0, log_entry)
                            shared_state.MASTER_BRAIN["surge_log"] = log[:50]
                        
                        price_memory[sym] = {"price": current_price, "vol": current_vol, "high": new_high, "ts": now_ts}
                        
                if (now_ts - price_memory[sym]["ts"]) > 60:
                    price_memory[sym] = {"price": current_price, "vol": current_vol, "high": new_high, "ts": now_ts}

        except Exception as e:
            print(f"⚠️ [FIRE-CONTROL] 湧浪引擎發生錯誤: {e}")
            
        time.sleep(2)