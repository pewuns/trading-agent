import os
import json
import requests
import hashlib
import numpy as np
from datetime import datetime, timedelta
import pytz
import re
import xml.etree.ElementTree as ET

# Tokeny
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# Pliki
SIGNALS_FILE = 'signals_history.json'
AI_MEMORY_FILE = 'ai_memory.json'
WEEKLY_REPORT_FILE = 'weekly_report.json'
MONTHLY_REPORT_FILE = 'monthly_report.json'
TIMEFRAME_WEIGHTS_FILE = 'timeframe_weights.json'

TIMEZONE = pytz.timezone('Europe/Warsaw')

# Rynki
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

# Interwały
TIMEFRAMES = {
    '5m': {'interval': '5m', 'range': '1d', 'default_weight': 0.15},
    '15m': {'interval': '15m', 'range': '1d', 'default_weight': 0.20},
    '1h': {'interval': '60m', 'range': '5d', 'default_weight': 0.25},
    '4h': {'interval': '60m', 'range': '1mo', 'default_weight': 0.15},
    '1d': {'interval': '1d', 'range': '3mo', 'default_weight': 0.25},
}

# Kraje dla newsów
COUNTRY_NEWS = {
    'DAX': {'query_pl': 'Niemcy gospodarka DAX', 'query_en': 'Germany economy DAX'},
    'S&P500': {'query_pl': 'USA gospodarka Wall Street', 'query_en': 'US economy Wall Street'},
    'NASDAQ': {'query_pl': 'USA technologia giełda', 'query_en': 'US tech stocks'},
    'EUR/USD': {'query_pl': 'strefa euro ECB', 'query_en': 'Eurozone ECB'},
    'GOLD': {'query_pl': 'złoto cena', 'query_en': 'gold price'},
    'OIL WTI': {'query_pl': 'ropa naftowa cena', 'query_en': 'oil price OPEC'},
    'BITCOIN': {'query_pl': 'bitcoin kryptowaluty', 'query_en': 'bitcoin crypto'},
    'ETHEREUM': {'query_pl': 'ethereum kryptowaluty', 'query_en': 'ethereum crypto'},
    'SOLANA': {'query_pl': 'solana kryptowaluty', 'query_en': 'solana crypto'},
    'APPLE': {'query_pl': 'Apple akcje', 'query_en': 'Apple stock'},
    'MICROSOFT': {'query_pl': 'Microsoft akcje', 'query_en': 'Microsoft stock'},
    'NVIDIA': {'query_pl': 'Nvidia akcje', 'query_en': 'Nvidia stock'},
    'TESLA': {'query_pl': 'Tesla akcje', 'query_en': 'Tesla stock'},
    'AMAZON': {'query_pl': 'Amazon akcje', 'query_en': 'Amazon stock'},
    'META': {'query_pl': 'Meta akcje', 'query_en': 'Meta stock'},
    'GOOGLE': {'query_pl': 'Google akcje', 'query_en': 'Google stock'},
}

GLOBAL_NEWS_QUERIES = {
    'geopolityka': {'query_en': 'war conflict geopolitics'},
    'ekonomia': {'query_en': 'economic crisis inflation'},
    'banki_centralne': {'query_en': 'central bank interest rates'},
    'dywidendy': {'query_en': 'dividend stocks ex-dividend date'},
}

# ============================================
# FILTRY BEZPIECZEŃSTWA
# ============================================

def check_spread(market_type):
    """Sprawdź czy spread jest akceptowalny"""
    spread_thresholds = {
        'forex': 0.0002,
        'index': 0.001,
        'commodity': 0.0005,
        'crypto': 0.002,
        'stock': 0.001,
    }
    actual_spreads = {
        'forex': 0.0001,
        'index': 0.0005,
        'commodity': 0.0003,
        'crypto': 0.001,
        'stock': 0.0005,
    }
    return actual_spreads.get(market_type, 0.001) <= spread_thresholds.get(market_type, 0.001)

def check_extreme_volatility(atr_percent, market_type):
    """Sprawdź czy zmienność nie jest ekstremalna"""
    volatility_limits = {
        'forex': 1.5,
        'index': 3.0,
        'commodity': 5.0,
        'crypto': 8.0,
        'stock': 5.0,
    }
    return atr_percent <= volatility_limits.get(market_type, 3.0)

