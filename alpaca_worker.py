import os
import time
import threading
import math
from alpaca_trade_api.stream import Stream
import pytz
from datetime import datetime

_MASTER_BRAIN = None
_DYNAMIC_WATCHLIST = None
_brain_lock = None
_alpaca_cooldown = {} # 💡 毫秒級防洪閘門紀錄器
TZ_TW = pytz.timezone('Asia/Taipei')

def init_alpaca(master_brain, watchlist, lock):
    global _MASTER_BRAIN, _DYNAMIC_WATCHLIST, _brain_lock
    _MASTER_BRAIN = master_brain
    _DYNAMIC_WATCHLIST = watchlist
    _brain_lock = lock
    threading.Thread(target=_alpaca_thread, daemon=True).start()

def _alpaca_thread():
    global _alpaca_cooldown
    api_key = os.getenv('ALPACA_API_KEY')
    secret_key = os.getenv('ALPACA_SECRET_KEY')
    if not api_key or not secret_key: return

    stream = Stream(api_key, secret_key, base_url='https://paper-api.alpaca.markets', data_feed='iex')
    price_history = {} 
    
    async def trade_callback(t):
        ticker = t.symbol
        price = t.price
        now_ts = time.time()
        
        if ticker not in price_history: price_history[ticker] = []
        price_history[ticker].append((now_ts, price))
        price_history[ticker] = [x for x in price_history[ticker] if now_ts - x[0] <= 10]
        
        # 💡 秒速點火偵測 (5 秒區塊)
        past_5s = [x for x in price_history[ticker] if 4.0 <= now_ts - x[0] <= 6.0]
        is_velocity_spike = False
        if past_5s:
            old_price = past_5s[0][1]
            if old_price > 0 and (price - old_price) / old_price >= 0.005: is_velocity_spike = True

        is_whole_dollar = False
        if 0.95 <= price % 1.0 <= 0.99 and price > 1.0: is_whole_dollar = True
            
        if is_velocity_spike or is_whole_dollar:
            with _brain_lock:
                if ticker in _MASTER_BRAIN["details"]:
                    cell = _MASTER_BRAIN["details"][ticker]
                    
                    # 基礎清理，避免無限疊加
                    raw_signal = cell.get("Signal", "").replace("⚡極速拉升(+0.5%/5s)", "").replace(f"🧲即將撞擊(${math.ceil(price):.2f})", "").strip()
                    
                    signal_triggered = False
                    new_tag = ""
                    
                    # 💡 核心修復：使用乾淨的標籤覆蓋
                    if is_velocity_spike:
                        new_tag = "⚡極速拉升(+0.5%/5s) "
                        cell["Status"] = "purple"
                        cell["StickyColor"] = "purple"
                        signal_triggered = True
                        
                    elif is_whole_dollar:
                        new_tag = f"🧲即將撞擊(${math.ceil(price):.2f}) "
                        if cell.get("Status") != "purple":
                            cell["Status"] = "yellow"
                            cell["StickyColor"] = "yellow"
                        signal_triggered = True

                    if signal_triggered:
                        cell["Signal"] = new_tag + raw_signal
                        cell["StickySignal"] = cell["Signal"]
                        cell["StickyTime"] = now_ts
                        
                        last_a_time = _alpaca_cooldown.get(ticker, 0)
                        if now_ts - last_a_time > 30:
                            _alpaca_cooldown[ticker] = now_ts
                            stats_copy = {k: v for k, v in cell.items() if k not in ["NewsList", "CatScore", "IsTrap", "StickySignal", "StickyColor", "StickyTime"]}
                            
                            # Alpaca 單獨觸發的訊號改為靜音
                            _MASTER_BRAIN["surge_log"].insert(0, {
                                **stats_copy, 
                                "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), 
                                "SignalTS": now_ts, 
                                "Audio": None
                            })
                            _MASTER_BRAIN["surge_log"] = _MASTER_BRAIN["surge_log"][:1000]

    def watch_subscriptions():
        current_subs = set()
        while True:
            time.sleep(5)
            try:
                # 💡 核心變更：強迫 Alpaca 只監聽「已經處理好且掛上排行榜」的菁英股票！
                leaderboard = _MASTER_BRAIN.get("leaderboard", [])
                valid_symbols = [item["Code"] for item in leaderboard if "Code" in item]
                
                # 💡 設定安全閾值：最多 28 檔，確保加上緩衝絕不超過免費版 30 檔上限
                safe_watchlist = valid_symbols[:28]
                target_subs = set(safe_watchlist)
                to_add = target_subs - current_subs
                to_remove = current_subs - target_subs
                
                # 💡 關鍵順序對調：先「退訂(unsubscribe)」釋放空間，再「訂閱(subscribe)」！
                if to_remove: 
                    stream.unsubscribe_trades(*to_remove)
                    for sym in to_remove: 
                        price_history.pop(sym, None)
                        _alpaca_cooldown.pop(sym, None) # 💡 聯動清空：徹底移除舊的冷卻記憶！
                        
                if to_add: 
                    stream.subscribe_trades(trade_callback, *to_add)
                    
                current_subs = target_subs
            except: pass

    threading.Thread(target=watch_subscriptions, daemon=True).start()
    
    while True:
        try:
            print("🚀 Alpaca 毫秒級火控雷達連線中...")
            stream.run()
        except ValueError as e:
            if "connection limit exceeded" in str(e).lower():
                print("🚨 Alpaca 連線數超載！冷卻 10 秒後重試...")
                time.sleep(10)
            else:
                time.sleep(5)
        except Exception:
            time.sleep(30)