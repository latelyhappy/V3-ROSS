import time
import concurrent.futures
from datetime import datetime
from tvDatafeed import TvDatafeed, Interval
import shared_state
import config
from utils import safe_float, format_vol, calc_hma
from engine_shares import get_shares_data
from engine_news import fetch_and_score_news
import requests

def update_dynamic_watchlist():
    """從 TradingView 掃描器獲取最新的強勢標的"""
    try:
        now_ny = datetime.now(config.TZ_NY)
        is_pm = now_ny.time() < datetime.strptime("09:30", "%H:%M").time()
        p_col, chg_col, vol_col = ("premarket_close", "premarket_change", "premarket_volume") if is_pm else ("close", "change", "volume")

        payload = {
        # 🚀 戰術修正：把所有成交量限制拿掉！只要符合價格區間的股票全部放行！
        "filter": [
            {"left": "price_last", "operation": "egreater", "right": 0.5},
            {"left": "price_last", "operation": "eless", "right": 50.0}
        ],
        "sort": {
            # 🚀 移除未定義的 is_premarket，改用直接判定 premarket_change！
            # 因為現在就是盤前時段，我們直接強制鎖定 premarket_change 進行排序！
            "expression": "premarket_change", 
            "method": "desc"
        }
        }

        res = requests.post("https://scanner.tradingview.com/america/scan", json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200 and res.json().get('data'):
            new_wlist, temp_stats = [], {}
            for x in res.json()['data']:
                sym, c, pct, v, pm_c, pm_pct, pm_v, mc, t, avg_v, r_vol = x['d'][0], x['d'][1], x['d'][2], x['d'][3], x['d'][4], x['d'][5], x['d'][6], x['d'][7], x['d'][8], x['d'][9], x['d'][10]
                new_wlist.append(sym)
                temp_stats[sym] = {
                    'type': t, 'avg_vol_10d': avg_v or 0, 'native_rel_vol': r_vol or 0, 
                    'total_vol': pm_v if is_pm else v, 'live_price': pm_c if is_pm else c, 'live_pct': pm_pct if is_pm else pct
                }
            with shared_state.brain_lock:
                shared_state.DYNAMIC_WATCHLIST = new_wlist[:100]
                shared_state.STATS_MAP.update(temp_stats)
    except: pass

def process_single_ticker(ticker, tv):
    """處理單支股票的 K 線與戰術運算"""
    try:
        # 1. 抓取 K 線 (設限 300 根以提升速度)
        df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=300, extended_session=True)
        stat = shared_state.STATS_MAP.get(ticker, {'live_price': 0.0, 'live_pct': 0.0, 'total_vol': 0, 'native_rel_vol': 1.0})
        
        # 2. 獲取基本面數據
        r_fl, o_sh = get_shares_data(ticker)
        if r_fl >= 20_000_000:
            with shared_state.brain_lock:
                if ticker in shared_state.MASTER_BRAIN["details"]: shared_state.MASTER_BRAIN["details"][ticker]["IsInvalid"] = True
            return

        p_live = float(df['close'].iloc[-1]) if df is not None and not df.empty else float(stat['live_price'])
        if p_live < 0.1 or p_live > 30.0: return

        pct_live = float(df['close'].pct_change().iloc[-1]*100) if df is not None and len(df)>1 else float(stat['live_pct'])
        v_raw = int(df['volume'].sum()) if df is not None else int(stat['total_vol'])
        
        # 3. 戰術數據更新 (HTML 格式化留給前端)
        with shared_state.brain_lock:
            cell = shared_state.MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0})
            stats = {
                "Code": ticker, "RelVol": f"{float(stat.get('native_rel_vol', 1.0)):.2f}x", "Vol": format_vol(v_raw),
                "Status": "green" if pct_live >= 0 else "red", 
                "Signal": "🎯 狙擊陣型" if pct_live > 5 else "監測中",
                "Float": format_shares_k_m(r_fl), "Outstanding": format_shares_k_m(o_sh),
                "Price": f"${p_live:.2f}", "PriceVal": safe_float(p_live), "PctVal": pct_live, "Pct": f"{pct_live:+.2f}%",
                "Daily_Vol_Raw": v_raw, "GapPct": safe_float(pct_live), "HighVal": p_live, "StopLoss": p_live*0.95
            }
            cell.update(stats)

            # 動態日誌
            if pct_live > 8:
                log_entry = {**stats, "Time": datetime.now(config.TZ_TW).strftime("%H:%M:%S"), "SignalTS": time.time(), "Row_Status": "flash-green"}
                if not any(l["Code"] == ticker and l["Time"] == log_entry["Time"] for l in shared_state.MASTER_BRAIN["surge_log"]):
                    shared_state.MASTER_BRAIN["surge_log"].insert(0, log_entry)
                    shared_state.MASTER_BRAIN["surge_log"] = shared_state.MASTER_BRAIN["surge_log"][:200]
    except: pass

def scanner_engine():
    """主掃描迴圈"""
    tv = TvDatafeed()
    if config.TW_USERNAME and config.TW_USERNAME != 'guest':
        try:
            tv = TvDatafeed(config.TW_USERNAME, config.TW_PASSWORD)
            shared_state.TV_LOGIN_STATUS = "✅ 登入成功"
        except: shared_state.TV_LOGIN_STATUS = "⚠️ 帳密錯誤"

    while True:
        try:
            now_ts = time.time()
            if now_ts - shared_state._last_list_update > 60: 
                update_dynamic_watchlist()
                shared_state._last_list_update = now_ts
                  
            wlist = shared_state.DYNAMIC_WATCHLIST.copy()
            if wlist:
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    # 使用 lambda 封裝 tv 傳遞
                    executor.map(lambda t: process_single_ticker(t, tv), wlist)
                
            with shared_state.brain_lock:
                all_items = list(shared_state.MASTER_BRAIN["details"].values())
                active_items = [x for x in all_items if x.get("Code") in shared_state.DYNAMIC_WATCHLIST and not x.get("IsInvalid", False)]
                shared_state.MASTER_BRAIN["leaderboard"] = sorted(active_items, key=lambda x: safe_float(str(x.get('GapPct', '0'))), reverse=True)[:100]
                shared_state.MASTER_BRAIN["last_update"] = datetime.now(config.TZ_TW).strftime('%H:%M:%S')
            time.sleep(1)
        except: time.sleep(2)

def process_single_ticker(ticker, tv):
    process_single_ticker(ticker, tv) # 這裡觸發上方的 process_single_ticker