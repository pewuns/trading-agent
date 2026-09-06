import os
import json
import requests
import hashlib
import numpy as np
from datetime import datetime, timedelta
import pytz
import re

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
EVENTS_CALENDAR_FILE = 'events_calendar.json'

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

# ─────────────────────────────────────────
# PAMIĘĆ AI
# ─────────────────────────────────────────
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
        return {'lessons': [], 'patterns': {}, 'weekly_stats': {}, 'monthly_stats': {}}
    
    def save(self):
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(self.memory, f, indent=2, ensure_ascii=False)
    
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

# ─────────────────────────────────────────
# SYGNAŁY
# ─────────────────────────────────────────
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
        time_diff = datetime.now(pytz.utc) - prev_time
        minutes_diff = time_diff.total_seconds() / 60
        if price_diff >= 0.3 or minutes_diff >= 30:
            self.signals[key] = signal
            self.signals[key]['timestamp'] = datetime.now(pytz.utc).isoformat()
            self.save_signals()
            return True
        return False

# ─────────────────────────────────────────
# FUNKCJE POMOCNICZE
# ─────────────────────────────────────────
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

def get_market_data(symbol, period='1d', interval='15m'):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {'interval': interval, 'range': period}
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        data = response.json()
        result = data['chart']['result'][0]
        quotes = result['indicators']['quote'][0]
        prices = [p for p in quotes['close'] if p is not None]
        highs = [h for h in quotes['high'] if h is not None]
        lows = [l for l in quotes['low'] if l is not None]
        return {'prices': prices, 'highs': highs, 'lows': lows}
    except:
        return None

def calculate_indicators(data):
    prices = data['prices']; highs = data['highs']; lows = data['lows']
    current_price = prices[-1]
    
    def sma(arr, period):
        if len(arr) < period: return None
        return sum(arr[-period:]) / period
    
    def rsi(arr, period=14):
        if len(arr) < period+1: return 50
        gains, losses = [], []
        for i in range(1, len(arr)):
            change = arr[i]-arr[i-1]
            gains.append(max(0, change)); losses.append(max(0, -change))
        avg_gain = sum(gains[-period:])/period
        avg_loss = sum(losses[-period:])/period
        if avg_loss == 0: return 100
        rs = avg_gain/avg_loss
        return 100 - (100/(1+rs))
    
    def macd(arr):
        def ema(a, n):
            if len(a) < n: return None
            mult = 2/(n+1)
            e = a[0]
            for x in a[1:]: e = (x-e)*mult + e
            return e
        e12 = ema(arr,12); e26 = ema(arr,26)
        if e12 is None or e26 is None: return 0
        return e12 - e26
    
    def atr(highs, lows, closes, period=14):
        if len(closes) < period+1: return 0
        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            trs.append(tr)
        return sum(trs[-period:])/period
    
    sma20 = sma(prices,20); sma50 = sma(prices,50)
    rsi_val = rsi(prices); macd_val = macd(prices)
    atr_val = atr(highs, lows, prices)
    atr_percent = (atr_val/current_price)*100 if current_price else 0
    
    return {
        'price': current_price,
        'sma20': sma20, 'sma50': sma50,
        'rsi': rsi_val, 'macd': macd_val,
        'atr': atr_val, 'atr_percent': atr_percent
    }

def fetch_market_news(market_name):
    try:
        query = market_name.replace('&', '%26')
        url = f"https://news.google.com/rss/search?q={query}+market&hl=en"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)
            articles = []
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ''
                if title:
                    articles.append({'title': title})
            return articles[:15]
    except:
        pass
    return []

# ─────────────────────────────────────────
# GROQ API
# ─────────────────────────────────────────
def call_groq(system_prompt, user_prompt, temperature=0.3, max_tokens=800):
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
            data = response.json()
            return data['choices'][0]['message']['content']
    except Exception as e:
        print(f"Błąd Groq: {e}")
    return None

