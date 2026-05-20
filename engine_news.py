import os
import time
import json
import threading
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from deep_translator import GoogleTranslator
from config import CATALYST_ARMORY_PATH, LEARNED_CATALYSTS_PATH, TRENDS_FILE_PATH, TRENDS_CACHE_TTL, FINNHUB_TOKEN, TZ_TW
from utils import convert_to_taiwan_time
import shared_state

def reload_armory():
    try:
        with open(CATALYST_ARMORY_PATH, 'r', encoding='utf-8') as f:
            shared_state.CATALYST_ARMORY = json.load(f)
    except: shared_state.CATALYST_ARMORY = {}

    try:
        if os.path.exists(LEARNED_CATALYSTS_PATH):
            with open(LEARNED_CATALYSTS_PATH, 'r', encoding='utf-8') as f:
                learned_data = json.load(f)
                if "THEMATIC_TRENDS" not in shared_state.CATALYST_ARMORY:
                    shared_state.CATALYST_ARMORY["THEMATIC_TRENDS"] = {}
                shared_state.CATALYST_ARMORY["THEMATIC_TRENDS"].update(learned_data)
    except: pass

def get_live_trends():
    now = time.time()
    if now - shared_state._last_trends_update < TRENDS_CACHE_TTL:
        return shared_state._cached_trends
    try:
        if os.path.exists(TRENDS_FILE_PATH):
            with open(TRENDS_FILE_PATH, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
                shared_state._cached_trends = {}
                for k, v in raw_data.items():
                    if isinstance(v, dict):
                        if "avg_impact_pct" not in v: v["avg_impact_pct"] = 0.0
                        shared_state._cached_trends[k] = v
                    else:
                        shared_state._cached_trends[k] = {"score": v, "count": 1, "avg_impact_pct": 0.0}
                shared_state._last_trends_update = now
        else: shared_state._cached_trends = {}
    except: pass
    return shared_state._cached_trends

def calculate_hft_score(headline, ticker=""):
    text = (headline or "").upper()
    total_score = 0
    elite_hits = []

    CORE_WORDS = ["WOLFPACK", "AFRL", "MARINE CORPS", "FDA APPROVAL", "FAST TRACK", "DEPARTMENT OF DEFENSE", "DOD"]
    for kw in CORE_WORDS:
        if kw in text:
            total_score += 100
            elite_hits.append("💎" + kw)

    reload_armory()
    for kw, score in shared_state.CATALYST_ARMORY.get("TOXIC_OFFERINGS", {}).items():
        if kw in text: total_score += score
    
    for kw, score in shared_state.CATALYST_ARMORY.get("INVERTED_TRAPS", {}).items():
        if kw in text:
            total_score += score
            elite_hits.append(kw)

    for kw, score in shared_state.CATALYST_ARMORY.get("MEGA_CATALYSTS", {}).items():
        if kw in text: total_score += score
        
    for kw, data in get_live_trends().items():
        if kw in text:
            score = data.get("score", 5)
            total_score += score
            if score >= 10 or data.get("count", 0) >= 3: elite_hits.append(kw)

    for cat in ["CLINICAL_SUCCESS", "COMPLIANCE_WINS", "THEMATIC_TRENDS"]: 
        for kw, score in shared_state.CATALYST_ARMORY.get(cat, {}).items():
            if kw in text: 
                total_score += score
                if score >= 8: elite_hits.append(kw)
                
    return total_score, False, elite_hits

def update_dynamic_catscore(cell):
    news_list = cell.get('NewsList', [])
    if not news_list:
        cell["CatScore"] = 0
        cell["IsTrap"] = False
        return
    
    news_list.sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
    latest_news = news_list[0]
    
    if latest_news.get('is_trap', False):
        cell["CatScore"] = -50
        cell["IsTrap"] = True
        cell["StickySignal"] = "💀 增發/毒藥陷阱"
        return
        
    best_score = max([n.get('score', 0) for n in news_list]) if news_list else 0
    has_trap = any([n.get('is_trap', False) for n in news_list])

    cell["CatScore"] = best_score
    cell["IsTrap"] = has_trap

def extract_top_catalysts():
    with shared_state.brain_lock:
        top_list = [d for d in shared_state.MASTER_BRAIN.get('details', {}).values() if d.get('NewsList', [])]
    try: return sorted(top_list, key=lambda x: (x['NewsList'][0].get('time', '00-00 00:00'), x.get('CatScore', 0)), reverse=True)
    except: return top_list

def fetch_and_score_news(ticker, cell, force=False):
    now = time.time()
    if not force and ticker in shared_state.news_cache and (now - shared_state.news_cache.get(ticker, 0) < 900): return
    shared_state.news_cache[ticker] = now
    try:
        url = f"[https://feeds.finance.yahoo.com/rss/2.0/headline?s=](https://feeds.finance.yahoo.com/rss/2.0/headline?s=){ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []
            now_tpe = datetime.now(TZ_TW)
            three_days_ago_tpe = now_tpe.date() - timedelta(days=3)
            
            with shared_state.brain_lock:
                finnhub_titles = {n['raw_title'] for n in cell.get('NewsList', []) if n.get('source') == 'Finnhub PR'}
            
            for item in root.findall('./channel/item')[:5]:
                dt_tpe = convert_to_taiwan_time(item.find('pubDate').text, source="yahoo")
                if dt_tpe.date() < three_days_ago_tpe: continue
                raw_t = item.find('title').text
                if raw_t in finnhub_titles: continue 
        
                score, is_trap, elites = calculate_hft_score(raw_t, ticker)
                
                articles.append({
                    "ticker": ticker, "title": raw_t, "raw_title": raw_t, "link": item.find('link').text, 
                    "time": dt_tpe.strftime("%m-%d %H:%M"), "is_today": (dt_tpe.date() == now_tpe.date()), 
                    "score": score, "elites": elites, "source": "Yahoo"
                })
            
            with shared_state.brain_lock:
                combined = articles + [n for n in cell.get('NewsList', []) if n['raw_title'] not in {a['raw_title'] for a in articles}]
                combined.sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
                cell["NewsList"] = combined[:10]
                update_dynamic_catscore(cell)
                shared_state.MASTER_BRAIN["top_catalysts"] = extract_top_catalysts()
    except Exception as e: pass

def finnhub_news_monitor_worker():
    if not FINNHUB_TOKEN: return
    time.sleep(10)
    seen_news_urls = set()
    while True:
        try:
            with shared_state.brain_lock: current_watchlist = shared_state.DYNAMIC_WATCHLIST.copy()
            if not current_watchlist: time.sleep(10); continue
            end_date = datetime.now(TZ_TW).strftime('%Y-%m-%d')
            start_date = (datetime.now(TZ_TW) - timedelta(days=3)).strftime('%Y-%m-%d')

            for ticker in current_watchlist:
                try:
                    url = f"[https://finnhub.io/api/v1/company-news?symbol=](https://finnhub.io/api/v1/company-news?symbol=){ticker}&from={start_date}&to={end_date}&token={FINNHUB_TOKEN}"
                    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    if res.status_code == 200:
                        for art in res.json()[:5]:
                            art_url = art.get('url', '')
                            art_headline = art.get('headline', '')
                            if not art_url or not art_headline or art_url in seen_news_urls: continue
                            
                            dt_tpe = convert_to_taiwan_time(art.get('datetime', time.time()), source="finnhub")
                            score, is_trap, elites = calculate_hft_score(art_headline, ticker)
                            
                            with shared_state.brain_lock:
                                cell = shared_state.MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                                if not any(n['link'] == art_url for n in cell['NewsList']):
                                    cell['NewsList'].append({
                                        "ticker": ticker, "title": art_headline, "raw_title": art_headline, "link": art_url,
                                        "time": dt_tpe.strftime("%m-%d %H:%M"), "is_today": (dt_tpe.date() == datetime.now(TZ_TW).date()),
                                        "score": int(score * 1.2), "elites": elites, "source": "Finnhub PR"
                                    })
                                    cell['NewsList'].sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
                                    cell['NewsList'] = cell['NewsList'][:10]
                                    update_dynamic_catscore(cell)
                                    shared_state.MASTER_BRAIN["top_catalysts"] = extract_top_catalysts()
                            seen_news_urls.add(art_url) 
                            if len(seen_news_urls) > 1000: seen_news_urls.pop() 
                except: pass
                time.sleep(1.5)
        except: time.sleep(30)