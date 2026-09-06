import os
import json
import requests
import hashlib
import numpy as np
from datetime import datetime, timedelta
import pytz
import re
import xml.etree.ElementTree as ET

# ============================================
# KONFIGURACJA
# ============================================

# Tokeny
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# Pliki
SIGNALS_FILE = 'signals_history.json'
REPORT_FILE = 'daily_report.json'
MODEL_FILE = 'model_weights.json'
AI_MEMORY_FILE = 'ai_memory.json'
WEEKLY_REPORT_FILE = 'weekly_report.json'
MONTHLY_REPORT_FILE = 'monthly_report.json'
STRATEGY_STATS_FILE = 'strategy_stats.json'
TIMEFRAME_WEIGHTS_FILE = 'timeframe_weights.json'

TIMEZONE = pytz.timezone('Europe/Warsaw')

# ============================================
# RYNKI
# ============================================
MARKETS = {
    'DAX': {'symbol': '^GDAXI', 'type': 'index', 'session': 'EU'},
    'S&P500': {'symbol': '^GSPC', 'type': 'index', 'session': 'US'},
    'NASDAQ': {'symbol': '^IXIC', 'type': 'index', 'session': 'US'},
    'EUR/USD': {'symbol': 'EURUSD=X', 'type': 'forex', 'session': 'EU_US'},
    'GOLD': {'symbol': 'GC=F', 'type': 'commodity', 'session': 'US'},
    'OIL WTI': {'symbol': 'CL=F', 'type': 'commodity', 'session': 'US'},
    'BITCOIN': {'symbol': 'BTC-USD', 'type': 'crypto', 'session': '24_7'},
    'ETHEREUM': {'symbol': 'ETH-USD', 'type': 'crypto', 'session': '24_7'},
    'SOLANA': {'symbol': 'SOL-USD', 'type': 'crypto', 'session': '24_7'},
    'APPLE': {'symbol': 'AAPL', 'type': 'stock', 'session': 'US'},
    'MICROSOFT': {'symbol': 'MSFT', 'type': 'stock', 'session': 'US'},
    'NVIDIA': {'symbol': 'NVDA', 'type': 'stock', 'session': 'US'},
    'TESLA': {'symbol': 'TSLA', 'type': 'stock', 'session': 'US'},
    'AMAZON': {'symbol': 'AMZN', 'type': 'stock', 'session': 'US'},
    'META': {'symbol': 'META', 'type': 'stock', 'session': 'US'},
    'GOOGLE': {'symbol': 'GOOGL', 'type': 'stock', 'session': 'US'},
}

# Interwały do analizy wielointerwałowej
TIMEFRAMES = {
    '5m': {'interval': '5m', 'range': '1d', 'default_weight': 0.15},
    '15m': {'interval': '15m', 'range': '1d', 'default_weight': 0.20},
    '1h': {'interval': '60m', 'range': '5d', 'default_weight': 0.25},
    '4h': {'interval': '60m', 'range': '1mo', 'default_weight': 0.15},
    '1d': {'interval': '1d', 'range': '3mo', 'default_weight': 0.25},
}

# Kraje i zapytania dla newsów
COUNTRY_NEWS = {
    'DAX': {'country': 'Germany', 'query_pl': 'Niemcy gospodarka DAX', 'query_en': 'Germany economy DAX'},
    'S&P500': {'country': 'USA', 'query_pl': 'USA gospodarka Wall Street', 'query_en': 'US economy Wall Street'},
    'NASDAQ': {'country': 'USA', 'query_pl': 'USA technologia giełda', 'query_en': 'US tech stocks'},
    'EUR/USD': {'country': 'EU', 'query_pl': 'strefa euro ECB', 'query_en': 'Eurozone ECB'},
    'GOLD': {'country': 'Global', 'query_pl': 'złoto cena', 'query_en': 'gold price'},
    'OIL WTI': {'country': 'Global', 'query_pl': 'ropa naftowa cena', 'query_en': 'oil price OPEC'},
    'BITCOIN': {'country': 'Global', 'query_pl': 'bitcoin kryptowaluty', 'query_en': 'bitcoin crypto'},
    'ETHEREUM': {'country': 'Global', 'query_pl': 'ethereum kryptowaluty', 'query_en': 'ethereum crypto'},
    'SOLANA': {'country': 'Global', 'query_pl': 'solana kryptowaluty', 'query_en': 'solana crypto'},
    'APPLE': {'country': 'USA', 'query_pl': 'Apple akcje', 'query_en': 'Apple stock'},
    'MICROSOFT': {'country': 'USA', 'query_pl': 'Microsoft akcje', 'query_en': 'Microsoft stock'},
    'NVIDIA': {'country': 'USA', 'query_pl': 'Nvidia akcje', 'query_en': 'Nvidia stock'},
    'TESLA': {'country': 'USA', 'query_pl': 'Tesla akcje', 'query_en': 'Tesla stock'},
    'AMAZON': {'country': 'USA', 'query_pl': 'Amazon akcje', 'query_en': 'Amazon stock'},
    'META': {'country': 'USA', 'query_pl': 'Meta akcje', 'query_en': 'Meta stock'},
    'GOOGLE': {'country': 'USA', 'query_pl': 'Google akcje', 'query_en': 'Google stock'},
}

