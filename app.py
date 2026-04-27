import streamlit as st
import pandas as pd
import time
from datetime import datetime
from tvDatafeed import TvDatafeed, Interval
import json
import os
import requests
import winsound
import concurrent.futures  # V44.0 新增：異步併發套件

# --- 全局變數與初始化 ---
st.set_page_config(page_title="Sniper V44.0 狙擊系統", layout="wide")

# 初始化 tvDatafeed (訪客模式)
try:
    tv = TvDatafeed()
except Exception as e:
    st.error(f"tvDatafeed 初始化失敗: {e}")

# 您的觀察名單 (請依需求增刪)
DYNAMIC_WATCHLIST = ['OGN', 'TRT', 'TIVC', 'GME', 'AMC', 'TSLA', 'NVDA', 'AAPL'] 

FINNHUB_TOKEN = os.environ.get("FINNHUB_TOKEN", "您的_FINNHUB_API_KEY_請放這裡")

# --- Session State 初始化 ---
if 'scanner_running' not in st.session_state:
    st.session_state.scanner_running = False
if 'scan_results' not in st.session_state:
    st.session_state.scan_results = []
if 'news_results' not in st.session_state:
    st.session_state.news_results = [] # 假設這裡存放 catalysts.json 評分結果

# --- 輔助函式 ---
def play_alarm():
    """觸發警報音"""
    try:
        winsound.Beep(1000, 500)
    except:
        pass

def save_session():
    """儲存狀態"""
    with open('session_state.json', 'w') as f:
        json.dump(st.session_state.scan_results, f)

def get_real_float_finnhub(symbol):
    """盤前靜態 Float 鎖定 (沿用您的完美寫法)"""
    cache_file = "float_cache.json"
    float_cache = {}
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            float_cache = json.load(f)
            
    if symbol in float_cache:
        return float_cache[symbol]
        
    try:
        url = f"https://finnhub.io/api/v1/stock/profile2?symbol={symbol}&token={FINNHUB_TOKEN}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # finnhub 的 shareOutstanding 單位是百萬(M)
            real_float = data.get('shareOutstanding', 0) * 1e6 
            if real_float > 0:
                float_cache[symbol] = real_float
                with open(cache_file, 'w') as f:
                    json.dump(float_cache, f)
                return real_float
    except Exception as e:
        print(f"Finnhub Float 抓取失敗 {symbol}: {e}")
    return 0

def check_volume_comb(df):
    """量能梳子檢測 (保留您的視覺比例演算法)"""
    if len(df) < 10:
        return False, "資料不足"
    recent_vol = df['volume'].tail(10)
    v_max = recent_vol.max()
    if v_max == 0:
        return False, "無交易量"
    
    # 缺牙檢測
    teeth = recent_vol[recent_vol < (v_max * 0.05)]
    if len(teeth) > 0:
        return False, "⚠️流動性斷層"
        
    avg_vol = recent_vol.mean()
    if avg_vol > (v_max * 0.25):
        return True, "🪮健康梳子"
    return False, "量能不均"

