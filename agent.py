import os
import json
import requests
import hashlib
import numpy as np
import re
from datetime import datetime, timedelta
import pytz
from pathlib import Path

# Tokeny
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
NEWS_API_KEY = os.environ.get('NEWS_API_KEY', '')

# Pliki
SIGNALS_FILE = 'signals_history.json'
REPORT_FILE = 'daily_report.json'
MODEL_FILE = 'model_weights.json'
EVENTS_FILE = 'events_impact.json'

TIMEZONE = pytz.timezone('Europe/Warsaw')
WORK_DAYS = [0, 1, 2, 3, 4]  # pon-pt

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

# Typowe spready (przybliżone wartości z XTB)
SPREADS = {
    'forex': 0.0001,   # 1 pip
    'index': 1.0,      # 1 punkt indeksowy
    'commodity': 0.2,  # 0.2 USD
    'crypto': 0.001,   # 0.1% ceny
    'stock': 0.02,     # 2 centy
}

# Słowa kluczowe dla sentymentu
POSITIVE_WORDS = ['growth', 'profit', 'increase', 'positive', 'bullish', 'rally', 'gain', 'recovery', 'expansion', 'success', 'improvement', 'record', 'surge', 'boom', 'upgrade', 'outperform', 'strong', 'beat', 'exceed', 'adoption', 'etf', 'breakout', 'support']
NEGATIVE_WORDS = ['recession', 'decline', 'loss', 'negative', 'bearish', 'crash', 'crisis', 'downturn', 'collapse', 'risk', 'warning', 'debt', 'inflation', 'default', 'bankrupt', 'layoff', 'downgrade', 'underperform', 'weak', 'miss', 'below', 'ban', 'hack', 'fraud', 'resistance', 'lawsuit']

# Wydarzenia ważne dla rynków (słowa kluczowe do wykrywania w nagłówkach)
IMPORTANT_EVENTS_KEYWORDS = [
    'FOMC', 'Fed decision', 'ECB', 'BoJ', 'BoE', 'interest rate', 'CPI', 'inflation',
    'GDP', 'unemployment', 'NFP', 'nonfarm payrolls', 'PMI', 'trade balance',
    'OPEC', 'oil supply', 'geopolitical', 'war', 'sanction', 'blockade', 'Strait of Hormuz',
    'election', 'summit', 'pandemic', 'virus', 'tariff', 'Brexit', 'stimulus'
]

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
        except Exception as e:
            print(f"Błąd wczytywania: {e}")
        return {}

    def save_signals(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.signals, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"Błąd zapisu: {e}")
            return False

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

def is_weekend():
    return datetime.now(TIMEZONE).weekday() >= 5

def current_hour():
    return datetime.now(TIMEZONE).hour

def is_training_time():
    now = datetime.now(TIMEZONE)
    return now.hour == 8 and now.minute < 10

def is_summary_time():
    now = datetime.now(TIMEZONE)
    return now.hour == 19 and now.minute < 10

def is_morning_report_time():
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
        volumes = [v for v in quotes['volume'] if v is not None]
        return {'prices': prices, 'highs': highs, 'lows': lows, 'volumes': volumes}
    except:
        return None

def calculate_indicators(data):
    prices = data['prices']; highs = data['highs']; lows = data['lows']; volumes = data['volumes']
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
    def bollinger(arr, period=20):
        if len(arr) < period: return None, None, None
        sma_val = sum(arr[-period:])/period
        std = (sum([(x-sma_val)**2 for x in arr[-period:]])/period)**0.5
        return sma_val+2*std, sma_val, sma_val-2*std
    def stochastic(highs, lows, closes, period=14):
        if len(closes) < period: return 50, 50
        hh = max(highs[-period:]); ll = min(lows[-period:])
        k = 100*(closes[-1]-ll)/(hh-ll) if hh!=ll else 50
        d = 50
        return k, d
    def atr(highs, lows, closes, period=14):
        if len(closes) < period+1: return 0
        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            trs.append(tr)
        return sum(trs[-period:])/period
    sma20 = sma(prices,20); sma50 = sma(prices,50)
    rsi_val = rsi(prices); macd_val = macd(prices)
    bb_upper, bb_mid, bb_lower = bollinger(prices)
    stoch_k, stoch_d = stochastic(highs, lows, prices)
    atr_val = atr(highs, lows, prices)
    atr_percent = (atr_val/current_price)*100 if current_price else 0
    return {
        'price': current_price,
        'sma20': sma20, 'sma50': sma50,
        'rsi': rsi_val, 'macd': macd_val,
        'bb_upper': bb_upper, 'bb_mid': bb_mid, 'bb_lower': bb_lower,
        'stoch_k': stoch_k, 'stoch_d': stoch_d,
        'atr': atr_val, 'atr_percent': atr_percent
    }

