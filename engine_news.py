import time
import threading
from datetime import datetime
import feedparser
import requests
import urllib.parse
import json

import shared_state
from config import TZ_NY, TZ_TW
from utils import convert_to_taiwan_time

# 內建高勝率戰略詞庫 (防呆備用，若 catalysts.json 不存在時使用)
DEFAULT_CATALYSTS = {
    "FDA": 10, "APPROVAL": 9, "MERGER": 8, "ACQUISITION": 8,
    "EARNINGS": 6, "REVENUE": 5, "BEATS": 7, "MISSES": -6,
    "OFFERING": -8, "BANKRUPTCY": -10, "BUYOUT": 9, "PATENT": 6,
    "GUIDANCE": 7, "TRIAL": 6, "AGREEMENT": 5, "PARTNERSHIP": 6
}

def load_catalysts():
    try:
        with open('catalysts.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 強制轉大寫，確保比對精準
            return {k.upper(): v for k, v in data.items()}
    except:
        return DEFAULT_CATALYSTS

def translate_to_zh(text):
    """利用 Google Translate API 進行極速免費翻譯"""
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-TW&dt=t&q={urllib.parse.quote(text)}"
        response = requests.get(url, timeout=3).json()
        return "".join([x[0] for x in response[0]])
    except:
        return text

def finnhub_news_monitor_worker():
    print("🗞️ [NEWS] 啟動情報監控網 (英文原生 AI 評分 + 中文翻譯模組)...")
    catalysts = load_catalysts()
    
    while True:
        try:
            with shared_state.brain_lock:
                watch_list = shared_state.DYNAMIC_WATCHLIST.copy()
            
            if not watch_list:
                time.sleep(5)
                continue
            
            top_catalysts_list = []
            
            for sym in watch_list[:15]: # 鎖定前 15 大強勢股
                rss_url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
                feed = feedparser.parse(rss_url)
                
                news_items = []
                sym_max_score = 0
                
                for entry in feed.entries[:3]:
                    raw_title = entry.title
                    link = entry.link
                    pub_time = convert_to_taiwan_time(entry.published, source="yahoo")
                    
                    # 🚀 盲區二修復：在翻譯前，先用「英文原文」進行 AI 評分比對！
                    score = 0
                    elites = []
                    raw_upper = raw_title.upper()
                    
                    for kw, val in catalysts.items():
                        if kw in raw_upper:
                            score += val
                            elites.append(kw)
                    
                    # 評分完畢後，再翻譯成中文給指揮官看
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
            
            with shared_state.brain_lock:
                top_catalysts_list.sort(key=lambda x: x["CatScore"], reverse=True)
                shared_state.MASTER_BRAIN["top_catalysts"] = top_catalysts_list
                
        except Exception as e:
            print(f"⚠️ [NEWS] 情報網發生異常: {e}")
            
        time.sleep(20) # 每 20 秒刷新一輪新聞，不卡資源

def get_live_trends():
    return []