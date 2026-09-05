import os
import requests
import json
from datetime import datetime, timedelta
import hashlib

# Token z GitHub Secrets
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# Plik do przechowywania historii sygnałów
SIGNALS_FILE = 'signals_history.json'

# Rynki do monitorowania
MARKETS = {
    'S&P500': '^GSPC',
    'NASDAQ': '^IXIC',
    'DAX': '^GDAXI',
    'FTSE100': '^FTSE',
    'CAC40': '^FCHI',
    'NIKKEI': '^N225',
    'HANG SENG': '^HSI',
    'WIG20': 'WIG20.WA',
    'EUR/USD': 'EURUSD=X',
    'USD/JPY': 'JPY=X',
    'GOLD': 'GC=F',
    'OIL WTI': 'CL=F',
}

class SignalManager:
    """Zarządzanie historią sygnałów"""
    
    def __init__(self, file_path=SIGNALS_FILE):
        self.file_path = file_path
        self.signals = self.load_signals()
    
    def load_signals(self):
        """Wczytaj historię sygnałów z pliku"""
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Błąd wczytywania historii: {e}")
        return {}
    
    def save_signals(self):
        """Zapisz historię sygnałów do pliku"""
        try:
            with open(self.file_path, 'w') as f:
                json.dump(self.signals, f, indent=2)
        except Exception as e:
            print(f"Błąd zapisywania historii: {e}")
    
    def generate_signal_key(self, signal):
        """Generuj unikalny klucz sygnału"""
        # Klucz oparty na instrumencie i kierunku
        key = f"{signal['name']}_{signal['direction']}"
        return key
    
    def is_duplicate(self, signal, threshold_percent=0.5):
        """Sprawdź czy sygnał jest duplikatem"""
        key = self.generate_signal_key(signal)
        
        if key not in self.signals:
            return False
        
        # Pobierz poprzedni sygnał
        previous = self.signals[key]
        
        # Sprawdź czy cena wejścia jest podobna (w zakresie threshold)
        previous_entry = previous['entry']
        current_entry = signal['entry']
        
        price_diff_percent = abs(current_entry - previous_entry) / previous_entry * 100
        
        # Sprawdź czy sygnał jest świeży (mniej niż 4 godziny)
        previous_time = datetime.fromisoformat(previous['timestamp'])
        time_diff = datetime.now() - previous_time
        
        # Jeśli cena jest podobna i sygnał jest świeży - to duplikat
        if price_diff_percent < threshold_percent and time_diff < timedelta(hours=4):
            return True
        
        return False
    
    def update_signal(self, signal):
        """Zaktualizuj historię sygnałów"""
        key = self.generate_signal_key(signal)
        
        # Sprawdź czy sygnał się zmienił
        if key in self.signals:
            previous = self.signals[key]
            previous_entry = previous['entry']
            current_entry = signal['entry']
            
            price_diff_percent = abs(current_entry - previous_entry) / previous_entry * 100
            
            # Jeśli cena zmieniła się o więcej niż 0.5% - to nowy sygnał
            if price_diff_percent < 0.5:
                # Zaktualizuj timestamp, ale nie wysyłaj
                return False
        
        # Zapisz nowy sygnał
        self.signals[key] = {
            'name': signal['name'],
            'direction': signal['direction'],
            'entry': signal['entry'],
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'confidence': signal['confidence'],
            'rsi': signal['rsi'],
            'timestamp': datetime.now().isoformat(),
        }
        
        self.save_signals()
        return True
    
    def clean_old_signals(self, max_age_hours=24):
        """Usuń sygnały starsze niż max_age_hours"""
        current_time = datetime.now()
        signals_to_remove = []
        
        for key, signal in self.signals.items():
            signal_time = datetime.fromisoformat(signal['timestamp'])
            if current_time - signal_time > timedelta(hours=max_age_hours):
                signals_to_remove.append(key)
        
        for key in signals_to_remove:
            del self.signals[key]
        
        if signals_to_remove:
            self.save_signals()


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
        
        if not prices:
            return None
        
        current_price = prices[-1]
        
        # Oblicz wskaźniki
        def sma(data, period):
            if len(data) < period:
                return None
            return sum(data[-period:]) / period
        
        def ema(data, period):
            if len(data) < period:
                return None
            multiplier = 2 / (period + 1)
            ema_value = data[0]
            for price in data[1:]:
                ema_value = (price - ema_value) * multiplier + ema_value
            return ema_value
        
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
        
        def macd(data, fast=12, slow=26, signal=9):
            if len(data) < slow + signal:
                return 0
            ema_fast = ema(data, fast)
            ema_slow = ema(data, slow)
            return ema_fast - ema_slow
        
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
        
        # Oblicz wszystkie wskaźniki
        sma20 = sma(prices, 20)
        sma50 = sma(prices, 50)
        sma200 = sma(prices, 200)
        ema20 = ema(prices, 20)
        rsi_value = rsi(prices)
        macd_value = macd(prices)
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
            'sma200': sma200,
            'ema20': ema20,
            'rsi': rsi_value,
            'macd': macd_value,
            'adx': adx_value,
            'atr': atr,
            'volume': quotes.get('volume', [0])[-1] if quotes.get('volume') else 0,
        }
        
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None


