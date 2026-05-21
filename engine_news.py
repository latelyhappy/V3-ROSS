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

# 妖股與正規軍複合高勝率詞庫
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

def safe_escape(text):
    """🚀 盲點一修復：安全脫逸！防止單雙引號把前端 HTML 標籤切斷導致新聞隱形"""
    if not text: return ""
    return text.replace('"', '&quot;').replace("'", "&#39;")

def translate_to_zh(text):
    """🚀 盲點一修復：加入安全備援，如果 Google 翻譯擋人，直接回傳英文原文，絕不當機！"""
    if not text: return ""
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-TW&dt=t&q={urllib.parse.quote(text)}"
        headers = {"User-Agent": "Mozilla/5.0"} # 偽裝瀏覽器防擋
        response = requests.get(url, headers=headers, timeout=4)
        if response.status_code == 200:
            data = response.json()
            return "".join([x[0] for x in data[0]])
        return text 
    except:
        return text

def finnhub_news_monitor_worker():
    print("🗞️ [NEWS] 啟動情報監控網 (防斷裂裝甲與備援機制啟動)...")
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
                    news_data = yf.Ticker(sym).news
                    news_items = []
                    sym_max_score = 0
                    
                    for entry in news_data[:3]:
                        raw_title = entry.get("title", "")
                        link = entry.get("link", "")
                        
                        if not raw_title:
                            continue
                        
                        pub_ts = entry.get("providerPublishTime", time.time())
                        pub_time = datetime.fromtimestamp(pub_ts, pytz.UTC).astimezone(TZ_TW)
                        
                        score = 0
                        elites = []
                        raw_upper = raw_title.upper()
                        
                        # 🚀 AI 評分機制：確保用純大寫的英文原文進行碰撞比對
                        for kw, val in catalysts.items():
                            if kw in raw_upper:
                                score += val
                                elites.append(kw)
                        
                        # 翻譯與安全過濾
                        zh_title = translate_to_zh(raw_title)
                        safe_raw_title = safe_escape(raw_title)
                        safe_zh_title = safe_escape(zh_title)
                        
                        news_items.append({
                            "time": pub_time.strftime("%m-%d %H:%M"),
                            "raw_title": safe_raw_title,
                            "title": safe_zh_title,
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
            
        time.sleep(20)

def get_live_trends():
    return []