def analyze_news_with_ai(articles, market_name, memory_context):
    if not articles:
        return {'sentiment': 0, 'impact': 'low', 'direction': 'neutral', 'reasoning': ''}
    news_text = "\n".join([f"- {art['title']}" for art in articles[:10]])
    prompt = f"""Przeanalizuj wpływ newsów na {market_name}.

{memory_context}

Nagłówki:
{news_text}

Odpowiedz w JSON:
{{
    "sentiment": <-1 do 1>,
    "impact": <"low"/"medium"/"high">,
    "direction": <"up"/"down"/"neutral">,
    "reasoning": <uzasadnienie>
}}
"""
    result = call_groq("Analityk rynkowy. Odpowiadaj tylko JSON.", prompt, max_tokens=300)
    if result:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except:
                pass
    return {'sentiment': 0, 'impact': 'low', 'direction': 'neutral', 'reasoning': ''}

def analyze_chart_with_ai(prices, market_name):
    if len(prices) < 30:
        return None
    recent_prices = prices[-30:]
    prompt = f"""Przeanalizuj wzorzec cenowy dla {market_name}.

Ostatnie 30 cen: {recent_prices}

Odpowiedz w JSON:
{{
    "pattern": <nazwa wzorca>,
    "strength": <1-10>,
    "predicted_direction": <"up"/"down"/"sideways">,
    "confidence": <0-1>
}}
"""
    result = call_groq("Analityk techniczny. Odpowiadaj tylko JSON.", prompt, max_tokens=300)
    if result:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except:
                pass
    return None

def suggest_sl_tp_with_ai(entry, atr, market_name):
    prompt = f"""Zasugeruj poziomy Stop Loss i Take Profit dla {market_name}.

Cena wejścia: {entry}
ATR: {atr}

Odpowiedz w JSON:
{{
    "sl_multiplier": <mnożnik ATR dla SL>,
    "tp_multiplier": <mnożnik ATR dla TP>,
    "risk_reward_ratio": <stosunek>
}}
"""
    result = call_groq("Ekspert zarządzania ryzykiem. Odpowiadaj tylko JSON.", prompt, max_tokens=200)
    if result:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except:
                pass
    return {'sl_multiplier': 1.5, 'tp_multiplier': 2.5, 'risk_reward_ratio': 1.67}

# ─────────────────────────────────────────
# NOWE FUNKCJE
# ─────────────────────────────────────────

def analyze_market_sentiment():
    """Analiza sentymentu całego rynku (globalny obraz)"""
    print("📊 Analiza globalnego sentymentu...")
    
    # Pobierz newsy dla głównych rynków
    all_news = []
    for name in ['S&P500', 'NASDAQ', 'GOLD', 'OIL WTI', 'BITCOIN', 'EUR/USD']:
        articles = fetch_market_news(name)
        all_news.extend(articles[:5])
    
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
    "markets_to_watch": <lista instrumentów które mogą być najbardziej zmienne>
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
                    message += f"Rynki do obserwacji: {data['markets_to_watch']}\n"
                send_telegram(message)
            except:
                pass

