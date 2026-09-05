import os
import requests
import json
from datetime import datetime, timedelta, timezone
import hashlib
import pytz
from pathlib import Path

# Token z GitHub Secrets
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
NEWS_API_KEY = os.environ.get('NEWS_API_KEY', '')

# Plik do przechowywania historii sygnałów
SIGNALS_FILE = 'signals_history.json'

# Strefa czasowa (Europe/Warsaw dla polskich godzin)
TIMEZONE = pytz.timezone('Europe/Warsaw')

# Godziny pracy agenta
WORK_HOURS_START = 8  # 8:00 rano
WORK_HOURS_END = 19   # 19:00 wieczorem
WORK_DAYS = [0, 1, 2, 3, 4]  # 0=Poniedziałek, 4=Piątek

# Rynki do monitorowania
MARKETS = {
    'S&P500': {'symbol': '^GSPC', 'country': 'USA', 'keywords': ['US', 'America', 'Fed', 'Wall Street']},
    'NASDAQ': {'symbol': '^IXIC', 'country': 'USA', 'keywords': ['tech', 'Nasdaq', 'Apple', 'Microsoft']},
    'DAX': {'symbol': '^GDAXI', 'country': 'Germany', 'keywords': ['Germany', 'German', 'DAX', 'ECB']},
    'FTSE100': {'symbol': '^FTSE', 'country': 'UK', 'keywords': ['UK', 'Britain', 'London', 'BoE']},
    'CAC40': {'symbol': '^FCHI', 'country': 'France', 'keywords': ['France', 'French', 'Paris']},
    'NIKKEI': {'symbol': '^N225', 'country': 'Japan', 'keywords': ['Japan', 'Japanese', 'BoJ', 'Tokyo']},
    'HANG SENG': {'symbol': '^HSI', 'country': 'China', 'keywords': ['China', 'Chinese', 'Hong Kong']},
    'WIG20': {'symbol': 'WIG20.WA', 'country': 'Poland', 'keywords': ['Poland', 'Polish', 'Warsaw', 'NBP']},
    'EUR/USD': {'symbol': 'EURUSD=X', 'country': 'EU', 'keywords': ['Euro', 'ECB', 'Europe', 'EU']},
    'USD/JPY': {'symbol': 'JPY=X', 'country': 'Japan', 'keywords': ['Yen', 'BoJ', 'Japan', 'USD']},
    'GOLD': {'symbol': 'GC=F', 'country': 'Global', 'keywords': ['gold', 'precious metals', 'safe haven']},
    'OIL WTI': {'symbol': 'CL=F', 'country': 'Global', 'keywords': ['oil', 'crude', 'OPEC', 'energy']},
}

# Słowa kluczowe dla sentymentu
POSITIVE_WORDS = [
    'growth', 'profit', 'increase', 'positive', 'bullish', 'rally', 'gain',
    'recovery', 'expansion', 'success', 'improvement', 'record', 'surge',
    'boom', 'upgrade', 'outperform', 'strong', 'beat', 'exceed'
]

NEGATIVE_WORDS = [
    'recession', 'decline', 'loss', 'negative', 'bearish', 'crash', 'crisis',
    'downturn', 'collapse', 'risk', 'warning', 'debt', 'inflation', 'default',
    'bankrupt', 'layoff', 'downgrade', 'underperform', 'weak', 'miss', 'below'
]

CRITICAL_EVENTS = [
    'rate decision', 'rate hike', 'rate cut', 'fomc', 'ecb', 'boj', 'boe',
    'non-farm', 'nfp', 'cpi', 'inflation data', 'gdp', 'unemployment',
    'fed chair', 'central bank', 'monetary policy'
]


