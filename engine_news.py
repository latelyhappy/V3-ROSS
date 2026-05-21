import time
import threading
from datetime import datetime
import requests
import urllib.parse
import json
import yfinance as yf
import pytz

import shared_state
from config import TZ_NY, TZ_TW

# 🚀 擴充：加入低價妖股專屬的高勝率詞彙 (解決評分一直為 0 的問題)
DEFAULT_CATALYSTS = {
    "FDA": 10, "APPROVAL": 9, "MERGER": 8, "ACQUISITION": 8,
    "EARNINGS": 6, "REVENUE": 5, "BEATS": 7, "MISSES": -6,
    "OFFERING": -8, "BANKRUPTCY": -10, "BUYOUT": 9, "PATENT": 6,
    "COMPLIANCE": 8, "REGAINS": 7, "SPLIT": -5, "REVERSE SPLIT": -8,
    "AGREEMENT": 5, "PARTNERSHIP": 6, "TRIAL": 6, "DATA": 5,
    "PHASE": 6, "LAUNCH": 7, "CONTRACT": 8, "AWARD": 7
}

def load_catalysts():
    try:
        with open('catalysts.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            return {k.upper(): v for k, v in data.items()}
    except:
        return DEFAULT_CATALYSTS

def translate_to_zh(text):
    """Google 輕量級免費翻譯 API"""
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-TW&dt=t&q={urllib.parse.quote(text)}"
        response = requests.get(url, timeout=3).json()
        return "".join([x[0] for x in response[0]])
    except:
        return text

def finnhub_news_monitor_worker():
    print("🗞️ [NEWS] 啟動情報監控網 (yFinance 原生新聞 API + 妖股詞庫)...")
    catalysts = load_catalysts()
    
    while True:
        try:
            with shared_state.brain_lock:
                watch_list = shared_state.DYNAMIC_WATCHLIST.copy()
            
            if not watch_list:
                time.sleep(5)
                continue
            
            top_catalysts_list = []
            
            for sym in watch_list[:15]: 
                try:
                    # 🚀 放棄 RSS，改用 yfinance 官方原生新聞 API，獲取最新最快的情報！
                    news_data = yf.Ticker(sym).news
                    news_items = []
                    sym_max_score = 0
                    
                    for entry in news_data[:3]:
                        raw_title = entry.get("title", "")
                        link = entry.get("link", "")
                        
                        # 解析 yFinance 給的時間戳
                        pub_ts = entry.get("providerPublishTime", time.time())
                        pub_time = datetime.fromtimestamp(pub_ts, pytz.UTC).astimezone(TZ_TW)
                        
                        score = 0
                        elites = []
                        raw_upper = raw_title.upper()
                        
                        # 🚀 使用英文原文與擴充詞庫進行比對打分！
                        for kw, val in catalysts.items():
                            if kw in raw_upper:
                                score += val
                                elites.append(kw)
                        
                        zh_title = translate_to_zh(raw_title)
                        
                        news_items.append({
                            "time": pub_time.strftime("%m-%d %H:%M"),
                            "raw_title": raw_title,
                            "title": zh_title,
                            "link": link,
                            "score": score,
                            "elites": elites,
                            "is_today": pub_time.date() == datetime.now(TZ_TW).date()
                        })
                        
                        if score > sym_max_score:
                            sym_max_score = score
                    
                    if news_items:
                        with shared_state.brain_lock:
                            if sym not in shared_state.MASTER_BRAIN["details"]:
                                shared_state.MASTER_BRAIN["details"][sym] = {}
                            shared_state.MASTER_BRAIN["details"][sym]["NewsList"] = news_items
                        
                        top_catalysts_list.append({
                            "Code": sym,
                            "CatScore": sym_max_score,
                            "NewsList": news_items
                        })
                except Exception as inner_e:
                    pass
                    
            with shared_state.brain_lock:
                top_catalysts_list.sort(key=lambda x: x["CatScore"], reverse=True)
                shared_state.MASTER_BRAIN["top_catalysts"] = top_catalysts_list
                
        except Exception as e:
            print(f"⚠️ [NEWS] 情報網發生異常: {e}")
            
        time.sleep(20) # 每 20 秒刷新一輪，避免被封鎖

def get_live_trends():
    return []