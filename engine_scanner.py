import time
import requests
import concurrent.futures
import pandas as pd
import numpy as np
from datetime import datetime
from tvDatafeed import TvDatafeed, Interval
from config import TZ_NY, TZ_TW, TW_USERNAME, TW_PASSWORD
from utils import safe_float, format_shares_k_m, format_vol, calc_hma
from engine_shares import get_shares_data, fetch_yfinance_prev_close
from engine_news import fetch_and_score_news
import shared_state

def update_dynamic_watchlist():
    try:
        now_ny = datetime.now(TZ_NY)
        is_pm = now_ny.time() < datetime.strptime("09:30", "%H:%M").time()
        p_col, chg_col, vol_col = ("premarket_close", "premarket_change", "premarket_volume") if is_pm else ("close", "change", "volume")

        payload = {
            "filter": [
                {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]},
                {"left": "type", "operation": "in_range", "right": ["stock", "fund", "dr"]}, 
                {"left": p_col, "operation": "in_range", "right": [0.1, 30]},
                {"left": chg_col, "operation": "egreater", "right": -50.0},
                {"left": vol_col, "operation": "egreater", "right": 500}
            ], 
            "columns": ["name", "close", "change", "volume", "premarket_close", "premarket_change", "premarket_volume", "market_cap_basic", "type", "average_volume_10d_calc", "relative_volume_10d_calc"], 
            "sort": {"sortBy": chg_col, "sortOrder": "desc"}, 
            "range": [0, 100] 
        }

        res = requests.post("[https://scanner.tradingview.com/america/scan](https://scanner.tradingview.com/america/scan)", json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
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

def scanner_engine():
    tv = TvDatafeed()
    shared_state.TV_LOGIN_STATUS = "👤 訪客模式 (V59.0解耦版)"
    if TW_USERNAME and TW_USERNAME != 'guest':
        try:
            tv = TvDatafeed(TW_USERNAME, TW_PASSWORD)
            shared_state.TV_LOGIN_STATUS = "✅ 登入成功"
        except: shared_state.TV_LOGIN_STATUS = "⚠️ 帳密錯誤 (訪客模式)"

    while True:
        try:
            now_ts = time.time()
            if now_ts - shared_state._last_list_update > 60: 
                update_dynamic_watchlist()
                shared_state._last_list_update = now_ts
                  
            if not shared_state.DYNAMIC_WATCHLIST: 
                time.sleep(2); continue
                
            wlist = shared_state.DYNAMIC_WATCHLIST.copy()

            def _process_ticker(ticker):
                try:
                    df = None
                    try:
                        df = tv.get_hist(symbol=ticker, exchange='', interval=Interval.in_1_minute, n_bars=300, extended_session=True)
                    except: pass
                    
                    stat = shared_state.STATS_MAP.get(ticker, {'live_price': 0.0, 'live_pct': 0.0, 'total_vol': 0, 'native_rel_vol': 1.0})
                    r_fl, o_sh = get_shares_data(ticker)

                    if r_fl >= 20_000_000:
                        with shared_state.brain_lock:
                            shared_state.MASTER_BRAIN["details"].setdefault(ticker, {})["IsInvalid"] = True
                        return
                    
                    p_live = float(df['close'].iloc[-1]) if df is not None and not df.empty else float(stat['live_price'])
                    pct_live = float(df['close'].pct_change().iloc[-1]*100) if df is not None and len(df)>1 else float(stat['live_pct'])
                    v_raw = int(df['volume'].sum()) if df is not None else int(stat['total_vol'])
                    r_vol_val = float(stat['native_rel_vol'])

                    if p_live < 0.1 or p_live > 30.0: return

                    float_str = format_shares_k_m(r_fl)
                    out_str = format_shares_k_m(o_sh)
                    f_color = "cyan-bg" if (0 < r_fl < 1000000) else "gray-bg"
                    turnover_val = (v_raw / r_fl) if r_fl > 0 else 0.0
                    
                    # 乾淨文字訊號，HTML樣式交給前端
                    signal_text = "● 金叉啟動" if pct_live > 5 else "常規波動"
                    
                    with shared_state.brain_lock:
                        cell = shared_state.MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": [], "CatScore": 0})
                        stats = {
                            "Code": ticker, "RelVol": f"{r_vol_val:.2f}x", "Vol": format_vol(v_raw),
                            "Status": "green" if pct_live >= 0 else "red", 
                            "Signal": signal_text,
                            "Float": float_str, "Outstanding": out_str, "Float_Color": f_color,
                            "Real_Float_Shares": r_fl, "Shares_Outstanding_Raw": o_sh,
                            "Price": f"${p_live:.2f}", "PriceVal": safe_float(p_live), "Pct": f"{pct_live:+.2f}%", "Amt": f"{pct_live:+.2f}",
                            "Daily_Vol_Raw": v_raw, "Turnover": f"{turnover_val*100:.1f}%", "GapPct": safe_float(pct_live), "HighVal": p_live, "StopLoss": p_live*0.95
                        }
                        cell.update(stats)

                        # 日誌觸發 (無攔截)
                        if pct_live > 8:
                            log_entry = {**stats, "Time": datetime.now(TZ_TW).strftime("%H:%M:%S"), "SignalTS": now_ts, "Row_Status": "flash-green" if pct_live > 15 else "normal"}
                            if not any(l["Code"] == ticker and l["Time"] == log_entry["Time"] for l in shared_state.MASTER_BRAIN["surge_log"]):
                                shared_state.MASTER_BRAIN["surge_log"].insert(0, log_entry)
                                shared_state.MASTER_BRAIN["surge_log"] = shared_state.MASTER_BRAIN["surge_log"][:200]
                except: pass

            # 多執行緒降頻並發防 Ban
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                executor.map(_process_ticker, wlist)
                
            with shared_state.brain_lock:
                all_items = list(shared_state.MASTER_BRAIN["details"].values())
                active_items = [x for x in all_items if x.get("Code") in shared_state.DYNAMIC_WATCHLIST and not x.get("IsInvalid", False)]
                shared_state.MASTER_BRAIN["leaderboard"] = sorted(active_items, key=lambda x: safe_float(str(x.get('GapPct', '0'))), reverse=True)[:100]
                shared_state.MASTER_BRAIN["last_update"] = datetime.now(TZ_TW).strftime('%H:%M:%S')
            
            time.sleep(1)
        except: time.sleep(2)