def analyze_market(name, symbol):
    """Analiza rynku"""
    data = get_market_data(symbol)
    if not data:
        return None
    
    if data['sma20'] is None or data['sma50'] is None:
        return None
    
    price = data['price']
    sma20 = data['sma20']
    sma50 = data['sma50']
    sma200 = data['sma200']
    ema20 = data['ema20']
    rsi = data['rsi']
    macd = data['macd']
    adx = data['adx']
    atr = data['atr']
    
    # Warunki LONG (bardziej rygorystyczne)
    long_conditions = []
    
    # Trend
    if price > sma20:
        long_conditions.append(True)
    else:
        long_conditions.append(False)
    
    if price > sma50:
        long_conditions.append(True)
    else:
        long_conditions.append(False)
    
    if sma200 and price > sma200:
        long_conditions.append(True)
    else:
        long_conditions.append(False)
    
    # Momentum
    if 30 < rsi < 70:
        long_conditions.append(True)
    else:
        long_conditions.append(False)
    
    if rsi > 50:
        long_conditions.append(True)
    else:
        long_conditions.append(False)
    
    if macd > 0:
        long_conditions.append(True)
    else:
        long_conditions.append(False)
    
    # Siła trendu
    if adx > 20:
        long_conditions.append(True)
    else:
        long_conditions.append(False)
    
    # Zmienność
    if atr > 0:
        long_conditions.append(True)
    else:
        long_conditions.append(False)
    
    # Warunki SHORT
    short_conditions = []
    
    # Trend
    if price < sma20:
        short_conditions.append(True)
    else:
        short_conditions.append(False)
    
    if price < sma50:
        short_conditions.append(True)
    else:
        short_conditions.append(False)
    
    if sma200 and price < sma200:
        short_conditions.append(True)
    else:
        short_conditions.append(False)
    
    # Momentum
    if 30 < rsi < 70:
        short_conditions.append(True)
    else:
        short_conditions.append(False)
    
    if rsi < 50:
        short_conditions.append(True)
    else:
        short_conditions.append(False)
    
    if macd < 0:
        short_conditions.append(True)
    else:
        short_conditions.append(False)
    
    # Siła trendu
    if adx > 20:
        short_conditions.append(True)
    else:
        short_conditions.append(False)
    
    # Zmienność
    if atr > 0:
        short_conditions.append(True)
    else:
        short_conditions.append(False)
    
    long_score = sum(long_conditions) / len(long_conditions)
    short_score = sum(short_conditions) / len(short_conditions)
    
    # Generuj sygnał tylko przy wysokiej pewności
    if long_score >= 0.75:  # 75% warunków spełnionych
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
        }
    elif short_score >= 0.75:
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
        }
    
    return None


def send_telegram(signal):
    """Wyślij sygnał do Telegram"""
    try:
        emoji = '🟢' if signal['direction'] == 'LONG' else '🔴'
        
        message = f"""
{emoji} SYGNAŁ {signal['direction']} {emoji}

📊 Instrument: {signal['name']}
💰 Wejście: {signal['entry']:.4f}
🛑 Stop Loss: {signal['stop_loss']:.4f}
🎯 Take Profit: {signal['take_profit']:.4f}
📈 RSI: {signal['rsi']:.1f}
📊 ADX: {signal['adx']:.1f}
📏 ATR: {signal['atr']:.4f}
🎯 Pewność: {signal['confidence']:.0%}
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

ID: {hashlib.md5(f"{signal['name']}_{signal['direction']}_{signal['entry']}".encode()).hexdigest()[:8]}
"""
        
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
        }
        
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            print(f"✅ Wysłano sygnał: {signal['name']} {signal['direction']}")
            return True
        else:
            print(f"❌ Błąd wysyłania: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Błąd Telegram: {e}")
        return False


def main():
    """Główna funkcja"""
    print(f"🔄 Agent uruchomiony: {datetime.now()}")
    
    # Inicjalizacja managera sygnałów
    signal_manager = SignalManager()
    
    # Wyczyść stare sygnały (starsze niż 24h)
    signal_manager.clean_old_signals(24)
    
    signals_found = 0
    signals_sent = 0
    signals_duplicated = 0
    
    for name, symbol in MARKETS.items():
        signal = analyze_market(name, symbol)
        
        if signal:
            signals_found += 1
            
            # Sprawdź czy sygnał nie jest duplikatem
            if not signal_manager.is_duplicate(signal):
                # Nowy sygnał - wyślij
                if signal_manager.update_signal(signal):
                    if send_telegram(signal):
                        signals_sent += 1
            else:
                signals_duplicated += 1
                print(f"⏭️ Duplikat pominięty: {name} {signal['direction']}")
    
    print(f"""
📊 Podsumowanie ({datetime.now().strftime('%H:%M:%S')}):
   Znaleziono sygnałów: {signals_found}
   Wysłano nowych: {signals_sent}
   Pominięto duplikatów: {signals_duplicated}
""")


if __name__ == "__main__":
    main()