def calculate_indicators(df):
    """計算核心技術指標：EMA9, EMA52, VWAP"""
    # 確保有足夠長度計算 EMA52
    if len(df) > 0:
        df['EMA9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['EMA52'] = df['close'].ewm(span=52, adjust=False).mean()
        
        # VWAP = 累計(典型價格 * 交易量) / 累計交易量
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        df['VWAP'] = (typical_price * df['volume']).cumsum() / df['volume'].cumsum()
    return df

# --- V44.0 核心引擎 ---
def scanner_engine():
    global tv
    
    def process_symbol(symbol):
        try:
            exchange = 'NASDAQ'
            # 抓取 1Min K 線 (抓 60 根以確保 EMA52 計算準確)
            hist_df = tv.get_hist(symbol, exchange, interval=Interval.in_1_minute, n_bars=60)
            if hist_df is None or hist_df.empty:
                exchange = 'NYSE'
                hist_df = tv.get_hist(symbol, exchange, interval=Interval.in_1_minute, n_bars=60)
            
            if hist_df is None or hist_df.empty:
                return None

            # 計算不可或缺的核心指標
            hist_df = calculate_indicators(hist_df)
            p_live = hist_df['close'].iloc[-1]
            vwap_live = hist_df['VWAP'].iloc[-1]

            # 抓取 Daily K 線：取得「真實昨收價」與「當日累積總量」
            daily_df = tv.get_hist(symbol, exchange, interval=Interval.in_daily, n_bars=2)
            if daily_df is not None and not daily_df.empty:
                prev_close = daily_df['close'].iloc[-2] if len(daily_df) >= 2 else daily_df['open'].iloc[0]
                true_total_vol = daily_df['volume'].iloc[-1] # 直接鎖定官方總量
            else:
                prev_close = hist_df['open'].iloc[0] 
                true_total_vol = hist_df['volume'].sum()

            pct_change = ((p_live - prev_close) / prev_close) * 100 if prev_close else 0

            # 萃取 Float 與計算 HP%
            real_float = get_real_float_finnhub(symbol)
            if real_float > 0:
                hp_ratio = (true_total_vol / real_float) * 100
                float_str = f"{real_float/1e6:.1f}M"
            else:
                hp_ratio = 0.0
                float_str = "N/A"

            # 量能梳子判斷
            is_healthy, comb_msg = check_volume_comb(hist_df)

            # 新聞物理錨點與 VWAP 突破判斷
            breakout_msg = "⏳等待確認"
            cat_score = 0
            
            if 'news_results' in st.session_state:
                news_data = next((item for item in st.session_state.news_results if item['代碼'] == symbol), None)
                if news_data:
                    cat_score = news_data.get('該股總分(CatScore)', 0)
                    p_news = float(news_data.get('新聞當刻高點', 0))
                    
                    if p_news > 0:
                        # 邏輯強化：確認實體突破新聞高點，且價格必須站穩 VWAP 之上
                        if p_live > p_news and p_live > vwap_live:
                            breakout_msg = f"✅ VWAP/新聞 雙突破 ({p_news:.2f})"
                        elif p_live > p_news:
                            breakout_msg = f"⚠️ 破新聞但低於 VWAP"
                        else:
                            breakout_msg = f"⏳ 尚未突破 ({p_news:.2f})"

            return {
                '代碼': symbol,
                '當前價格': round(p_live, 3),
                '漲幅%': round(pct_change, 2),
                '成交量': true_total_vol, 
                'HP%': round(hp_ratio, 2),
                'Float': float_str,
                'Comb': comb_msg,
                'Breakout': breakout_msg,
                '總分': cat_score,
                'VWAP': round(vwap_live, 3),
                'EMA9': round(hist_df['EMA9'].iloc[-1], 3)
            }
        except Exception as e:
            # 靜默失敗
            return None

    # 異步掃描主迴圈
    while st.session_state.scanner_running:
        start_time = time.time()
        
        # 異步併發：5 檔同時抓取，大幅降低延遲
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(process_symbol, DYNAMIC_WATCHLIST))
            
        updated_results = [r for r in results if r is not None]

        # 警報觸發邏輯：總分高、高周轉率，且站上 VWAP
        for r in updated_results:
            if r['總分'] >= 50 and r['HP%'] >= 5 and "雙突破" in r['Breakout']:
                play_alarm()

        st.session_state.scan_results = updated_results
        save_session()

        # 強制 5 秒冷卻節奏
        elapsed = time.time() - start_time
        sleep_time = max(1.0, 5.0 - elapsed)
        time.sleep(sleep_time)

# --- UI 渲染介面 ---
st.title("🎯 Sniper V44.0 戰術監控終端")
st.markdown("### 異步極速掃描 | 真實量能對齊 | VWAP 突破追蹤")

col1, col2 = st.columns([1, 5])
with col1:
    if st.button("啟動掃描引擎" if not st.session_state.scanner_running else "🛑 停止掃描"):
        st.session_state.scanner_running = not st.session_state.scanner_running
        if st.session_state.scanner_running:
            st.rerun() # 重新執行腳本以啟動迴圈

with col2:
    if st.session_state.scanner_running:
        st.success("🟢 引擎運轉中 (5秒異步刷新)")
        scanner_engine()
    else:
        st.warning("🔴 引擎已停止")

if st.session_state.scan_results:
    df_display = pd.DataFrame(st.session_state.scan_results)
    st.dataframe(
        df_display.style.format({
            '當前價格': '{:.2f}',
            '漲幅%': '{:.2f}%',
            '成交量': '{:,.0f}',
            'HP%': '{:.2f}%',
            'VWAP': '{:.2f}',
            'EMA9': '{:.2f}'
        }),
        height=600,
        use_container_width=True
    )
