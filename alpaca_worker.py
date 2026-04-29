import os
import time
import threading
import math
from alpaca_trade_api.stream import Stream

# 接收來自 app.py 的共享大腦
_MASTER_BRAIN = None
_DYNAMIC_WATCHLIST = None
_brain_lock = None

def init_alpaca(master_brain, watchlist, lock):
    global _MASTER_BRAIN, _DYNAMIC_WATCHLIST, _brain_lock
    _MASTER_BRAIN = master_brain
    _DYNAMIC_WATCHLIST = watchlist
    _brain_lock = lock
    
    # 啟動背景獨立線程，絕不卡死主程式
    threading.Thread(target=_alpaca_thread, daemon=True).start()

def _alpaca_thread():
    api_key = os.getenv('ALPACA_API_KEY')
    secret_key = os.getenv('ALPACA_SECRET_KEY')
    
    if not api_key or not secret_key:
        print("⚠️ Alpaca API 憑證未設置，極速感測模組待命中...")
        return

    # 啟用 Alpaca 免費版 IEX 即時串流
    stream = Stream(api_key, secret_key, base_url='https://paper-api.alpaca.markets', data_feed='iex')
    
    price_history = {} # 記錄逐筆報價: { ticker: [(timestamp, price), ...] }
    
    async def trade_callback(t):
        ticker = t.symbol
        price = t.price
        now_ts = time.time()
        
        # 1. 更新 10 秒內的價格記憶
        if ticker not in price_history:
            price_history[ticker] = []
        price_history[ticker].append((now_ts, price))
        price_history[ticker] = [x for x in price_history[ticker] if now_ts - x[0] <= 10]
        
        # 2. ⚡【加速度偵測】：比對 5 秒前的價格 (0.5% 漲幅門檻)
        past_5s = [x for x in price_history[ticker] if 4.0 <= now_ts - x[0] <= 6.0]
        is_velocity_spike = False
        if past_5s:
            old_price = past_5s[0][1]
            if old_price > 0 and (price - old_price) / old_price >= 0.005:
                is_velocity_spike = True

        # 3. 🧲【整數關卡磁吸】：距離整數只差 1~5 美分
        is_whole_dollar = False
        if 0.95 <= price % 1.0 <= 0.99 and price > 1.0:
            is_whole_dollar = True
            
        # 4. 戰術警報寫入主大腦
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
                        # 如果已經是紫色(爆量/極速)，就不要降級成黃色
                        if cell.get("Status") != "purple":
                            cell["Status"] = "yellow"
                            cell["StickyColor"] = "yellow"
                        cell["StickySignal"] = cell["Signal"]
                        cell["StickyTime"] = now_ts

    # 背景排程：每 5 秒同步一次 Top 40 追蹤名單
    def watch_subscriptions():
        current_subs = set()
        while True:
            time.sleep(5)
            try:
                target_subs = set(_DYNAMIC_WATCHLIST)
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
    
    # 啟動串流 (遇斷線自動重連)
    while True:
        try:
            print("🚀 Alpaca 毫秒級火控雷達連線中...")
            stream.run()
        except Exception as e:
            print(f"⚠️ Alpaca 連線中斷，5秒後重連: {e}")
            time.sleep(5)