def compare_strategies():
    """Porównanie skuteczności różnych strategii"""
    print("📊 Porównanie strategii...")
    
    signal_manager = SignalManager()
    signals = signal_manager.signals
    
    if not signals:
        return
    
    # Podziel sygnały na kategorie
    by_market = {}
    by_direction = {'LONG': [], 'SHORT': []}
    
    for key, sig in signals.items():
        name = sig.get('name', '?')
        direction = sig.get('direction', '?')
        confidence = sig.get('confidence', 0)
        
        if name not in by_market:
            by_market[name] = []
        by_market[name].append(sig)
        
        if direction in by_direction:
            by_direction[direction].append(sig)
    
    # Przygotuj podsumowanie
    summary = f"📊 *PORÓWNANIE STRATEGII*\n\n"
    summary += f"Łączna liczba sygnałów: {len(signals)}\n\n"
    
    summary += "*Według instrumentu:*\n"
    for name, sigs in sorted(by_market.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
        avg_conf = sum(s.get('confidence', 0) for s in sigs) / len(sigs)
        summary += f"• {name}: {len(sigs)} sygnałów (śr. pewność: {avg_conf:.0%})\n"
    
    summary += f"\n*Według kierunku:*\n"
    summary += f"• LONG: {len(by_direction['LONG'])} sygnałów\n"
    summary += f"• SHORT: {len(by_direction['SHORT'])} sygnałów\n"
    
    # Zapisz statystyki
    stats = {
        'total_signals': len(signals),
        'by_market': {name: len(sigs) for name, sigs in by_market.items()},
        'by_direction': {
            'LONG': len(by_direction['LONG']),
            'SHORT': len(by_direction['SHORT']),
        },
        'timestamp': datetime.now(pytz.utc).isoformat(),
    }
    with open(STRATEGY_STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=2)
    
    send_telegram(summary)

def check_upcoming_events():
    """Sprawdź nadchodzące wydarzenia ekonomiczne"""
    print("📅 Sprawdzanie kalendarza wydarzeń...")
    
    # Znane wydarzenia cykliczne (można rozszerzyć)
    now = datetime.now(TIMEZONE)
    upcoming = []
    
    # FOMC - zwykle 8 razy w roku (środa)
    # ECB - zwykle co 6 tygodni (czwartek)
    # NFP - pierwszy piątek miesiąca
    if now.weekday() == 4 and now.day <= 7:
        upcoming.append("📌 NFP (Non-Farm Payrolls) - dziś!")
    
    # CPI - zwykle 10-15 dnia miesiąca
    if 10 <= now.day <= 15:
        upcoming.append("📌 Możliwa publikacja CPI w tym tygodniu")
    
    if upcoming:
        message = "📅 *NADCHODZĄCE WYDARZENIA*\n\n"
        message += "\n".join(upcoming)
        message += "\n\n⚠️ Zachowaj ostrożność w tych dniach"
        send_telegram(message)

def generate_weekly_report():
    """Raport tygodniowy"""
    print("📊 Generowanie raportu tygodniowego...")
    
    ai_memory = AIMemory()
    signal_manager = SignalManager()
    
    lessons = ai_memory.memory.get('lessons', [])
    current_week = datetime.now(TIMEZONE).strftime('%Y-W%V')
    week_signals = []
    
    for key, sig in signal_manager.signals.items():
        try:
            sig_time = datetime.fromisoformat(sig.get('timestamp', ''))
            if sig_time.strftime('%Y-W%V') == current_week:
                week_signals.append(sig)
        except:
            pass
    
    lessons_text = "\n".join([f"- {l['lesson'][:150]}" for l in lessons[-20:]]) if lessons else "Brak"
    signals_text = ""
    for sig in week_signals[:20]:
        signals_text += f"- {sig.get('name', '?')} {sig.get('direction', '?')} (pewność: {sig.get('confidence', 0):.0%})\n"
    
    prompt = f"""Wygeneruj raport tygodniowy dla trading agenta.

Liczba sygnałów: {len(week_signals)}
Lekcje AI: {lessons_text}

Sygnały:
{signals_text if signals_text else "Brak"}

Napisz raport po polsku: podsumowanie, czego AI się nauczyło, sugestie na przyszły tydzień.
"""
    result = call_groq("Analityk rynkowy. Piszesz raporty tygodniowe.", prompt, temperature=0.5, max_tokens=500)
    
    if result:
        report = {'week': current_week, 'timestamp': datetime.now(pytz.utc).isoformat(), 'report': result}
        with open(WEEKLY_REPORT_FILE, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2)
        send_telegram(f"📊 *RAPORT TYGODNIOWY*\n\n{result}")
        ai_memory.add_lesson(f"Raport tygodniowy: {result[:200]}")

def generate_monthly_report():
    """Raport miesięczny"""
    print("📊 Generowanie raportu miesięcznego...")
    
    ai_memory = AIMemory()
    signal_manager = SignalManager()
    
    lessons = ai_memory.memory.get('lessons', [])
    current_month = datetime.now(TIMEZONE).strftime('%Y-%m')
    
    # Zbierz statystyki
    total_signals = len(signal_manager.signals)
    lessons_count = len(lessons)
    
    # Zapisz statystyki miesięczne
    monthly_stats = {
        'month': current_month,
        'total_signals': total_signals,
        'lessons_count': lessons_count,
        'timestamp': datetime.now(pytz.utc).isoformat(),
    }
    ai_memory.memory['monthly_stats'] = monthly_stats
    ai_memory.save()
    
    prompt = f"""Wygeneruj raport miesięczny dla trading agenta.

Miesiąc: {current_month}
Liczba sygnałów: {total_signals}
Liczba lekcji AI: {lessons_count}

Napisz raport po polsku: podsumowanie miesiąca, trendy, czego AI się nauczyło, prognozy na przyszły miesiąc.
"""
    result = call_groq("Analityk rynkowy. Piszesz raporty miesięczne.", prompt, temperature=0.5, max_tokens=600)
    
    if result:
        report = {'month': current_month, 'timestamp': datetime.now(pytz.utc).isoformat(), 'report': result}
        with open(MONTHLY_REPORT_FILE, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2)
        send_telegram(f"📊 *RAPORT MIESIĘCZNY*\n\n{result}")
        ai_memory.add_lesson(f"Raport miesięczny: {result[:200]}")

# ─────────────────────────────────────────
# ANALIZA RYNKU
# ─────────────────────────────────────────
def analyze_market(name, market_info, ai_memory):
    session = market_info.get('session', '24_7')
    now = datetime.now(TIMEZONE)
    hour = now.hour
    
    if session == 'US' and not (15 <= hour < 22):
        return None
    if session == 'EU' and not (9 <= hour < 17):
        return None
    
    data = get_market_data(market_info['symbol'])
    if not data:
        return None
    
    ind = calculate_indicators(data)
    if ind['sma20'] is None or ind['sma50'] is None:
        return None
    
    if ind['atr_percent'] < 0.3 or ind['atr_percent'] > 5:
        return None
    
    articles = fetch_market_news(name)
    memory_context = ai_memory.get_context()
    news_analysis = analyze_news_with_ai(articles, name, memory_context)
    chart_analysis = analyze_chart_with_ai(data['prices'], name)
    sl_tp = suggest_sl_tp_with_ai(ind['price'], ind['atr'], name)
    
    p = ind['price']
    
    long_score = 0
    if p > ind['sma20']: long_score += 1
    if p > ind['sma50']: long_score += 1
    if 30 < ind['rsi'] < 70: long_score += 1
    if ind['rsi'] > 50: long_score += 1
    if ind['macd'] > 0: long_score += 1
    
    short_score = 0
    if p < ind['sma20']: short_score += 1
    if p < ind['sma50']: short_score += 1
    if 30 < ind['rsi'] < 70: short_score += 1
    if ind['rsi'] < 50: short_score += 1
    if ind['macd'] < 0: short_score += 1
    
    total = 5
    long_conf = long_score / total
    short_conf = short_score / total
    
    if news_analysis:
        sentiment = news_analysis.get('sentiment', 0)
        impact = news_analysis.get('impact', 'low')
        if impact == 'high':
            long_conf += sentiment * 0.2
            short_conf -= sentiment * 0.2
    
    if chart_analysis:
        predicted_dir = chart_analysis.get('predicted_direction', 'sideways')
        if predicted_dir == 'up':
            long_conf += 0.1
        elif predicted_dir == 'down':
            short_conf += 0.1
    
    threshold = 0.7
    
    if long_conf >= threshold:
        return {
            'name': name, 'direction': 'LONG',
            'entry': p,
            'stop_loss': p - sl_tp['sl_multiplier'] * ind['atr'],
            'take_profit': p + sl_tp['tp_multiplier'] * ind['atr'],
            'confidence': long_conf,
            'rsi': ind['rsi'],
            'news_sentiment': news_analysis.get('sentiment', 0),
            'news_impact': news_analysis.get('impact', 'low'),
            'news_reasoning': news_analysis.get('reasoning', ''),
            'chart_pattern': chart_analysis.get('pattern', '') if chart_analysis else '',
            'ai_direction': chart_analysis.get('predicted_direction', '') if chart_analysis else '',
        }
    elif short_conf >= threshold:
        return {
            'name': name, 'direction': 'SHORT',
            'entry': p,
            'stop_loss': p + sl_tp['sl_multiplier'] * ind['atr'],
            'take_profit': p - sl_tp['tp_multiplier'] * ind['atr'],
            'confidence': short_conf,
            'rsi': ind['rsi'],
            'news_sentiment': news_analysis.get('sentiment', 0),
            'news_impact': news_analysis.get('impact', 'low'),
            'news_reasoning': news_analysis.get('reasoning', ''),
            'chart_pattern': chart_analysis.get('pattern', '') if chart_analysis else '',
            'ai_direction': chart_analysis.get('predicted_direction', '') if chart_analysis else '',
        }
    return None

# ─────────────────────────────────────────
# GŁÓWNA PĘTLA
# ─────────────────────────────────────────
def main():
    if is_weekend():
        print("Weekend - agent nie pracuje.")
        return
    
    # Raport miesięczny (1. dnia miesiąca o 8:00)
    if is_monthly_report_time():
        generate_monthly_report()
        return
    
    # Raport tygodniowy (piątek 19:00)
    if is_friday_evening():
        generate_weekly_report()
        return
    
    # Poranny raport sentymentu (8:10-8:20)
    if is_morning_sentiment_time():
        analyze_market_sentiment()
        check_upcoming_events()
        return
    
    # Porównanie strategii (co 4 godziny)
    if datetime.now(TIMEZONE).hour in [9, 13, 17] and datetime.now(TIMEZONE).minute < 10:
        compare_strategies()
        return
    
    print(f"Analiza rynków z AI: {datetime.now(TIMEZONE)}")
    sm = SignalManager()
    ai_memory = AIMemory()
    potential = []
    
    for name, info in MARKETS.items():
        signal = analyze_market(name, info, ai_memory)
        if signal:
            if sm.should_send_signal(signal):
                potential.append(signal)
    
    if potential:
        sorted_sigs = sorted(potential, key=lambda x: x['confidence'], reverse=True)[:10]
        msg = "🚨 *TOP SYGNAŁY (z pełną analizą AI)*\n\n"
        for i, s in enumerate(sorted_sigs, 1):
            emoji = '🟢' if s['direction'] == 'LONG' else '🔴'
            msg += f"{i}. {emoji} {s['name']} ({s['direction']})\n"
            msg += f"   Pewność: {s['confidence']:.0%}\n"
            msg += f"   Sentyment AI: {s['news_sentiment']:.2f}\n"
            msg += f"   Wzorzec AI: {s['chart_pattern']}\n"
            msg += f"   Kierunek AI: {s['ai_direction']}\n"
            if s['news_reasoning']:
                msg += f"   AI: {s['news_reasoning'][:100]}...\n"
            msg += "\n"
        send_telegram(msg)
        ai_memory.add_lesson(f"Wysłano {len(sorted_sigs)} sygnałów o {datetime.now(TIMEZONE)}")
    else:
        print("Brak sygnałów.")

if __name__ == "__main__":
    main()