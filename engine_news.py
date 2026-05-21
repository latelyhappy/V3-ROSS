import os
import time
import json
import threading
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from deep_translator import GoogleTranslator

# 🚀 匯入 V59.0 解耦模組
import shared_state
from config import (
    FINNHUB_TOKEN, TZ_TW, TZ_NY, TRENDS_FILE_PATH, TRENDS_CACHE_TTL,
    CATALYST_ARMORY_PATH, LEARNED_CATALYSTS_PATH
)
from utils import convert_to_taiwan_time

# ==========================================
# 🧠 戰術軍火庫與趨勢載入
# ==========================================
def reload_armory():
    """載入人工與 AI 學習的雙重催化劑軍火庫"""
    try:
        if os.path.exists(CATALYST_ARMORY_PATH):
            with open(CATALYST_ARMORY_PATH, 'r', encoding='utf-8') as f:
                shared_state.CATALYST_ARMORY = json.load(f)
        else:
            shared_state.CATALYST_ARMORY = {}
    except Exception as e:
        print(f"⚠️ 讀取 catalysts.json 失敗: {e}")
        shared_state.CATALYST_ARMORY = {}

    try:
        # 💡 V59.0：合併 AI 學習軍火庫，保護人工設定不被覆寫
        if os.path.exists(LEARNED_CATALYSTS_PATH):
            with open(LEARNED_CATALYSTS_PATH, 'r', encoding='utf-8') as f:
                learned_data = json.load(f)
                if "THEMATIC_TRENDS" not in shared_state.CATALYST_ARMORY:
                    shared_state.CATALYST_ARMORY["THEMATIC_TRENDS"] = {}
                shared_state.CATALYST_ARMORY["THEMATIC_TRENDS"].update(learned_data)
    except: pass

# 初始化執行一次
reload_armory()

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
        else:
            shared_state._cached_trends = {}
    except: pass
    return shared_state._cached_trends

# ==========================================
# ⚖️ 情報評分與處理引擎
# ==========================================
def is_news_echo(new_title, new_dt_tpe, existing_news_list):
    """判斷是否為重複發布的新聞 (Echo)"""
    new_words = set(re.findall(r'\b[A-Z]{3,}\b', new_title.upper()))
    if not new_words: return False
    for n in existing_news_list:
        try:
            old_dt_str = f"{new_dt_tpe.year}-{n['time']}"
            old_dt_tpe = datetime.strptime(old_dt_str, "%Y-%m-%d %H:%M")
            old_dt_tpe = TZ_TW.localize(old_dt_tpe)
            if abs((new_dt_tpe - old_dt_tpe).total_seconds()) <= 10800: # 3小時內
                old_words = set(re.findall(r'\b[A-Z]{3,}\b', n['raw_title'].upper()))
                if old_words:
                    overlap = len(new_words.intersection(old_words)) / len(new_words.union(old_words))
                    if overlap >= 0.6: return True
        except: pass
    return False

def calculate_hft_score(headline, ticker=""):
    """使用 CatScore 演算法對新聞進行評分"""
    text = (headline or "").upper()
    total_score = 0
    elite_hits = []

    CORE_WORDS = ["WOLFPACK", "AFRL", "MARINE CORPS", "FDA APPROVAL", "FAST TRACK", "DEPARTMENT OF DEFENSE", "DOD"]
    for kw in CORE_WORDS:
        if kw in text:
            total_score += 100
            elite_hits.append("💎" + kw)

    reload_armory()
    is_toxic = False
    
    for kw, score in shared_state.CATALYST_ARMORY.get("TOXIC_OFFERINGS", {}).items():
        if kw in text: 
            total_score += score
            is_toxic = True
    
    for kw, score in shared_state.CATALYST_ARMORY.get("INVERTED_TRAPS", {}).items():
        if kw in text:
            total_score += score
            is_toxic = False 
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
                
    return total_score, is_toxic, elite_hits

def update_dynamic_catscore(cell):
    """更新股票的最高情報評分"""
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
    """提取排名前段的情報清單"""
    with shared_state.brain_lock:
        top_list = [d for d in shared_state.MASTER_BRAIN.get('details', {}).values() if d.get('NewsList', [])]
    try:
        return sorted(top_list, key=lambda x: (x['NewsList'][0].get('time', '00-00 00:00'), x.get('CatScore', 0)), reverse=True)
    except:
        return top_list

def background_translate_worker(ticker, en_headline):
    """背景翻譯 Worker"""
    try:
        zh_text = GoogleTranslator(source='auto', target='zh-TW').translate(en_headline)
        with shared_state.brain_lock:
            for t in ticker.split(','):
                if t in shared_state.MASTER_BRAIN['details']:
                    for article in shared_state.MASTER_BRAIN['details'][t].get('NewsList', []):
                        if article.get('raw_title') == en_headline and article['title'] == "⏳ 翻譯中...": 
                            article['title'] = zh_text
    except: pass

def check_sec_fatal_traps(ticker):
    """SEC Edgar 即時陷阱偵測"""
    headers = {"User-Agent": "SniperQuantSystem_V59 AdminContact@yourdomain.com", "Accept-Encoding": "gzip, deflate"}
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=&output=atom"
    try:
        time.sleep(0.5) 
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for entry in root.findall('{http://www.w3.org/2005/Atom}entry')[:3]:
                title_elem = entry.find('{http://www.w3.org/2005/Atom}title')
                if title_elem is not None and any(trap in title_elem.text.upper() for trap in ['S-1', 'S-3', 'F-1', 'F-3']): 
                    return True
        return False
    except: return False