def check_liquidity(volume, market_type):
    """Sprawdź czy jest wystarczająca płynność"""
    min_volume = {
        'forex': 1000000,
        'index': 100000,
        'commodity': 50000,
        'crypto': 100,
        'stock': 1000000,
    }
    return volume >= min_volume.get(market_type, 100000)

# ============================================
# KLASY
# ============================================

class AIMemory:
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
        return {'lessons': []}
    
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
        lessons_text = "\n".join([f"- {l['lesson'][:200]}" for l in self.memory['lessons'][-15:]])
        return f"Dotychczasowe lekcje:\n{lessons_text}"

class TimeframeWeights:
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
    
    def get_weights(self):
        return self.weights

class SignalManager:
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
        minutes_diff = (datetime.now(pytz.utc) - prev_time).total_seconds() / 60
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
        return {'prices': prices, 'highs': highs, 'lows': lows, 'volumes': volumes, 'opens': opens}
    except Exception as e:
        print(f"Błąd pobierania {symbol}: {e}")
        return None

def calculate_indicators(data):
    if not data or len(data['prices']) < 50:
        return None
    prices = data['prices']
    highs = data['highs']
    lows = data['lows']
    volumes = data.get('volumes', [])
    current_price = prices[-1]
    
    def sma(arr, period):
        if len(arr) < period: return None
        return sum(arr[-period:]) / period
    
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
        return 100 - (100/(1+avg_gain/avg_loss))
    
    def atr(highs, lows, closes, period=14):
        if len(closes) < period+1: return 0
        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            trs.append(tr)
        return sum(trs[-period:])/period
    
    sma20 = sma(prices, 20)
    sma50 = sma(prices, 50)
    rsi_val = rsi(prices)
    atr_val = atr(highs, lows, prices)
    atr_percent = (atr_val/current_price)*100 if current_price else 0
    
    avg_volume = sum(volumes[-20:]) / min(len(volumes), 20) if volumes else 0
    
    return {
        'price': current_price,
        'sma20': sma20,
        'sma50': sma50,
        'rsi': rsi_val,
        'atr': atr_val,
        'atr_percent': atr_percent,
        'avg_volume': avg_volume,
    }

# ============================================
# ANALIZA WIELOINTERWAŁOWA
# ============================================

def analyze_timeframes(symbol, timeframe_weights):
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
    trend_count = 0
    if ind['price'] and ind['sma20'] and ind['price'] > ind['sma20']: trend_count += 1
    if ind['price'] and ind['sma50'] and ind['price'] > ind['sma50']: trend_count += 1
    if ind['sma20'] and ind['sma50'] and ind['sma20'] > ind['sma50']: trend_count += 1
    if trend_count >= 2: return 'UP'
    if trend_count == 0: return 'DOWN'
    return 'SIDEWAYS'

def combine_timeframe_analysis(timeframe_results):
    if not timeframe_results:
        return None
    combined = {'trend_score': 0, 'momentum_score': 0, 'total_weight': 0, 'details': {}}
    for tf_name, ind in timeframe_results.items():
        weight = ind.get('weight', 0.2)
        combined['total_weight'] += weight
        trend = 0
        if ind['price'] and ind['sma20'] and ind['price'] > ind['sma20']: trend += 1
        if ind['price'] and ind['sma50'] and ind['price'] > ind['sma50']: trend += 1
        if ind['sma20'] and ind['sma50'] and ind['sma20'] > ind['sma50']: trend += 1
        combined['trend_score'] += (trend / 3) * weight
        momentum = 0
        if 30 < ind['rsi'] < 70: momentum += 0.5
        if ind['rsi'] > 50: momentum += 0.5
        combined['momentum_score'] += momentum * weight
        combined['details'][tf_name] = {
            'price': ind['price'],
            'trend': ind.get('trend', 'SIDEWAYS'),
            'rsi': ind['rsi'],
        }
    if combined['total_weight'] > 0:
        combined['trend_score'] /= combined['total_weight']
        combined['momentum_score'] /= combined['total_weight']
    return combined