# Kategorie newsów światowych
GLOBAL_NEWS_QUERIES = {
    'geopolityka': {'query_pl': 'wojna konflikt geopolityka', 'query_en': 'war conflict geopolitics'},
    'ekonomia': {'query_pl': 'kryzys gospodarka inflacja', 'query_en': 'economic crisis inflation'},
    'banki_centralne': {'query_pl': 'bank centralny stopy procentowe', 'query_en': 'central bank interest rates'},
    'polska': {'query_pl': 'Polska gospodarka NBP', 'query_en': 'Poland economy NBP'},
    'usa': {'query_pl': 'USA gospodarka Fed', 'query_en': 'US economy Fed'},
    'europa': {'query_pl': 'Europa gospodarka ECB', 'query_en': 'Europe economy ECB'},
}

# ============================================
# KLASY
# ============================================

class AIMemory:
    """Pamięć AI - zapisuje lekcje i analizy"""
    def __init__(self, file_path=AI_MEMORY_FILE):
        self.file_path = file_path
        self.memory = self.load()
    
    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except:
            pass
        return {
            'lessons': [],
            'timeframe_analysis': {},
            'divergence_history': {},
            'news_sentiment_history': {},
        }
    
    def save(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.memory, f, indent=2, ensure_ascii=False)
        except:
            pass
    
    def add_lesson(self, lesson):
        self.memory['lessons'].append({
            'timestamp': datetime.now(pytz.utc).isoformat(),
            'lesson': lesson
        })
        self.memory['lessons'] = self.memory['lessons'][-500:]
        self.save()
    
    def get_context(self):
        if not self.memory['lessons']:
            return "Brak wcześniejszych lekcji."
        lessons_text = "\n".join([
            f"- {lesson['lesson'][:200]}" 
            for lesson in self.memory['lessons'][-15:]
        ])
        return f"Dotychczasowe lekcje:\n{lessons_text}"
    
    def add_news_sentiment(self, market_name, sentiment_data):
        if market_name not in self.memory['news_sentiment_history']:
            self.memory['news_sentiment_history'][market_name] = []
        self.memory['news_sentiment_history'][market_name].append({
            'timestamp': datetime.now(pytz.utc).isoformat(),
            'data': sentiment_data
        })
        self.memory['news_sentiment_history'][market_name] = self.memory['news_sentiment_history'][market_name][-100:]
        self.save()


class TimeframeWeights:
    """Auto-adaptacyjne wagi interwałów"""
    def __init__(self, file_path=TIMEFRAME_WEIGHTS_FILE):
        self.file_path = file_path
        self.weights = self.load()
    
    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    return json.load(f)
        except:
            pass
        return {tf: config['default_weight'] for tf, config in TIMEFRAMES.items()}
    
    def save(self):
        try:
            with open(self.file_path, 'w') as f:
                json.dump(self.weights, f, indent=2)
        except:
            pass
    
    def get_weights(self):
        return self.weights