class SignalManager:
    """Zarządzanie historią sygnałów z naprawionym zapisem"""
    
    def __init__(self, file_path=SIGNALS_FILE):
        self.file_path = file_path
        self.signals = self.load_signals()
        print(f"📂 Wczytano {len(self.signals)} historycznych sygnałów")
    
    def load_signals(self):
        """Wczytaj historię sygnałów z pliku"""
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content.strip():
                        return json.loads(content)
        except Exception as e:
            print(f"❌ Błąd wczytywania historii: {e}")
        return {}
    
    def save_signals(self):
        """Zapisz historię sygnałów do pliku"""
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.signals, f, indent=2, ensure_ascii=False)
            print(f"💾 Zapisano {len(self.signals)} sygnałów do pliku")
            return True
        except Exception as e:
            print(f"❌ Błąd zapisywania historii: {e}")
            return False
    
    def get_signal_key(self, signal):
        """Generuj klucz sygnału"""
        return f"{signal['name']}_{signal['direction']}"
    
    def should_send_signal(self, signal):
        """
        Sprawdź czy sygnał powinien być wysłany.
        Sygnał jest wysyłany jeśli:
        1. Nie ma wcześniejszego sygnału dla tego instrumentu i kierunku
        2. Cena zmieniła się o więcej niż 0.3% od ostatniego sygnału
        3. Minęło więcej niż 30 minut od ostatniego sygnału
        4. Zmienił się sentyment newsów o więcej niż 0.3
        5. Pojawiły się nowe krytyczne wydarzenia
        """
        key = self.get_signal_key(signal)
        
        # Jeśli nie ma wcześniejszego sygnału - wyślij
        if key not in self.signals:
            print(f"✅ Nowy sygnał dla {key}")
            self._add_signal(signal, key)
            return True
        
        previous = self.signals[key]
        
        # Sprawdź różnicę cen
        price_diff = abs(signal['entry'] - previous['entry']) / previous['entry'] * 100
        
        # Sprawdź różnicę czasu
        prev_time = datetime.fromisoformat(previous['timestamp'])
        time_diff = datetime.now(timezone.utc) - prev_time
        minutes_diff = time_diff.total_seconds() / 60
        
        # Sprawdź różnicę sentymentu
        sentiment_diff = abs(signal['news_sentiment'] - previous.get('news_sentiment', 0))
        
        # Sprawdź czy są nowe krytyczne wydarzenia
        new_critical = len(signal.get('critical_events', [])) - len(previous.get('critical_events', []))
        
        # Kryteria wysyłki nowego sygnału
        should_send = False
        reason = ""
        
        if price_diff >= 0.3:
            should_send = True
            reason = f"cena zmieniła się o {price_diff:.2f}%"
        
        elif minutes_diff >= 30:
            should_send = True
            reason = f"minęło {minutes_diff:.0f} minut"
        
        elif sentiment_diff >= 0.3:
            should_send = True
            reason = f"sentyment zmienił się o {sentiment_diff:.2f}"
        
        elif new_critical > 0:
            should_send = True
            reason = f"pojawiło się {new_critical} nowych wydarzeń"
        
        if should_send:
            print(f"🔄 Aktualizacja sygnału {key}: {reason}")
            self._add_signal(signal, key)
            return True
        else:
            print(f"⏭️ Pominięto {key}: brak istotnych zmian (cena: {price_diff:.2f}%, czas: {minutes_diff:.0f}min)")
            return False
    
    def _add_signal(self, signal, key):
        """Dodaj lub zaktualizuj sygnał w historii"""
        self.signals[key] = {
            'name': signal['name'],
            'direction': signal['direction'],
            'entry': signal['entry'],
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'confidence': signal['confidence'],
            'rsi': signal['rsi'],
            'news_sentiment': signal.get('news_sentiment', 0),
            'critical_events_count': len(signal.get('critical_events', [])),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        self.save_signals()
    
    def clean_old_signals(self, max_age_hours=12):
        """Usuń sygnały starsze niż max_age_hours"""
        current_time = datetime.now(timezone.utc)
        signals_to_remove = []
        
        for key, signal in self.signals.items():
            try:
                signal_time = datetime.fromisoformat(signal['timestamp'])
                if current_time - signal_time > timedelta(hours=max_age_hours):
                    signals_to_remove.append(key)
            except:
                signals_to_remove.append(key)
        
        for key in signals_to_remove:
            del self.signals[key]
        
        if signals_to_remove:
            print(f"🗑️ Usunięto {len(signals_to_remove)} starych sygnałów")
            self.save_signals()


class NewsAnalyzer:
    """Analiza wiadomości i sentymentu"""
    
    def __init__(self, api_key=''):
        self.api_key = api_key
        self.news_cache = {}
        self.cache_timeout = 15  # minut
    
    def fetch_news_fallback(self, keywords):
        """Pobierz wiadomości z Google News RSS"""
        try:
            query = ' OR '.join(keywords[:3])
            url = f"https://news.google.com/rss/search?q={query}&hl=en"
            
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(response.content)
                
                articles = []
                for item in root.findall('.//item'):
                    title = item.find('title').text if item.find('title') is not None else ''
                    pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ''
                    
                    articles.append({
                        'title': title,
                        'publishedAt': pub_date,
                    })
                
                return articles[:15]
        
        except Exception as e:
            print(f"❌ Błąd pobierania newsów: {e}")
        
        return []
    
    def analyze_sentiment(self, articles):
        """Analiza sentymentu artykułów"""
        if not articles:
            return {'score': 0, 'positive': 0, 'negative': 0, 'total': 0}
        
        positive_count = 0
        negative_count = 0
        total_relevant = 0
        
        for article in articles:
            title = article.get('title', '').lower()
            text = title
            
            pos_matches = sum(1 for word in POSITIVE_WORDS if word in text)
            neg_matches = sum(1 for word in NEGATIVE_WORDS if word in text)
            
            if pos_matches > 0 or neg_matches > 0:
                total_relevant += 1
                if pos_matches > neg_matches:
                    positive_count += 1
                elif neg_matches > pos_matches:
                    negative_count += 1
        
        score = 0
        if total_relevant > 0:
            score = (positive_count - negative_count) / total_relevant
        
        return {
            'score': score,
            'positive': positive_count,
            'negative': negative_count,
            'total': total_relevant,
        }
    
    def check_critical_events(self, articles):
        """Sprawdź czy są krytyczne wydarzenia ekonomiczne"""
        if not articles:
            return []
        
        critical_news = []
        
        for article in articles:
            title = article.get('title', '').lower()
            
            for event in CRITICAL_EVENTS:
                if event in title:
                    critical_news.append({
                        'title': article.get('title', ''),
                        'event': event,
                    })
                    break
        
        return critical_news
    
    def get_market_news(self, market_name, market_info):
        """Pobierz i analizuj wiadomości dla rynku"""
        keywords = market_info.get('keywords', [market_name])
        
        # Sprawdź cache
        cache_key = market_name
        if cache_key in self.news_cache:
            cached_data = self.news_cache[cache_key]
            time_diff = datetime.now() - cached_data['timestamp']
            if time_diff < timedelta(minutes=self.cache_timeout):
                return cached_data['data']
        
        # Pobierz wiadomości
        articles = self.fetch_news_fallback(keywords)
        
        # Analizuj
        sentiment = self.analyze_sentiment(articles)
        critical_events = self.check_critical_events(articles)
        
        data = {
            'sentiment': sentiment,
            'critical_events': critical_events,
            'articles_count': len(articles),
        }
        
        # Zapisz w cache
        self.news_cache[cache_key] = {
            'timestamp': datetime.now(),
            'data': data,
        }
        
        return data


def is_working_hours():
    """Sprawdź czy jesteśmy w godzinach pracy"""
    now = datetime.now(TIMEZONE)
    
    # Sprawdź dzień tygodnia (0=Poniedziałek, 6=Niedziela)
    if now.weekday() not in WORK_DAYS:
        print(f"📅 Weekend - agent nie pracuje ({now.strftime('%A')})")
        return False
    
    # Sprawdź godzinę
    if now.hour < WORK_HOURS_START or now.hour >= WORK_HOURS_END:
        print(f"🕐 Poza godzinami pracy: {now.strftime('%H:%M')}")
        return False
    
    return True


def get_market_data(symbol):
    """Pobierz dane rynkowe z Yahoo Finance"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            'interval': '5m',
            'range': '1d',
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)'
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        data = response.json()
        
        if 'chart' not in data or not data['chart']['result']:
            return None
        
        result = data['chart']['result'][0]
        quotes = result['indicators']['quote'][0]
        
        prices = [p for p in quotes['close'] if p is not None]
        highs = [h for h in quotes['high'] if h is not None]
        lows = [l for l in quotes['low'] if l is not None]
        
        if not prices or len(prices) < 50:
            return None
        
        current_price = prices[-1]
        
        def sma(data, period):
            if len(data) < period:
                return None
            return sum(data[-period:]) / period
        
        def rsi(data, period=14):
            if len(data) < period + 1:
                return 50
            gains = []
            losses = []
            for i in range(1, len(data)):
                change = data[i] - data[i-1]
                gains.append(max(0, change))
                losses.append(max(0, -change))
            
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            
            if avg_loss == 0:
                return 100
            
            rs = avg_gain / avg_loss
            return 100 - (100 / (1 + rs))
        
        def adx(highs, lows, closes, period=14):
            if len(closes) < period + 1:
                return 0
            
            tr_values = []
            plus_dm = []
            minus_dm = []
            
            for i in range(1, len(closes)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1])
                )
                tr_values.append(tr)
                
                up_move = highs[i] - highs[i-1]
                down_move = lows[i-1] - lows[i]
                
                plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
                minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
            
            if len(tr_values) < period:
                return 0
            
            atr = sum(tr_values[-period:]) / period
            if atr == 0:
                return 0
            
            plus_di = 100 * sum(plus_dm[-period:]) / period / atr
            minus_di = 100 * sum(minus_dm[-period:]) / period / atr
            
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) != 0 else 0
            
            return dx
        
        sma20 = sma(prices, 20)
        sma50 = sma(prices, 50)
        rsi_value = rsi(prices)
        adx_value = adx(highs, lows, prices)
        
        # ATR
        tr_values = []
        for i in range(1, len(prices)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - prices[i-1]),
                abs(lows[i] - prices[i-1])
            )
            tr_values.append(tr)
        
        atr = sum(tr_values[-14:]) / 14 if tr_values else current_price * 0.02
        
        return {
            'price': current_price,
            'sma20': sma20,
            'sma50': sma50,
            'rsi': rsi_value,
            'adx': adx_value,
            'atr': atr,
        }
        
    except Exception as e:
        print(f"❌ Błąd pobierania {symbol}: {e}")
        return None


def analyze_market(name, market_info, news_data):
    """Analiza rynku z uwzględnieniem newsów"""
    data = get_market_data(market_info['symbol'])
    if not data:
        return None
    
    if data['sma20'] is None or data['sma50'] is None:
        return None
    
    price = data['price']
    sma20 = data['sma20']
    sma50 = data['sma50']
    rsi = data['rsi']
    adx = data['adx']
    atr = data['atr']
    
    news_sentiment = news_data['sentiment']['score'] if news_data else 0
    critical_events = news_data['critical_events'] if news_data else []
    
    # Warunki LONG
    long_conditions = [
        price > sma20,           # Cena powyżej SMA20
        price > sma50,           # Cena powyżej SMA50
        30 < rsi < 70,           # RSI w zakresie
        rsi > 50,                # Momentum dodatnie
        adx > 20,                # Silny trend
        news_sentiment > -0.3,   # Sentyment nie jest bardzo negatywny
        len(critical_events) < 3 # Mniej niż 3 krytyczne wydarzenia
    ]
    
    # Warunki SHORT
    short_conditions = [
        price < sma20,           # Cena poniżej SMA20
        price < sma50,           # Cena poniżej SMA50
        30 < rsi < 70,           # RSI w zakresie
        rsi < 50,                # Momentum ujemne
        adx > 20,                # Silny trend
        news_sentiment < 0.3,    # Sentyment nie jest bardzo pozytywny
        len(critical_events) < 3 # Mniej niż 3 krytyczne wydarzenia
    ]
    
    long_score = sum(long_conditions) / len(long_conditions)
    short_score = sum(short_conditions) / len(short_conditions)
    
    # Próg pewności
    confidence_threshold = 0.7  # 70% warunków musi być spełnionych
    
    if long_score >= confidence_threshold:
        return {
            'name': name,
            'direction': 'LONG',
            'entry': price,
            'stop_loss': price - 1.5 * atr,
            'take_profit': price + 2.5 * atr,
            'confidence': long_score,
            'rsi': rsi,
            'adx': adx,
            'atr': atr,
            'news_sentiment': news_sentiment,
            'critical_events': critical_events,
            'news_count': news_data.get('articles_count', 0) if news_data else 0,
        }
    elif short_score >= confidence_threshold:
        return {
            'name': name,
            'direction': 'SHORT',
            'entry': price,
            'stop_loss': price + 1.5 * atr,
            'take_profit': price - 2.5 * atr,
            'confidence': short_score,
            'rsi': rsi,
            'adx': adx,
            'atr': atr,
            'news_sentiment': news_sentiment,
            'critical_events': critical_events,
            'news_count': news_data.get('articles_count', 0) if news_data else 0,
        }
    
    return None


def send_telegram(signal):
    """Wyślij sygnał do Telegram"""
    try:
        emoji = '🟢' if signal['direction'] == 'LONG' else '🔴'
        sentiment_emoji = '📈' if signal['news_sentiment'] > 0.2 else '📉' if signal['news_sentiment'] < -0.2 else '➡️'
        
        events_text = ''
        if signal.get('critical_events'):
            events_text = '\n'.join([f"• {event['title'][:50]}..." for event in signal['critical_events'][:3]])
        
        message = f"""
{emoji} SYGNAŁ {signal['direction']} {emoji}

📊 Instrument: {signal['name']}
💰 Wejście: {signal['entry']:.4f}
🛑 Stop Loss: {signal['stop_loss']:.4f}
🎯 Take Profit: {signal['take_profit']:.4f}

📈 Wskaźniki:
• RSI: {signal['rsi']:.1f}
• ADX: {signal['adx']:.1f}
• ATR: {signal['atr']:.4f}

📰 Newsy:
• Sentyment: {sentiment_emoji} {signal['news_sentiment']:.2f}
• Liczba newsów: {signal.get('news_count', 0)}
• Krytyczne wydarzenia: {len(signal.get('critical_events', []))}

{events_text}

🎯 Pewność: {signal['confidence']:.0%}
⏰ {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}

ID: {hashlib.md5(f"{signal['name']}_{signal['direction']}_{signal['entry']}".encode()).hexdigest()[:8]}
"""
        
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
        }
        
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            print(f"✅ Wysłano: {signal['name']} {signal['direction']}")
            return True
        else:
            print(f"❌ Błąd Telegram: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Błąd wysyłania: {e}")
        return False


def main():
    """Główna funkcja"""
    print(f"🔄 Agent uruchomiony: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Sprawdź godziny pracy
    if not is_working_hours():
        print("⏸️ Agent wstrzymany - poza godzinami pracy")
        return
    
    # Inicjalizacja
    signal_manager = SignalManager()
    news_analyzer = NewsAnalyzer(api_key=NEWS_API_KEY)
    
    # Wyczyść stare sygnały
    signal_manager.clean_old_signals(12)
    
    print(f"📊 Rozpoczynam analizę {len(MARKETS)} rynków...")
    
    signals_found = 0
    signals_sent = 0
    signals_skipped = 0
    
    for name, market_info in MARKETS.items():
        try:
            # Pobierz newsy
            news_data = news_analyzer.get_market_news(name, market_info)
            
            # Analizuj rynek
            signal = analyze_market(name, market_info, news_data)
            
            if signal:
                signals_found += 1
                
                # Sprawdź czy wysłać
                if signal_manager.should_send_signal(signal):
                    if send_telegram(signal):
                        signals_sent += 1
                else:
                    signals_skipped += 1
            
        except Exception as e:
            print(f"❌ Błąd analizy {name}: {e}")
    
    print(f"""
📊 Podsumowanie ({datetime.now(TIMEZONE).strftime('%H:%M:%S')}):
   Znaleziono sygnałów: {signals_found}
   Wysłano: {signals_sent}
   Pominięto (duplikaty): {signals_skipped}
   Historia: {len(signal_manager.signals)} sygnałów w pliku
""")


if __name__ == "__main__":
    main()