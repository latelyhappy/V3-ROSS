import time
import threading
from datetime import datetime
import requests
import urllib.parse
import json
import yfinance as yf
import pytz
import feedparser
from email.utils import parsedate_to_datetime

import shared_state
from config import TZ_NY, TZ_TW

DEFAULT_CATALYSTS = {
    "FDA": 10, "APPROVAL": 9, "MERGER": 8, "ACQUISITION": 8,
    "EARNINGS": 6, "REVENUE": 5, "BEATS": 7, "MISSES": -6,
    "OFFERING": -8, "BANKRUPTCY": -10, "BUYOUT": 9, "PATENT": 6,
    "COMPLIANCE": 8, "REGAINS": 7, "SPLIT": -5, "REVERSE SPLIT": -8,
    "AGREEMENT": 5, "PARTNERSHIP": 6, "CONTRACT": 8
}

def load_catalysts():
    try:
        with open('catalysts.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            return {k.upper(): v for k, v in data.items()}
    except:
        return DEFAULT_CATALYSTS

def safe_escape(text):
    if not text: return ""
    return text.replace('"', '&quot;').replace("'", "&#39;")

def translate_to_zh(text):
    if not text: return ""
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-TW&dt=t&q={urllib.parse.quote(text)}"
        headers = {"User-Agent": "Mozilla/5.0"} 
        response = requests.get(url, headers=headers, timeout=4)
        if response.status_code == 200:
            return "".join([x[0] for x in response.json()[0]])
        return text 
    except:
        return text

def parse_rss_time(time_str):
    try:
        dt = parsedate_to_datetime(time_str)
        return dt.astimezone(TZ_TW) if dt.tzinfo else TZ_NY.localize(dt).astimezone(TZ_TW)
    except:
        return datetime.now(TZ_TW)

def finnhub_news_monitor_worker():
    print("🗞️ [NEWS] 啟動雙核防斷裂情報網...")
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
                    news_items = []
                    sym_max_score = 0
                    raw_news_entries = []
                    
                    # 雙核抓取策略
                    rss_url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
                    feed = feedparser.parse(rss_url)
                    if feed.entries:
                        for entry in feed.entries[:3]:
                            raw_news_entries.append({
                                "raw_title": entry.title, "link": entry.link, "pub_time": parse_rss_time(entry.published)
                            })
                    else:
                        try:
                            news_data = yf.Ticker(sym).news
                            for entry in news_data[:3]:
                                pub_ts = entry.get("providerPublishTime", time.time())
                                raw_news_entries.append({
                                    "raw_title": entry.get("title", ""), "link": entry.get("link", ""),
                                    "pub_time": datetime.fromtimestamp(pub_ts, pytz.UTC).astimezone(TZ_TW)
                                })
                        except: pass
                            
                    for entry in raw_news_entries:
                        raw_title = entry["raw_title"]
                        if not raw_title: continue
                        
                        score = 0
                        elites = []
                        raw_upper = raw_title.upper()
                        
                        for kw, val in catalysts.items():
                            if kw in raw_upper:
                                score += val
                                elites.append(kw)
                        
                        zh_title = translate_to_zh(raw_title)
                        
                        news_items.append({
                            "time": entry["pub_time"].strftime("%m-%d %H:%M"),
                            "raw_title": safe_escape(raw_title),
                            "title": safe_escape(zh_title),
                            "link": entry["link"],
                            "score": score,
                            "elites": elites,
                            "is_today": entry["pub_time"].date() == datetime.now(TZ_TW).date()
                        })
                        
                        if score > sym_max_score:
                            sym_max_score = score
                    
                    if news_items:
                        with shared_state.brain_lock:
                            if sym not in shared_state.MASTER_BRAIN["details"]:
                                shared_state.MASTER_BRAIN["details"][sym] = {}
                            shared_state.MASTER_BRAIN["details"][sym]["NewsList"] = news_items
                        
                        top_catalysts_list.append({
                            "Code": sym, "CatScore": sym_max_score, "NewsList": news_items
                        })
                except: pass
                    
            with shared_state.brain_lock:
                top_catalysts_list.sort(key=lambda x: x["CatScore"], reverse=True)
                shared_state.MASTER_BRAIN["top_catalysts"] = top_catalysts_list
                
        except Exception as e:
            print(f"⚠️ [NEWS] 情報網異常: {e}")
            
        time.sleep(15)

def get_live_trends():
    return []