class SignalManager:
    """Zarządzanie sygnałami i zapobieganie duplikatom"""
    def __init__(self, file_path=SIGNALS_FILE):
        self.file_path = file_path
        self.signals = self.load_signals()
    
    def load_signals(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content.strip():
                        return json.loads(content)
        except:
            pass
        return {}
    
    def save_signals(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.signals, f, indent=2, ensure_ascii=False)
        except:
            pass
    
    def should_send_signal(self, signal):
        key = f"{signal['name']}_{signal['direction']}"
        if key not in self.signals:
            self.signals[key] = signal
            self.signals[key]['timestamp'] = datetime.now(pytz.utc).isoformat()
            self.save_signals()
            return True
        previous = self.signals[key]
        price_diff = abs(signal['entry'] - previous['entry']) / previous['entry'] * 100
        prev_time = datetime.fromisoformat(previous['timestamp'])
        time_diff = datetime.now(pytz.utc) - prev_time
        minutes_diff = time_diff.total_seconds() / 60
        if price_diff >= 0.3 or minutes_diff >= 30:
            self.signals[key] = signal
            self.signals[key]['timestamp'] = datetime.now(pytz.utc).isoformat()
            self.save_signals()
            return True
        return False

# ============================================
# FUNKCJE POMOCNICZE
# ============================================

def is_weekend():
    return datetime.now(TIMEZONE).weekday() >= 5

def is_friday_evening():
    now = datetime.now(TIMEZONE)
    return now.weekday() == 4 and now.hour == 19 and now.minute < 10

def is_monthly_report_time():
    now = datetime.now(TIMEZONE)
    return now.day == 1 and now.hour == 8 and now.minute < 10

def is_morning_sentiment_time():
    now = datetime.now(TIMEZONE)
    return now.hour == 8 and 10 <= now.minute < 20

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
        requests.post(url, json=payload, timeout=10)
        return True
    except Exception as e:
        print(f"Błąd Telegram: {e}")
        return False

def get_market_data(symbol, interval='15m', range_period='1d'):
    """Pobierz dane rynkowe z Yahoo Finance"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {'interval': interval, 'range': range_period}
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        data = response.json()
        result = data['chart']['result'][0]
        quotes = result['indicators']['quote'][0]
        prices = [p for p in quotes['close'] if p is not None]
        highs = [h for h in quotes['high'] if h is not None]
        lows = [l for l in quotes['low'] if l is not None]
        volumes = [v for v in quotes['volume'] if v is not None]
        opens = [o for o in quotes['open'] if o is not None]
        return {
            'prices': prices,
            'highs': highs,
            'lows': lows,
            'volumes': volumes,
            'opens': opens
        }
    except Exception as e:
        print(f"Błąd pobierania {symbol}: {e}")
        return None

def calculate_indicators(data):
    """Oblicz wskaźniki techniczne"""
    if not data or len(data['prices']) < 50:
        return None
    
    prices = data['prices']
    highs = data['highs']
    lows = data['lows']
    current_price = prices[-1]
    
    def sma(arr, period):
        if len(arr) < period: return None
        return sum(arr[-period:]) / period
    
    def ema(arr, period):
        if len(arr) < period: return None
        mult = 2/(period+1)
        e = arr[0]
        for x in arr[1:]:
            e = (x-e)*mult + e
        return e
    
    def rsi(arr, period=14):
        if len(arr) < period+1: return 50
        gains, losses = [], []
        for i in range(1, len(arr)):
            change = arr[i]-arr[i-1]
            gains.append(max(0, change))
            losses.append(max(0, -change))
        avg_gain = sum(gains[-period:])/period
        avg_loss = sum(losses[-period:])/period
        if avg_loss == 0: return 100
        rs = avg_gain/avg_loss
        return 100 - (100/(1+rs))
    
    def macd(arr):
        e12 = ema(arr, 12)
        e26 = ema(arr, 26)
        if e12 is None or e26 is None: return 0, 0
        return e12 - e26, e12 - e26
    
    def bollinger(arr, period=20):
        if len(arr) < period: return None, None, None
        sma_val = sum(arr[-period:])/period
        std = (sum([(x-sma_val)**2 for x in arr[-period:]])/period)**0.5
        return sma_val+2*std, sma_val, sma_val-2*std
    
    def stochastic(highs, lows, closes, period=14):
        if len(closes) < period: return 50, 50
        hh = max(highs[-period:])
        ll = min(lows[-period:])
        k = 100*(closes[-1]-ll)/(hh-ll) if hh != ll else 50
        return k, 50
    
    def atr(highs, lows, closes, period=14):
        if len(closes) < period+1: return 0
        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            trs.append(tr)
        return sum(trs[-period:])/period
    
    sma20 = sma(prices, 20)
    sma50 = sma(prices, 50)
    sma200 = sma(prices, 200)
    rsi_val = rsi(prices)
    macd_line, macd_signal = macd(prices)
    bb_upper, bb_mid, bb_lower = bollinger(prices)
    stoch_k, stoch_d = stochastic(highs, lows, prices)
    atr_val = atr(highs, lows, prices)
    atr_percent = (atr_val/current_price)*100 if current_price else 0
    
    return {
        'price': current_price,
        'sma20': sma20,
        'sma50': sma50,
        'sma200': sma200,
        'rsi': rsi_val,
        'macd_line': macd_line,
        'macd_signal': macd_signal,
        'bb_upper': bb_upper,
        'bb_mid': bb_mid,
        'bb_lower': bb_lower,
        'stoch_k': stoch_k,
        'stoch_d': stoch_d,
        'atr': atr_val,
        'atr_percent': atr_percent,
    }

# ============================================
# ANALIZA WIELOINTERWAŁOWA
# ============================================

def analyze_timeframes(market_name, symbol, timeframe_weights):
    """Analiza wielointerwałowa"""
    timeframe_results = {}
    
    for tf_name, tf_config in TIMEFRAMES.items():
        data = get_market_data(symbol, tf_config['interval'], tf_config['range'])
        if data:
            ind = calculate_indicators(data)
            if ind:
                ind['weight'] = timeframe_weights.get(tf_name, tf_config['default_weight'])
                ind['trend'] = determine_trend(ind)
                timeframe_results[tf_name] = ind
    
    return timeframe_results if timeframe_results else None

def determine_trend(ind):
    """Określ trend na podstawie wskaźników"""
    trend_count = 0
    if ind['price'] and ind['sma20'] and ind['price'] > ind['sma20']: trend_count += 1
    if ind['price'] and ind['sma50'] and ind['price'] > ind['sma50']: trend_count += 1
    if ind['sma200'] and ind['price'] and ind['price'] > ind['sma200']: trend_count += 1
    if ind['sma20'] and ind['sma50'] and ind['sma20'] > ind['sma50']: trend_count += 1
    
    if trend_count >= 3: return 'UP'
    if trend_count <= 1: return 'DOWN'
    return 'SIDEWAYS'

def combine_timeframe_analysis(timeframe_results):
    """Połącz analizy z różnych interwałów"""
    if not timeframe_results:
        return None
    
    combined = {
        'trend_score': 0,
        'momentum_score': 0,
        'total_weight': 0,
        'details': {}
    }
    
    for tf_name, ind in timeframe_results.items():
        weight = ind.get('weight', 0.2)
        combined['total_weight'] += weight
        
        # Trend score
        trend = 0
        if ind['price'] and ind['sma20'] and ind['price'] > ind['sma20']: trend += 1
        if ind['price'] and ind['sma50'] and ind['price'] > ind['sma50']: trend += 1
        if ind['sma200'] and ind['price'] and ind['price'] > ind['sma200']: trend += 1
        if ind['sma20'] and ind['sma50'] and ind['sma20'] > ind['sma50']: trend += 1
        trend_normalized = trend / 4
        
        # Momentum score
        momentum = 0
        if 30 < ind['rsi'] < 70: momentum += 0.5
        if ind['rsi'] > 50: momentum += 0.5
        if ind['macd_line'] > ind['macd_signal']: momentum += 0.5
        if ind['stoch_k'] > ind['stoch_d']: momentum += 0.5
        momentum_normalized = momentum / 2
        
        combined['trend_score'] += trend_normalized * weight
        combined['momentum_score'] += momentum_normalized * weight
        
        combined['details'][tf_name] = {
            'price': ind['price'],
            'trend': ind.get('trend', 'SIDEWAYS'),
            'rsi': ind['rsi'],
            'atr_percent': ind['atr_percent'],
        }
    
    if combined['total_weight'] > 0:
        combined['trend_score'] /= combined['total_weight']
        combined['momentum_score'] /= combined['total_weight']
    
    return combined

def create_heatmap(timeframe_results):
    """Stwórz tekstową heatmapę"""
    if not timeframe_results:
        return ""
    
    heatmap = "```\n"
    heatmap += "INTERWAŁ | TREND | RSI | ZGODNOŚĆ\n"
    heatmap += "---------|-------|-----|----------\n"
    
    for tf_name, ind in timeframe_results.items():
        trend = ind.get('trend', '?')
        rsi = ind.get('rsi', 50)
        alignment = '✅' if trend == 'UP' and rsi > 50 else '✅' if trend == 'DOWN' and rsi < 50 else '❌'
        heatmap += f"{tf_name:8} | {trend:5} | {rsi:3.0f} | {alignment}\n"
    
    heatmap += "```"
    return heatmap

def detect_divergences(timeframe_results):
    """Wykryj dywergencje między interwałami"""
    divergences = []
    
    if not timeframe_results or len(timeframe_results) < 2:
        return divergences
    
    short_tfs = ['5m', '15m']
    long_tfs = ['4h', '1d']
    
    for short_tf in short_tfs:
        if short_tf not in timeframe_results:
            continue
        for long_tf in long_tfs:
            if long_tf not in timeframe_results:
                continue
            short_trend = timeframe_results[short_tf].get('trend', '?')
            long_trend = timeframe_results[long_tf].get('trend', '?')
            if short_trend == 'UP' and long_trend == 'DOWN':
                divergences.append(f"⚠️ {short_tf} UP vs {long_tf} DOWN")
            elif short_trend == 'DOWN' and long_trend == 'UP':
                divergences.append(f"⚠️ {short_tf} DOWN vs {long_tf} UP")
    
    return divergences

def analyze_multi_timeframe_with_ai(market_name, combined, timeframe_results, divergences):
    """Analiza MTF przez AI"""
    if not combined:
        return None
    
    details_text = ""
    for tf, detail in combined['details'].items():
        details_text += f"{tf}: trend={detail['trend']}, RSI={detail['rsi']:.1f}\n"
    
    divergences_text = "\n".join(divergences) if divergences else "Brak"
    
    prompt = f"""Przeanalizuj wielointerwałową analizę dla {market_name}.

{details_text}
Trend score: {combined['trend_score']:.2f}
Momentum score: {combined['momentum_score']:.2f}
Dywergencje: {divergences_text}

Odpowiedz w JSON:
{{
    "overall_trend": <"strong_up"/"up"/"sideways"/"down"/"strong_down">,
    "timeframe_alignment": <0-1>,
    "best_timeframe": <interwał>,
    "divergence_impact": <"none"/"warning"/"critical">,
    "reasoning": <uzasadnienie po polsku>
}}
"""
    result = call_groq("Analityk techniczny. Odpowiadaj tylko JSON.", prompt, max_tokens=400)
    if result:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except:
                pass
    return None

# ============================================
# ANALIZA NEWSÓW
# ============================================

def fetch_news_by_query(query, lang='en', limit=10):
    """Pobierz newsy dla konkretnego zapytania"""
    try:
        query_encoded = query.replace(' ', '+').replace('&', '%26')
        url = f"https://news.google.com/rss/search?q={query_encoded}&hl={lang}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            articles = []
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ''
                if title:
                    articles.append({'title': title})
            return articles[:limit]
    except Exception as e:
        print(f"Błąd pobierania newsów ({query}): {e}")
    return []

def fetch_market_news(market_name):
    """Ulepszone pobieranie newsów dla rynku z różnych kategorii"""
    all_articles = []
    market_info = COUNTRY_NEWS.get(market_name, {})
    
    # 1. Newsy o instrumencie (po angielsku)
    articles_en = fetch_news_by_query(f"{market_name} market", 'en', 5)
    all_articles.extend(articles_en)
    
    # 2. Newsy o instrumencie (po polsku)
    query_pl = market_info.get('query_pl', market_name)
    articles_pl = fetch_news_by_query(query_pl, 'pl', 5)
    all_articles.extend(articles_pl)
    
    # 3. Newsy z kraju instrumentu (po angielsku)
    query_en = market_info.get('query_en', '')
    if query_en:
        country_articles = fetch_news_by_query(query_en, 'en', 5)
        all_articles.extend(country_articles)
    
    # 4. Newsy światowe (ekonomia, geopolityka, banki centralne)
    for category in ['ekonomia', 'geopolityka', 'banki_centralne']:
        if category in GLOBAL_NEWS_QUERIES:
            global_articles = fetch_news_by_query(GLOBAL_NEWS_QUERIES[category]['query_en'], 'en', 3)
            all_articles.extend(global_articles)
    
    # 5. Newsy z Polski (dla wszystkich rynków, jako kontekst lokalny)
    poland_articles = fetch_news_by_query('Polska gospodarka NBP', 'pl', 3)
    all_articles.extend(poland_articles)
    
    # Usuń duplikaty
    unique_articles = []
    seen_titles = set()
    for article in all_articles:
        title_lower = article['title'].lower()
        if title_lower not in seen_titles:
            seen_titles.add(title_lower)
            unique_articles.append(article)
    
    return unique_articles[:30]

def analyze_news_with_ai(articles, market_name, memory_context):
    """Analiza newsów przez AI"""
    if not articles:
        return {'sentiment': 0, 'impact': 'low', 'direction': 'neutral', 'reasoning': 'Brak newsów'}
    
    news_text = "\n".join([f"- {art['title']}" for art in articles[:15]])
    
    prompt = f"""Przeanalizuj wpływ newsów na {market_name}.

{memory_context}

Nagłówki (z Polski, USA, Europy i świata):
{news_text}

Odpowiedz w JSON:
{{
    "sentiment": <-1 do 1>,
    "impact": <"low"/"medium"/"high">,
    "direction": <"up"/"down"/"neutral">,
    "reasoning": <uzasadnienie po polsku>
}}
"""
    result = call_groq("Analityk rynkowy. Odpowiadaj tylko JSON.", prompt, max_tokens=400)
    if result:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except:
                pass
    return {'sentiment': 0, 'impact': 'low', 'direction': 'neutral', 'reasoning': ''}

# ============================================
# GROQ API
# ============================================

def call_groq(system_prompt, user_prompt, temperature=0.3, max_tokens=800):
    """Wywołaj Groq API"""
    if not GROQ_API_KEY:
        return None
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        response = requests.post(url, json=payload, headers=headers, timeout=25)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"Błąd Groq: {e}")
    return None

# ============================================
# ANALIZA RYNKU
# ============================================

def analyze_market(name, market_info, ai_memory, timeframe_weights):
    """Główna analiza rynku"""
    session = market_info.get('session', '24_7')
    now = datetime.now(TIMEZONE)
    hour = now.hour
    
    # Sprawdź sesję
    if session == 'US' and not (15 <= hour < 22):
        return None
    if session == 'EU' and not (9 <= hour < 17):
        return None
    
    # 1. Analiza wielointerwałowa
    timeframe_results = analyze_timeframes(name, market_info['symbol'], timeframe_weights)
    if not timeframe_results:
        return None
    
    # 2. Połącz analizy
    combined = combine_timeframe_analysis(timeframe_results)
    if not combined:
        return None
    
    # 3. Dywergencje
    divergences = detect_divergences(timeframe_results)
    
    # 4. Analiza MTF przez AI
    mtf_analysis = analyze_multi_timeframe_with_ai(name, combined, timeframe_results, divergences)
    
    # 5. Newsy
    articles = fetch_market_news(name)
    news_analysis = analyze_news_with_ai(articles, name, ai_memory.get_context())
    
    # 6. Główny interwał (15m)
    main_data = get_market_data(market_info['symbol'], '15m', '1d')
    if not main_data:
        return None
    ind = calculate_indicators(main_data)
    if not ind:
        return None
    
    # Filtr zmienności
    if ind['atr_percent'] < 0.3 or ind['atr_percent'] > 5:
        return None
    
    p = ind['price']
    
    # 7. Oblicz pewność
    long_score = 0
    short_score = 0
    
    # Techniczne (15m)
    if p > ind['sma20']: long_score += 1
    if p > ind['sma50']: long_score += 1
    if 30 < ind['rsi'] < 70: long_score += 1
    if ind['rsi'] > 50: long_score += 1
    if ind['macd_line'] > ind['macd_signal']: long_score += 1
    
    if p < ind['sma20']: short_score += 1
    if p < ind['sma50']: short_score += 1
    if 30 < ind['rsi'] < 70: short_score += 1
    if ind['rsi'] < 50: short_score += 1
    if ind['macd_line'] < ind['macd_signal']: short_score += 1
    
    # MTF
    if combined['trend_score'] > 0.6: long_score += 2
    elif combined['trend_score'] < 0.4: short_score += 2
    
    if combined['momentum_score'] > 0.6: long_score += 1
    elif combined['momentum_score'] < 0.4: short_score += 1
    
    # AI MTF
    if mtf_analysis:
        alignment = mtf_analysis.get('timeframe_alignment', 0)
        if mtf_analysis.get('overall_trend') in ['strong_up', 'up']:
            long_score += alignment * 2
        elif mtf_analysis.get('overall_trend') in ['strong_down', 'down']:
            short_score += alignment * 2
    
    # Dywergencje (zmniejszają pewność)
    if divergences:
        long_score -= 0.5
        short_score -= 0.5
    
    # Newsy
    if news_analysis:
        sentiment = news_analysis.get('sentiment', 0)
        if news_analysis.get('impact') == 'high':
            long_score += sentiment * 2
            short_score -= sentiment * 2
    
    total = 10
    long_conf = max(0, long_score / total)
    short_conf = max(0, short_score / total)
    
    threshold = 0.7
    
    if long_conf >= threshold or short_conf >= threshold:
        direction = 'LONG' if long_conf >= short_conf else 'SHORT'
        confidence = max(long_conf, short_conf)
        
        # Heatmapa
        heatmap = create_heatmap(timeframe_results)
        
        return {
            'name': name,
            'direction': direction,
            'entry': p,
            'stop_loss': p - 1.5 * ind['atr'] if direction == 'LONG' else p + 1.5 * ind['atr'],
            'take_profit': p + 2.5 * ind['atr'] if direction == 'LONG' else p - 2.5 * ind['atr'],
            'confidence': confidence,
            'rsi': ind['rsi'],
            'news_sentiment': news_analysis.get('sentiment', 0),
            'news_impact': news_analysis.get('impact', 'low'),
            'news_reasoning': news_analysis.get('reasoning', ''),
            'chart_pattern': mtf_analysis.get('overall_trend', '') if mtf_analysis else '',
            'ai_direction': mtf_analysis.get('best_timeframe', '') if mtf_analysis else '',
            'timeframe_alignment': mtf_analysis.get('timeframe_alignment', 0) if mtf_analysis else 0,
            'divergences': divergences,
            'heatmap': heatmap,
        }
    return None

# ============================================
# FUNKCJE RAPORTÓW
# ============================================

def analyze_market_sentiment():
    """Poranny raport sentymentu"""
    all_news = []
    for name in ['S&P500', 'NASDAQ', 'GOLD', 'OIL WTI', 'BITCOIN', 'EUR/USD', 'WIG20']:
        all_news.extend(fetch_market_news(name)[:5])
    
    if not all_news:
        return
    
    news_text = "\n".join([f"- {art['title']}" for art in all_news[:20]])
    
    prompt = f"""Przeanalizuj globalny sentyment rynkowy na podstawie tych nagłówków:

{news_text}

Odpowiedz w JSON:
{{
    "global_sentiment": <-1 do 1>,
    "risk_appetite": <"risk_on"/"risk_off"/"neutral">,
    "summary": <2-3 zdania po polsku>,
    "markets_to_watch": <lista instrumentów>
}}
"""
    result = call_groq("Analityk globalnych rynków. Odpowiadaj tylko JSON.", prompt, max_tokens=400)
    if result:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                message = f"🌅 *PORANNY RAPORT SENTYMENTU*\n\n"
                message += f"Sentyment globalny: {data.get('global_sentiment', 0):.2f}\n"
                message += f"Apetyt na ryzyko: {data.get('risk_appetite', 'neutral')}\n\n"
                message += f"{data.get('summary', '')}\n\n"
                if data.get('markets_to_watch'):
                    message += f"👀 Rynki do obserwacji: {data['markets_to_watch']}\n"
                send_telegram(message)
            except:
                pass

def compare_strategies():
    """Porównanie skuteczności strategii"""
    sm = SignalManager()
    signals = sm.signals
    if not signals:
        return
    
    by_market = {}
    by_direction = {'LONG': [], 'SHORT': []}
    
    for key, sig in signals.items():
        name = sig.get('name', '?')
        direction = sig.get('direction', '?')
        by_market.setdefault(name, []).append(sig)
        if direction in by_direction:
            by_direction[direction].append(sig)
    
    summary = f"📊 *PORÓWNANIE STRATEGII*\n\n"
    summary += f"Łącznie sygnałów: {len(signals)}\n\n"
    summary += "*Według instrumentu:*\n"
    for name, sigs in sorted(by_market.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
        avg_conf = sum(s.get('confidence', 0) for s in sigs) / len(sigs)
        summary += f"• {name}: {len(sigs)} (śr. pewność: {avg_conf:.0%})\n"
    summary += f"\n*Według kierunku:*\n"
    summary += f"• LONG: {len(by_direction['LONG'])}\n"
    summary += f"• SHORT: {len(by_direction['SHORT'])}\n"
    
    send_telegram(summary)

def check_upcoming_events():
    """Kalendarz wydarzeń"""
    now = datetime.now(TIMEZONE)
    upcoming = []
    if now.weekday() == 4 and now.day <= 7:
        upcoming.append("📌 NFP (Non-Farm Payrolls) - dziś!")
    if 10 <= now.day <= 15:
        upcoming.append("📌 Możliwa publikacja CPI w tym tygodniu")
    if now.weekday() == 2:
        upcoming.append("📌 FOMC Meeting - środa")
    if upcoming:
        message = "📅 *NADCHODZĄCE WYDARZENIA*\n\n" + "\n".join(upcoming)
        message += "\n\n⚠️ Zachowaj ostrożność"
        send_telegram(message)

def generate_weekly_report():
    """Raport tygodniowy"""
    ai_memory = AIMemory()
    sm = SignalManager()
    current_week = datetime.now(TIMEZONE).strftime('%Y-W%V')
    week_signals = []
    
    for key, sig in sm.signals.items():
        try:
            sig_time = datetime.fromisoformat(sig.get('timestamp', ''))
            if sig_time.strftime('%Y-W%V') == current_week:
                week_signals.append(sig)
        except:
            pass
    
    prompt = f"""Wygeneruj raport tygodniowy dla trading agenta.

Liczba sygnałów w tym tygodniu: {len(week_signals)}

Napisz raport po polsku:
1. Podsumowanie tygodnia
2. Jakie instrumenty były najczęściej sygnalizowane
3. Sugestie na przyszły tydzień
"""
    result = call_groq("Analityk rynkowy. Piszesz raporty tygodniowe.", prompt, temperature=0.5, max_tokens=500)
    if result:
        report = {
            'week': current_week,
            'timestamp': datetime.now(pytz.utc).isoformat(),
            'report': result
        }
        with open(WEEKLY_REPORT_FILE, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        send_telegram(f"📊 *RAPORT TYGODNIOWY*\n\n{result}")

def generate_monthly_report():
    """Raport miesięczny"""
    sm = SignalManager()
    current_month = datetime.now(TIMEZONE).strftime('%Y-%m')
    
    prompt = f"""Wygeneruj raport miesięczny dla trading agenta.

Miesiąc: {current_month}
Liczba sygnałów: {len(sm.signals)}

Napisz raport po polsku:
1. Podsumowanie miesiąca
2. Trendy i wzorce
3. Prognozy na przyszły miesiąc
"""
    result = call_groq("Analityk rynkowy. Piszesz raporty miesięczne.", prompt, temperature=0.5, max_tokens=600)
    if result:
        report = {
            'month': current_month,
            'timestamp': datetime.now(pytz.utc).isoformat(),
            'report': result
        }
        with open(MONTHLY_REPORT_FILE, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        send_telegram(f"📊 *RAPORT MIESIĘCZNY*\n\n{result}")

# ============================================
# GŁÓWNA PĘTLA
# ============================================

def main():
    if is_weekend():
        print("Weekend - agent nie pracuje.")
        return
    
    # Raporty
    if is_monthly_report_time():
        generate_monthly_report()
        return
    
    if is_friday_evening():
        generate_weekly_report()
        return
    
    if is_morning_sentiment_time():
        analyze_market_sentiment()
        check_upcoming_events()
        return
    
    # Porównanie strategii co 4 godziny
    if datetime.now(TIMEZONE).hour in [9, 13, 17] and datetime.now(TIMEZONE).minute < 10:
        compare_strategies()
        return
    
    # Główna analiza
    print(f"Analiza rynków: {datetime.now(TIMEZONE)}")
    sm = SignalManager()
    ai_memory = AIMemory()
    tf_weights = TimeframeWeights()
    potential = []
    
    for name, info in MARKETS.items():
        signal = analyze_market(name, info, ai_memory, tf_weights.get_weights())
        if signal:
            if sm.should_send_signal(signal):
                potential.append(signal)
    
    if potential:
        sorted_sigs = sorted(potential, key=lambda x: x['confidence'], reverse=True)[:10]
        msg = "🚨 *TOP SYGNAŁY*\n\n"
        for i, s in enumerate(sorted_sigs, 1):
            emoji = '🟢' if s['direction'] == 'LONG' else '🔴'
            msg += f"{i}. {emoji} {s['name']} ({s['direction']})\n"
            msg += f"   Pewność: {s['confidence']:.0%}\n"
            msg += f"   Trend MTF: {s['chart_pattern']}\n"
            msg += f"   Zgodność: {s['timeframe_alignment']:.0%}\n"
            msg += f"   Sentyment: {s['news_sentiment']:.2f}\n"
            if s['divergences']:
                msg += f"   ⚠️ Dywergencje: {len(s['divergences'])}\n"
            msg += f"\n{s['heatmap']}\n\n"
        
        send_telegram(msg)
        ai_memory.add_lesson(f"Wysłano {len(sorted_sigs)} sygnałów")
    else:
        print("Brak sygnałów.")

if __name__ == "__main__":
    main()