def create_heatmap(timeframe_results):
    if not timeframe_results:
        return ""
    heatmap = "```\nINTERWAŁ | TREND | RSI | ZGODNOŚĆ\n---------|-------|-----|----------\n"
    for tf_name, ind in timeframe_results.items():
        trend = ind.get('trend', '?')
        rsi = ind.get('rsi', 50)
        alignment = '✅' if trend == 'UP' and rsi > 50 else '✅' if trend == 'DOWN' and rsi < 50 else '❌'
        heatmap += f"{tf_name:8} | {trend:5} | {rsi:3.0f} | {alignment}\n"
    heatmap += "```"
    return heatmap

def detect_divergences(timeframe_results):
    divergences = []
    if not timeframe_results or len(timeframe_results) < 2:
        return divergences
    for short_tf in ['5m', '15m']:
        for long_tf in ['4h', '1d']:
            if short_tf in timeframe_results and long_tf in timeframe_results:
                short_trend = timeframe_results[short_tf].get('trend', '?')
                long_trend = timeframe_results[long_tf].get('trend', '?')
                if short_trend == 'UP' and long_trend == 'DOWN':
                    divergences.append(f"⚠️ {short_tf} UP vs {long_tf} DOWN")
                elif short_trend == 'DOWN' and long_trend == 'UP':
                    divergences.append(f"⚠️ {short_tf} DOWN vs {long_tf} UP")
    return divergences

# ============================================
# ANALIZA NEWSÓW
# ============================================

def fetch_news_by_query(query, lang='en', limit=10):
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
    except:
        pass
    return []

def fetch_market_news(market_name):
    all_articles = []
    market_info = COUNTRY_NEWS.get(market_name, {})
    all_articles.extend(fetch_news_by_query(f"{market_name} market", 'en', 5))
    all_articles.extend(fetch_news_by_query(market_info.get('query_pl', market_name), 'pl', 5))
    if market_info.get('query_en'):
        all_articles.extend(fetch_news_by_query(market_info['query_en'], 'en', 5))
    for category in ['ekonomia', 'geopolityka', 'banki_centralne', 'dywidendy']:
        if category in GLOBAL_NEWS_QUERIES:
            all_articles.extend(fetch_news_by_query(GLOBAL_NEWS_QUERIES[category]['query_en'], 'en', 3))
    unique_articles = []
    seen = set()
    for a in all_articles:
        if a['title'].lower() not in seen:
            seen.add(a['title'].lower())
            unique_articles.append(a)
    return unique_articles[:30]

def call_groq(system_prompt, user_prompt, temperature=0.3, max_tokens=800):
    if not GROQ_API_KEY:
        return None
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
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

