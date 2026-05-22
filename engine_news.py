import time
import threading
from datetime import datetime, timedelta
import requests
import urllib.parse
import yfinance as yf
import pytz
import feedparser
from email.utils import parsedate_to_datetime

import shared_state
from config import TZ_NY, TZ_TW

def safe_escape(text):
    if not text: return ""
    return text.replace('"', '&quot;').replace("'", "&#39;")

def translate_to_zh(text):
    if not text: return ""
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-TW&dt=t&q={urllib.parse.quote(text)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"} 
        response = requests.get(url, headers=headers, timeout=4)
        if response.status_code == 200:
            return "".join([x[0] for x in response.json()[0]])
        return text 
    except:
        return text

def parse_rss_time(time_str):
    try:
        dt = parsedate_to_datetime(time_str)
        return dt.astimezone(TZ_TW) if dt.tzinfo else pytz.utc.localize(dt).astimezone(TZ_TW)
    except:
        return datetime.now(TZ_TW)

def finnhub_news_monitor_worker():
    print("🗞️ [NEWS] 啟動純淨情報網 (無評分 / 3日濾網 / 1分鐘極速更新)...")
    
    while True:
        try:
            with shared_state.brain_lock:
                watch_list = shared_state.DYNAMIC_WATCHLIST.copy()
            
            if not watch_list:
                time.sleep(5)
                continue
            
            top_news_list = []
            now_tw = datetime.now(TZ_TW)
            three_days_ago = now_tw - timedelta(days=3) # 🚀 3日濾網：只看最近3天
            
            for sym in watch_list[:15]: 
                try:
                    news_items = []
                    raw_news_entries = []
                    
                    # 優先使用 yFinance 官方 API 確保不被擋
                    try:
                        news_data = yf.Ticker(sym).news
                        for entry in news_data[:5]:
                            pub_ts = entry.get("providerPublishTime", time.time())
                            pub_time = datetime.fromtimestamp(pub_ts, pytz.UTC).astimezone(TZ_TW)
                            raw_news_entries.append({
                                "raw_title": entry.get("title", ""), 
                                "link": entry.get("link", ""),
                                "pub_time": pub_time
                            })
                    except:
                        # 備援使用 RSS
                        rss_url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
                        feed = feedparser.parse(rss_url)
                        for entry in feed.entries[:5]:
                            raw_news_entries.append({
                                "raw_title": entry.title, 
                                "link": entry.link, 
                                "pub_time": parse_rss_time(entry.published)
                            })
                            
                    for entry in raw_news_entries:
                        raw_title = entry["raw_title"]
                        pub_time = entry["pub_time"]
                        
                        if not raw_title: continue
                        
                        # 🚀 篩選：過濾掉超過 3 天的舊新聞
                        if pub_time < three_days_ago: continue
                        
                        zh_title = translate_to_zh(raw_title)
                        
                        # 🚀 判定是否為「今天」的新聞
                        is_today = pub_time.date() == now_tw.date()
                        
                        news_items.append({
                            "time": pub_time.strftime("%m-%d %H:%M"),
                            "timestamp": pub_time.timestamp(),
                            "raw_title": safe_escape(raw_title),
                            "title": safe_escape(zh_title),
                            "link": entry["link"],
                            "is_today": is_today
                        })
                    
                    # 依時間由新到舊排序
                    news_items.sort(key=lambda x: x["timestamp"], reverse=True)
                    
                    if news_items:
                        with shared_state.brain_lock:
                            if sym not in shared_state.MASTER_BRAIN["details"]:
                                shared_state.MASTER_BRAIN["details"][sym] = {}
                            shared_state.MASTER_BRAIN["details"][sym]["NewsList"] = news_items
                        
                        top_news_list.append({
                            "Code": sym,
                            "LatestTime": news_items[0]["timestamp"],
                            "NewsList": news_items
                        })
                except: pass
                    
            with shared_state.brain_lock:
                # 整個新聞雷達依照「誰的新聞最新」來排到最上面
                top_news_list.sort(key=lambda x: x["LatestTime"], reverse=True)
                shared_state.MASTER_BRAIN["top_catalysts"] = top_news_list
                
        except Exception as e:
            print(f"⚠️ [NEWS] 情報網發生異常: {e}")
            
        time.sleep(60) # 🚀 每 60 秒 (1分鐘) 搜索一次，既即時又安全不被封

def get_live_trends():
    return []