# ==========================================
# 📡 爬蟲與情報網
# ==========================================
def fetch_and_score_news(ticker, cell, force=False):
    """從 Yahoo RSS 抓取即時新聞並進行評分"""
    now = time.time()
    if not force and ticker in shared_state.news_cache and (now - shared_state.news_cache.get(ticker, 0) < 900): 
        return
    shared_state.news_cache[ticker] = now
    
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            articles = []
            all_elites = set()
            has_trap = False
            now_tpe = datetime.now(TZ_TW)
            three_days_ago_tpe = now_tpe.date() - timedelta(days=3)
            
            with shared_state.brain_lock:
                finnhub_titles = {n['raw_title'] for n in cell.get('NewsList', []) if n.get('source') == 'Finnhub PR'}
            
            for item in root.findall('./channel/item')[:5]:
                dt_tpe = convert_to_taiwan_time(item.find('pubDate').text, source="yahoo")
                if dt_tpe.date() < three_days_ago_tpe: continue
                raw_t = item.find('title').text
                if raw_t in finnhub_titles: continue 
        
                if is_news_echo(raw_t, dt_tpe, cell.get('NewsList', [])):
                    score, is_trap, elites = calculate_hft_score(raw_t, ticker)
                    raw_t = "[Echo] " + raw_t
                else:
                    score, is_trap, elites = calculate_hft_score(raw_t, ticker)
                
                if is_trap: has_trap = True
                all_elites.update(elites)
                if any("💎" in e for e in elites): raw_t = "[💎 核心情報] " + raw_t

                articles.append({
                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": raw_t,
                    "link": item.find('link').text, 
                    "time": dt_tpe.strftime("%m-%d %H:%M"),
                    "is_today": (dt_tpe.date() == now_tpe.date()), 
                    "score": score, "elites": list(elites), "source": "Yahoo",
                    "is_trap": is_trap
                })
            
            with shared_state.brain_lock:
                combined = articles + [n for n in cell.get('NewsList', []) if n['raw_title'] not in {a['raw_title'] for a in articles}]
                combined.sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
                cell["NewsList"] = combined[:10]
                update_dynamic_catscore(cell)
                cell["IsTrap"] = cell.get("IsTrap", False) or has_trap 
                cell["HasNews"] = len(cell["NewsList"]) > 0 
                shared_state.MASTER_BRAIN["top_catalysts"] = extract_top_catalysts()

            if articles:
                for art in articles:
                    if art['title'] == "⏳ 翻譯中...":
                        threading.Thread(target=background_translate_worker, args=(ticker, art['raw_title']), daemon=True).start()
                        time.sleep(0.5) 
    except Exception as e: pass

def finnhub_news_monitor_worker():
    """背景持續監控 Finnhub 高速新聞 API"""
    if not FINNHUB_TOKEN: return
    time.sleep(10)
    seen_news_urls = set()
    while True:
        try:
            with shared_state.brain_lock: 
                current_watchlist = shared_state.DYNAMIC_WATCHLIST.copy()
            if not current_watchlist: 
                time.sleep(10)
                continue
                
            end_date = datetime.now(TZ_NY).strftime('%Y-%m-%d')
            start_date = (datetime.now(TZ_NY) - timedelta(days=3)).strftime('%Y-%m-%d')

            for ticker in current_watchlist:
                try:
                    url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={start_date}&to={end_date}&token={FINNHUB_TOKEN}"
                    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    if res.status_code == 200:
                        for art in res.json()[:5]:
                            art_url = art.get('url', '')
                            art_headline = art.get('headline', '')
                            if not art_url or not art_headline or art_url in seen_news_urls: continue
                            
                            dt_tpe = convert_to_taiwan_time(art.get('datetime', time.time()), source="finnhub")
                           
                            with shared_state.brain_lock:
                                cell = shared_state.MASTER_BRAIN["details"].setdefault(ticker, {"NewsList": []})
                                if is_news_echo(art_headline, dt_tpe, cell.get('NewsList', [])):
                                    score, is_trap, elites = calculate_hft_score(art_headline, ticker)
                                    art_headline = "[Echo] " + art_headline
                                else:
                                    score, is_trap, elites = calculate_hft_score(art_headline, ticker)

                                score = int(score * 1.2)
                                if any("💎" in e for e in elites): art_headline = "[💎 核心情報] " + art_headline

                                new_article = {
                                    "ticker": ticker, "title": "⏳ 翻譯中...", "raw_title": art_headline, "link": art_url,
                                    "time": dt_tpe.strftime("%m-%d %H:%M"),
                                    "is_today": (dt_tpe.date() == datetime.now(TZ_TW).date()),
                                    "score": score, "elites": list(elites), "source": "Finnhub PR",
                                    "is_trap": is_trap
                                }

                                if not any(n['link'] == art_url for n in cell['NewsList']):
                                    cell['NewsList'].append(new_article)
                                    cell['NewsList'].sort(key=lambda x: x.get('time', '00-00 00:00'), reverse=True)
                                    cell['NewsList'] = cell['NewsList'][:10]
                                    update_dynamic_catscore(cell)
                                    cell["IsTrap"] = cell.get("IsTrap", False) or is_trap
                                    cell["HasNews"] = True
                                    shared_state.MASTER_BRAIN["top_catalysts"] = extract_top_catalysts()
                                    threading.Thread(target=background_translate_worker, args=(ticker, art_headline), daemon=True).start()
                                    
                            seen_news_urls.add(art_url) 
                            if len(seen_news_urls) > 1000: seen_news_urls.pop() 
                except: pass
                time.sleep(1.5) # 防限流
        except: time.sleep(30)