def get_seasonality_factor(now=None):
    if now is None:
        now = datetime.now(TIMEZONE)
    month = now.month
    if month in [12, 1]:
        return 0.8   # okres świąteczny, mniejsza płynność
    if month in [7, 8]:
        return 0.85  # wakacje
    if month in [9, 10]:
        return 1.2   # zwiększona zmienność jesienią
    return 1.0

def get_spread_for_type(market_type):
    return SPREADS.get(market_type, 0.001)

def load_events_impact():
    try:
        if os.path.exists(EVENTS_FILE):
            with open(EVENTS_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {
        "war": {"impact": -0.05, "instruments": ["OIL WTI", "GOLD"]},
        "blockade": {"impact": 0.1, "instruments": ["OIL WTI"]},
        "rate_hike": {"impact": -0.02, "instruments": ["S&P500", "NASDAQ"]},
        "pandemic": {"impact": -0.03, "instruments": ["BITCOIN", "APPLE"]}
    }

def save_events_impact(events):
    with open(EVENTS_FILE, 'w') as f:
        json.dump(events, f, indent=2)

def detect_important_events_from_news(articles):
    events_found = []
    for article in articles:
        title = article.get('title', '').lower()
        for keyword in IMPORTANT_EVENTS_KEYWORDS:
            if keyword.lower() in title:
                events_found.append(keyword)
                break
    return events_found

def fetch_morning_news():
    try:
        url = "https://news.google.com/rss?hl=en&gl=US&ceid=US:en"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)
            articles = []
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ''
                pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ''
                articles.append({'title': title, 'publishedAt': pub_date})
            return articles
    except:
        pass
    return []

def morning_report():
    articles = fetch_morning_news()
    if not articles:
        return
    important = []
    for art in articles:
        title = art['title']
        for kw in IMPORTANT_EVENTS_KEYWORDS:
            if kw.lower() in title.lower():
                important.append(title)
                break
    if important:
        msg = "🌅 *Poranny raport wydarzeń*\n\n"
        for t in important[:5]:
            msg += f"• {t}\n"
        send_telegram(msg)

def analyze_market(name, market_info, model_weights=None):
    session = market_info.get('session', '24_7')
    now = datetime.now(TIMEZONE)
    hour = now.hour
    minute = now.minute
    if session == 'US':
        if not (15 <= hour < 22):
            return None
        # Unikaj pierwszych 5 minut po otwarciu (duża zmienność)
        if hour == 15 and minute < 35:
            return None
    if session == 'EU':
        if not (9 <= hour < 17):
            return None
        if hour == 9 and minute < 15:
            return None

    data = get_market_data(market_info['symbol'])
    if not data:
        return None
    ind = calculate_indicators(data)
    if ind['sma20'] is None or ind['sma50'] is None:
        return None

    if ind['atr_percent'] < 0.3 or ind['atr_percent'] > 5:
        return None

    p = ind['price']
    season_factor = get_seasonality_factor()
    features = [
        p/ind['sma20'] if ind['sma20'] else 1,
        p/ind['sma50'] if ind['sma50'] else 1,
        ind['rsi'],
        ind['macd'],
        ind['atr_percent'],
        ind['stoch_k'] - ind['stoch_d'],
        1 if p > ind['bb_mid'] else -1,
    ]

    long_score = 0
    if p > ind['sma20']: long_score += 1
    if p > ind['sma50']: long_score += 1
    if 30 < ind['rsi'] < 70: long_score += 1
    if ind['rsi'] > 50: long_score += 1
    if ind['macd'] > 0: long_score += 1
    if ind['bb_lower'] and p > ind['bb_lower']: long_score += 1
    if ind['stoch_k'] > ind['stoch_d']: long_score += 1

    short_score = 0
    if p < ind['sma20']: short_score += 1
    if p < ind['sma50']: short_score += 1
    if 30 < ind['rsi'] < 70: short_score += 1
    if ind['rsi'] < 50: short_score += 1
    if ind['macd'] < 0: short_score += 1
    if ind['bb_upper'] and p < ind['bb_upper']: short_score += 1
    if ind['stoch_k'] < ind['stoch_d']: short_score += 1

    total = 7
    long_conf = long_score / total * season_factor
    short_conf = short_score / total * season_factor
    threshold = 0.7

    spread = get_spread_for_type(market_info['type'])
    if long_conf >= threshold:
        return {
            'name': name, 'direction': 'LONG',
            'entry': p,
            'stop_loss': p - 1.5 * ind['atr'] - spread,
            'take_profit': p + 2.5 * ind['atr'] - spread,
            'confidence': long_conf,
            'rsi': ind['rsi'],
            'atr_percent': ind['atr_percent'],
            'potential': ind['atr_percent'] * 2.5,
            'features': features,
        }
    elif short_conf >= threshold:
        return {
            'name': name, 'direction': 'SHORT',
            'entry': p,
            'stop_loss': p + 1.5 * ind['atr'] + spread,
            'take_profit': p - 2.5 * ind['atr'] + spread,
            'confidence': short_conf,
            'rsi': ind['rsi'],
            'atr_percent': ind['atr_percent'],
            'potential': ind['atr_percent'] * 2.5,
            'features': features,
        }
    return None

