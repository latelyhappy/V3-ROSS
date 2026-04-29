import os
import time
import threading
import math
from alpaca_trade_api.stream import Stream

_MASTER_BRAIN = None
_DYNAMIC_WATCHLIST = None
_brain_lock = None

def init_alpaca(master_brain, watchlist, lock):
    global _MASTER_BRAIN, _DYNAMIC_WATCHLIST, _brain_lock
    _MASTER_BRAIN = master_brain
    _DYNAMIC_WATCHLIST = watchlist
    _brain_lock = lock
    threading.Thread(target=_alpaca_thread, daemon=True).start()

def _alpaca_thread():
    api_key = os.getenv('ALPACA_API_KEY')
    secret_key = os.getenv('ALPACA_SECRET_KEY')
    
    if not api_key or not secret_key:
        print("⚠️ Alpaca API 憑證未設置，極速感測模組待命中...")
        return

    stream = Stream(api_key, secret_key, base_url='https://paper-api.alpaca.markets', data_feed='iex')
    price_history = {} 
    
    async def trade_callback(t):
        ticker = t.symbol
        price = t.price
        now_ts = time.time()
        
        if ticker not in price_history:
            price_history[ticker] = []
        price_history[ticker].append((now_ts, price))
        price_history[ticker] = [x for x in price_history[ticker] if now_ts - x[0] <= 10]
        
        past_5s = [x for x in price_history[ticker] if 4.0 <= now_ts - x[0] <= 6.0]
        is_velocity_spike = False
        if past_5s:
            old_price = past_5s[0][1]
            if old_price > 0 and (price - old_price) / old_price >= 0.005:
                is_velocity_spike = True

        is_whole_dollar = False
        if 0.95 <= price % 1.0 <= 0.99 and price > 1.0:
            is_whole_dollar = True
            
        if is_velocity_spike or is_whole_dollar:
            with _brain_lock:
                if ticker in _MASTER_BRAIN["details"]:
                    cell = _MASTER_BRAIN["details"][ticker]
                    current_signal = cell.get("Signal", "")
                    
                    if is_velocity_spike:
                        if "⚡極速" not in current_signal:
                            cell["Signal"] = f"⚡極速拉升(+0.5%/5s) " + current_signal
                        cell["Status"] = "purple"
                        cell["StickyColor"] = "purple"
                        cell["StickySignal"] = cell["Signal"]
                        cell["StickyTime"] = now_ts
                        
                    elif is_whole_dollar:
                        if "🧲磁吸" not in current_signal:
                            cell["Signal"] = f"🧲即將撞擊(${math.ceil(price):.2f}) " + current_signal
                        if cell.get("Status") != "purple":
                            cell["Status"] = "yellow"
                            cell["StickyColor"] = "yellow"
                        cell["StickySignal"] = cell["Signal"]
                        cell["StickyTime"] = now_ts

    def watch_subscriptions():
        current_subs = set()
        while True:
            time.sleep(5)
            try:
                # 💡 【核心修復】：強制截斷為前 25 檔，避免突破 Alpaca 免費版 30 檔的上限！
                safe_watchlist = list(_DYNAMIC_WATCHLIST)[:25]
                target_subs = set(safe_watchlist)
                
                to_add = target_subs - current_subs
                to_remove = current_subs - target_subs
                
                if to_add:
                    stream.subscribe_trades(trade_callback, *to_add)
                if to_remove:
                    stream.unsubscribe_trades(*to_remove)
                    for sym in to_remove:
                        price_history.pop(sym, None)
                        
                current_subs = target_subs
            except:
                pass

    threading.Thread(target=watch_subscriptions, daemon=True).start()
    
    while True:
        try:
            print("🚀 Alpaca 毫秒級火控雷達連線中...")
            stream.run()
        except Exception as e:
            print(f"⚠️ Alpaca 連線中斷，5秒後重連: {e}")
            time.sleep(5)