import time
import threading
from datetime import datetime
import shared_state
from config import TZ_NY, TZ_TW

def init_alpaca():
    print("🚀 [FIRE-CONTROL] 啟動湧浪動態日誌引擎 (Ross 起漲前兆狙擊版)...")
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
                    
                # 初始化記憶體 (新增 vol_history 陣列來計算成交量加速度)
                if sym not in price_memory:
                    price_memory[sym] = {
                        "price": current_price, 
                        "vol": current_vol, 
                        "high": current_price, 
                        "ts": now_ts,
                        "vol_history": [] 
                    }
                    continue
                    
                old_price = price_memory[sym]["price"]
                old_vol = price_memory[sym].get("vol", current_vol)
                old_high = price_memory[sym].get("high", old_price)
                old_ts = price_memory[sym]["ts"]
                vol_history = price_memory[sym].get("vol_history", [])
                
                # 計算即時變數
                gap_pct = info.get("GapPct", 0.0)
                vol_diff = current_vol - old_vol
                rel_vol = info.get("RelVol", "-")
                
                # 取得 Scanner 打上的狙擊死囚印記
                is_sniper_target = info.get("IsSniperTarget", False)
                
                # 🚀 記錄並計算過去 20 秒內的「平均每兩秒成交動能」
                if vol_diff >= 0:
                    vol_history.append(vol_diff)
                    if len(vol_history) > 10:
                        vol_history.pop(0)
                
                avg_vol_diff = sum(vol_history) / len(vol_history) if vol_history else 0.1
                
                # 判斷特殊徽章
                badges = ""
                if gap_pct >= 5.0: badges += "⚡"
                if vol_diff >= 50000: badges += "🐳"
                if vol_diff >= 200000: badges += "🌊"
                
                # 更新今日最高價
                new_high = max(old_high, current_price)
                
                # 🎯 核心戰術：Ross Cameron 起漲前兆偵測！
                # 條件：是狙擊標靶，且單次爆量超過平均的 3 倍，且具備實質買盤 (>5000股)
                pre_breakout_triggered = False
                if is_sniper_target and vol_diff > (avg_vol_diff * 3) and vol_diff >= 5000:
                    pre_breakout_triggered = True
                
                if old_price > 0:
                    change_pct = ((current_price - old_price) / old_price) * 100
                    
                    # 觸發防線：常規急拉 >= 0.3%，或者「觸發了起漲前兆」
                    if (change_pct >= 0.3 or pre_breakout_triggered) and (now_ts - old_ts) >= 2: 
                        
                        crossed_whole = None
                        for level in [1.0, 2.0, 3.0, 4.0, 5.0, 10.0]:
                            if old_price < level <= current_price:
                                crossed_whole = level
                                break
                        
                        # 產生高含金量提示文字
                        if pre_breakout_triggered and change_pct < 0.3:
                            # 價格還沒噴，但量已經異常暴增
                            signal_text = f"🎯 [起漲前兆] 異常大單點火 ({int(vol_diff)}股)"
                        else:
                            signal_text = f"🚀 急拉 +{change_pct:.2f}%"
                            
                        if crossed_whole:
                            signal_text += f" (突破 ${crossed_whole:.2f} 關卡)"
                        elif current_price > old_high and change_pct >= 0.3:
                            signal_text += " (👑 突破盤前新高)"
                            
                        log_entry = {
                            "SignalTS": int(now_ts),
                            "Time": time_str,
                            "Code": sym,
                            "Price": f"${current_price:.2f}",
                            "Vol": info.get("Vol", "-"),
                            "RelVol": rel_vol,
                            "Badges": badges, 
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
                        
                        # 重置基準，避免瘋狂洗頻
                        price_memory[sym] = {"price": current_price, "vol": current_vol, "high": new_high, "ts": now_ts, "vol_history": vol_history}
                        
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
                        
                        price_memory[sym] = {"price": current_price, "vol": current_vol, "high": new_high, "ts": now_ts, "vol_history": vol_history}
                        
                # 定期推進，維持計算靈敏度
                if (now_ts - price_memory[sym]["ts"]) > 60:
                    price_memory[sym]["price"] = current_price
                    price_memory[sym]["vol"] = current_vol
                    price_memory[sym]["ts"] = now_ts

        except Exception as e:
            print(f"⚠️ [FIRE-CONTROL] 湧浪引擎發生錯誤: {e}")
            
        time.sleep(2)