def train_model_from_report():
    print("Trenowanie modelu...")
    if not os.path.exists(REPORT_FILE):
        return None
    with open(REPORT_FILE, 'r') as f:
        report = json.load(f)
    X = []; y = []
    for item in report.get('signals', []):
        if 'features' in item and 'win' in item:
            X.append(item['features'])
            y.append(1 if item['win'] else 0)
    if len(X) < 5:
        return None
    X = np.array(X); y = np.array(y)
    X_b = np.c_[np.ones((X.shape[0],1)), X]
    lr = 0.1
    weights = np.zeros(X_b.shape[1])
    for _ in range(1000):
        z = X_b.dot(weights)
        pred = 1/(1+np.exp(-z))
        grad = X_b.T.dot(pred-y)/len(y)
        weights -= lr*grad
    model = {'weights': weights.tolist()}
    with open(MODEL_FILE, 'w') as f:
        json.dump(model, f)
    print("Model zapisany.")
    return model

def load_model():
    try:
        if os.path.exists(MODEL_FILE):
            with open(MODEL_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return None

def predict_probability(features, model):
    if not model or 'weights' not in model:
        return 0.5
    w = np.array(model['weights'])
    x = np.array([1] + features)
    z = x.dot(w)
    return 1/(1+np.exp(-z))

def end_of_day_summary():
    print("Generowanie podsumowania dnia...")
    sm = SignalManager()
    signals = sm.signals
    if not signals:
        send_telegram("ℹ️ Dzisiaj nie było sygnałów.")
        return
    results = []
    for key, sig in signals.items():
        symbol = MARKETS.get(sig['name'], {}).get('symbol')
        if not symbol:
            continue
        data = get_market_data(symbol, period='1d', interval='5m')
        if not data:
            continue
        prices = data['prices']
        entry = sig['entry']; sl = sig['stop_loss']; tp = sig['take_profit']
        spread = get_spread_for_type(MARKETS.get(sig['name'], {}).get('type', 'stock'))
        win = False
        if sig['direction'] == 'LONG':
            for p in prices:
                if p <= sl - spread:
                    win = False
                    break
                if p >= tp + spread:
                    win = True
                    break
        else:
            for p in prices:
                if p >= sl + spread:
                    win = False
                    break
                if p <= tp - spread:
                    win = True
                    break
        results.append({
            'name': sig['name'],
            'direction': sig['direction'],
            'entry': entry,
            'win': win,
            'features': sig.get('features', []),
        })
    report = {
        'date': datetime.now(TIMEZONE).strftime('%Y-%m-%d'),
        'signals': results,
        'total': len(results),
        'wins': sum(1 for r in results if r['win']),
    }
    with open(REPORT_FILE, 'w') as f:
        json.dump(report, f, indent=2)

    # Aktualizacja bazy zdarzeń na podstawie wyników (prosty mechanizm)
    # Można rozbudować: jeśli sygnał był trafiony, zwiększ wagę zdarzeń z nim związanych.
    events = load_events_impact()
    # Przykładowa aktualizacja – zostawiamy bez zmian na razie
    save_events_impact(events)

    msg = f"📊 *Podsumowanie dnia {report['date']}*\n✅ Trafne: {report['wins']}/{report['total']}\n❌ Chybione: {report['total']-report['wins']}\n"
    send_telegram(msg)

def main():
    if is_weekend():
        print("Weekend - agent nie pracuje.")
        return

    if is_morning_report_time():
        morning_report()
        return

    if is_training_time():
        train_model_from_report()
        return

    if is_summary_time():
        end_of_day_summary()
        return

    print(f"Analiza rynków: {datetime.now(TIMEZONE)}")
    sm = SignalManager()
    model = load_model()
    potential = []
    for name, info in MARKETS.items():
        signal = analyze_market(name, info)
        if signal:
            prob = predict_probability(signal['features'], model)
            signal['ml_probability'] = prob
            if prob < 0.5:
                signal['confidence'] *= 0.8
            else:
                signal['confidence'] *= min(1.2, 1 + prob)
            if sm.should_send_signal(signal):
                potential.append(signal)
    if potential:
        sorted_sigs = sorted(potential, key=lambda x: (x['confidence'], x['potential']), reverse=True)[:10]
        msg = "🚨 *TOP SYGNAŁY*\n"
        for i, s in enumerate(sorted_sigs, 1):
            emoji = '🟢' if s['direction'] == 'LONG' else '🔴'
            msg += f"{i}. {emoji} {s['name']} ({s['direction']}) Pewność: {s['confidence']:.0%}, ML: {s.get('ml_probability',0.5):.0%}\n"
        send_telegram(msg)
    else:
        print("Brak sygnałów.")

if __name__ == "__main__":
    main()