def analyze_news_with_ai(articles, market_name, memory_context):
    if not articles:
        return {'sentiment': 0, 'impact': 'low', 'direction': 'neutral', 'reasoning': 'Brak newsów'}
    news_text = "\n".join([f"- {a['title']}" for a in articles[:15]])
    prompt = f"""Przeanalizuj wpływ newsów na {market_name}.

{memory_context}

Nagłówki:
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
# GŁÓWNA ANALIZA RYNKU
# ============================================

def analyze_market(name, market_info, ai_memory, timeframe_weights):
    session = market_info.get('session', '24_7')
    now = datetime.now(TIMEZONE)
    hour = now.hour
    
    if session == 'US' and not (15 <= hour < 22):
        return None
    if session == 'EU' and not (9 <= hour < 17):
        return None
    
    # Pobierz dane główne (15m)
    main_data = get_market_data(market_info['symbol'], '15m', '1d')
    if not main_data:
        return None
    
    ind = calculate_indicators(main_data)
    if not ind:
        return None
    
    market_type = market_info['type']
    current_price = ind['price']
    atr_percent = ind['atr_percent']
    avg_volume = ind['avg_volume']
    
    # ============================================
    # FILTRY BEZPIECZEŃSTWA
    # ============================================
    if not check_spread(market_type):
        print(f"❌ {name}: Spread za duży")
        return None
    
    if not check_extreme_volatility(atr_percent, market_type):
        print(f"❌ {name}: Ekstremalna zmienność (ATR: {atr_percent:.2f}%)")
        return None
    
    if not check_liquidity(avg_volume, market_type):
        print(f"❌ {name}: Za mała płynność (Wolumen: {avg_volume:.0f})")
        return None
    
    # Analiza wielointerwałowa
    timeframe_results = analyze_timeframes(market_info['symbol'], timeframe_weights)
    if not timeframe_results:
        return None
    
    combined = combine_timeframe_analysis(timeframe_results)
    if not combined:
        return None
    
    divergences = detect_divergences(timeframe_results)
    
    # Newsy
    articles = fetch_market_news(name)
    news_analysis = analyze_news_with_ai(articles, name, ai_memory.get_context())
    
    p = ind['price']
    
    # Oblicz pewność
    long_score = 0
    short_score = 0
    
    # Techniczne (15m)
    if p > ind['sma20']: long_score += 1
    if p > ind['sma50']: long_score += 1
    if 30 < ind['rsi'] < 70: long_score += 1
    if ind['rsi'] > 50: long_score += 1
    
    if p < ind['sma20']: short_score += 1
    if p < ind['sma50']: short_score += 1
    if 30 < ind['rsi'] < 70: short_score += 1
    if ind['rsi'] < 50: short_score += 1
    
    # MTF
    if combined['trend_score'] > 0.6: long_score += 2
    elif combined['trend_score'] < 0.4: short_score += 2
    
    # Dywergencje
    if divergences:
        long_score -= 0.5
        short_score -= 0.5
    
    # Newsy
    if news_analysis:
        sentiment = news_analysis.get('sentiment', 0)
        if news_analysis.get('impact') == 'high':
            long_score += sentiment * 2
            short_score -= sentiment * 2
    
    total = 8
    long_conf = max(0, long_score / total)
    short_conf = max(0, short_score / total)
    
    threshold = 0.7
    
    if long_conf >= threshold or short_conf >= threshold:
        direction = 'LONG' if long_conf >= short_conf else 'SHORT'
        confidence = max(long_conf, short_conf)
        
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
            'chart_pattern': combined['trend_score'],
            'timeframe_alignment': combined['trend_score'],
            'divergences': divergences,
            'heatmap': create_heatmap(timeframe_results),
        }
    return None

# ============================================
# RAPORTY
# ============================================

def analyze_market_sentiment():
    all_news = []
    for name in ['S&P500', 'NASDAQ', 'GOLD', 'OIL WTI', 'BITCOIN', 'EUR/USD']:
        all_news.extend(fetch_market_news(name)[:5])
    if not all_news:
        return
    news_text = "\n".join([f"- {a['title']}" for a in all_news[:20]])
    prompt = f"Przeanalizuj globalny sentyment:\n{news_text}\nOdpowiedz w JSON: {{\"global_sentiment\": <-1 do 1>, \"risk_appetite\": <string>, \"summary\": <string>}}"
    result = call_groq("Analityk globalnych rynków.", prompt, max_tokens=300)
    if result:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                message = f"🌅 *PORANNY RAPORT*\n\n"
                message += f"Sentyment: {data.get('global_sentiment', 0):.2f}\n"
                message += f"Apetyt: {data.get('risk_appetite', 'neutral')}\n\n"
                message += f"{data.get('summary', '')}"
                send_telegram(message)
            except:
                pass

def generate_weekly_report():
    sm = SignalManager()
    current_week = datetime.now(TIMEZONE).strftime('%Y-W%V')
    week_signals = []
    for key, sig in sm.signals.items():
        try:
            sig_time = datetime.fromisoformat(sig.get('timestamp', '2000-01-01T00:00:00+00:00'))
            if sig_time.strftime('%Y-W%V') == current_week:
                week_signals.append(sig)
        except:
            pass
    prompt = f"Raport tygodniowy. Sygnały: {len(week_signals)}. Napisz po polsku."
    result = call_groq("Analityk rynkowy.", prompt, max_tokens=500)
    if result:
        send_telegram(f"📊 *RAPORT TYGODNIOWY*\n\n{result}")

def generate_monthly_report():
    sm = SignalManager()
    prompt = f"Raport miesięczny. Sygnały: {len(sm.signals)}. Napisz po polsku."
    result = call_groq("Analityk rynkowy.", prompt, max_tokens=600)
    if result:
        send_telegram(f"📊 *RAPORT MIESIĘCZNY*\n\n{result}")

# ============================================
# GŁÓWNA PĘTLA
# ============================================

def main():
    if is_weekend():
        print("Weekend - agent nie pracuje.")
        return
    
    if is_monthly_report_time():
        generate_monthly_report()
        return
    
    if is_friday_evening():
        generate_weekly_report()
        return
    
    if is_morning_sentiment_time():
        analyze_market_sentiment()
        return
    
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