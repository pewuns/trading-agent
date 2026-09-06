import os
import json
import time
import socket
import ssl
import logging
import requests
import numpy as np
from datetime import datetime
import pytz
import re
import xml.etree.ElementTree as ET

# ============================================
# LOGOWANIE (zamiast cichych `except: pass`)
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('trading_bot')

# Tokeny
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# Pliki
SIGNALS_FILE = 'signals_history.json'
AI_MEMORY_FILE = 'ai_memory.json'
TIMEFRAME_WEIGHTS_FILE = 'timeframe_weights.json'

TIMEZONE = pytz.timezone('Europe/Warsaw')

# Zarządzanie ryzykiem (konfigurowalne przez zmienne środowiskowe)
ACCOUNT_SIZE = float(os.environ.get('ACCOUNT_SIZE', 10000))
RISK_PER_TRADE_PERCENT = float(os.environ.get('RISK_PER_TRADE_PERCENT', 1.0))
MAX_PER_CLUSTER = int(os.environ.get('MAX_PER_CLUSTER', 2))
SIGNAL_TIMEOUT_HOURS = float(os.environ.get('SIGNAL_TIMEOUT_HOURS', 48))

DISCLAIMER = (
    "\n\n⚠️ _To automatyczny, niebacktestowany system analityczny. "
    "Nie jest to porada inwestycyjna. Handel wiąże się z ryzykiem straty kapitału. "
    "Zweryfikuj sygnały samodzielnie przed podjęciem decyzji._"
)

# ============================================
# XTB xAPI - realny spread bid/ask
# ============================================
# UWAGA: Ten klient nie był testowany na żywym połączeniu z serwerami XTB
# (środowisko developerskie nie ma dostępu do sieci). Przed produkcyjnym
# użyciem przetestuj na koncie DEMO i zweryfikuj format odpowiedzi z aktualną
# dokumentacją: http://developers.xstore.pro/documentation
# Jeśli logowanie się nie powiedzie lub dane logowania nie są ustawione,
# kod automatycznie spada z powrotem na proxy zmienności (estimate_spread_proxy).
XTB_LOGIN = os.environ.get('XTB_LOGIN')
XTB_PASSWORD = os.environ.get('XTB_PASSWORD')
XTB_ACCOUNT_TYPE = os.environ.get('XTB_ACCOUNT_TYPE', 'demo')  # 'demo' albo 'real'

XTB_HOSTS = {
    'demo': ('xapi.xtb.com', 5124),
    'real': ('xapi.xtb.com', 5112),
}

# Mapowanie nazw rynków z MARKETS na symbole XTB.
# TE TICKERY WYMAGAJĄ WERYFIKACJI w xStation5 (Narzędzia > Specyfikacja
# instrumentów) - XTB czasem zmienia/różnicuje symbole między kontami.
XTB_SYMBOLS = {
    'DAX': 'DE30',
    'S&P500': 'US500',
    'NASDAQ': 'US100',
    'EUR/USD': 'EURUSD',
    'GOLD': 'GOLD',
    'OIL WTI': 'OIL.WTI',
    'BITCOIN': 'BITCOIN',
    'ETHEREUM': 'ETHEREUM',
    'SOLANA': 'SOLANA',
    'APPLE': 'APPLE.US',
    'MICROSOFT': 'MICROSOFT.US',
    'NVIDIA': 'NVIDIA.US',
    'TESLA': 'TESLA.US',
    'AMAZON': 'AMAZON.US',
    'META': 'META.US',
    'GOOGLE': 'ALPHABET.US',
}

# Maksymalny akceptowalny spread jako % ceny środkowej, liczony z REALNYCH
# danych bid/ask z XTB. To są znacznie ciaśniejsze progi niż proxy zmienności,
# bo to już jest prawdziwy koszt transakcyjny, nie przybliżenie.
MAX_REAL_SPREAD_PERCENT = {
    'forex': 0.0004,
    'index': 0.0015,
    'commodity': 0.002,
    'crypto': 0.006,
    'stock': 0.0015,
}


class XTBClient:
    """Minimalny klient do XTB xAPI (protokół JSON po TCP+SSL).
    Loguje się raz, pozwala pobrać dane symbolu (w tym bid/ask), wylogowuje się.
    Nie implementuje streamingu ani utrzymywania sesji (ping) - jest pomyślany
    do jednorazowego pobrania spreadów w ramach jednego cyklu analizy, nie do
    długo działającej sesji."""

    def __init__(self, login, password, account_type='demo'):
        self.login = login
        self.password = password
        self.host, self.port = XTB_HOSTS.get(account_type, XTB_HOSTS['demo'])
        self.sock = None

    def connect(self):
        raw_sock = socket.create_connection((self.host, self.port), timeout=10)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw_sock, server_hostname=self.host)

    def _send(self, command_dict):
        payload = json.dumps(command_dict) + "\n"
        self.sock.sendall(payload.encode('utf-8'))

    def _receive(self, buffer_size=8192, timeout=10):
        self.sock.settimeout(timeout)
        chunks = []
        while True:
            chunk = self.sock.recv(buffer_size)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b'\n'):
                break
        raw = b''.join(chunks).decode('utf-8').strip()
        return json.loads(raw) if raw else None

    def login_session(self):
        try:
            self.connect()
            self._send({
                "command": "login",
                "arguments": {"userId": self.login, "password": self.password}
            })
            resp = self._receive()
            if not resp or not resp.get('status'):
                logger.error(f"Logowanie do XTB nie powiodło się: {resp}")
                return False
            return True
        except (socket.error, ssl.SSLError, json.JSONDecodeError, OSError) as e:
            logger.error(f"Błąd połączenia z XTB: {e}")
            return False

    def get_symbol_spread(self, symbol):
        """Zwraca {'bid','ask','spread_abs','spread_percent'} albo None."""
        try:
            self._send({"command": "getSymbol", "arguments": {"symbol": symbol}})
            resp = self._receive()
            if not resp or not resp.get('status'):
                logger.warning(f"XTB getSymbol({symbol}) nieudane: {resp}")
                return None
            data = resp.get('returnData', {})
            bid = data.get('bid')
            ask = data.get('ask')
            if bid is None or ask is None:
                return None
            mid = (bid + ask) / 2
            if mid == 0:
                return None
            spread_abs = ask - bid
            return {
                'bid': bid,
                'ask': ask,
                'spread_abs': spread_abs,
                'spread_percent': spread_abs / mid,
            }
        except (socket.timeout, socket.error, json.JSONDecodeError) as e:
            logger.warning(f"Błąd komunikacji z XTB dla {symbol}: {e}")
            return None

    def logout(self):
        try:
            self._send({"command": "logout"})
            self._receive(timeout=5)
        except Exception:
            pass
        finally:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass


def fetch_xtb_spreads():
    """Loguje się RAZ do XTB i pobiera spready dla wszystkich zmapowanych rynków.
    Zwraca {market_name: spread_info}. Pusty dict (bez wyjątku) jeśli brak
    danych logowania albo połączenie się nie powiedzie - w takim wypadku
    analyze_market spada z powrotem na proxy zmienności."""
    if not (XTB_LOGIN and XTB_PASSWORD):
        return {}
    client = XTBClient(XTB_LOGIN, XTB_PASSWORD, XTB_ACCOUNT_TYPE)
    results = {}
    try:
        if not client.login_session():
            return {}
        for market_name, xtb_symbol in XTB_SYMBOLS.items():
            info = client.get_symbol_spread(xtb_symbol)
            if info:
                results[market_name] = info
            else:
                logger.warning(f"Brak spreadu XTB dla {market_name} ({xtb_symbol}) - użyty zostanie fallback")
            time.sleep(0.2)  # nie zalewaj API zapytaniami
    except Exception as e:
        logger.error(f"Błąd sesji XTB: {e}")
    finally:
        client.logout()
    return results


def check_spread_real_or_proxy(market_type, xtb_spread_info, fallback_proxy):
    """Preferuje realny spread z XTB (bid/ask). Jeśli niedostępny (brak
    danych logowania, błąd połączenia, brak symbolu), spada na proxy
    zmienności i JAWNIE to zwraca jako źródło - żeby w sygnale było widać,
    czy to jest prawdziwy spread czy przybliżenie."""
    if xtb_spread_info and xtb_spread_info.get('spread_percent') is not None:
        sp = xtb_spread_info['spread_percent']
        ok = sp <= MAX_REAL_SPREAD_PERCENT.get(market_type, 0.002)
        return ok, 'xtb_real', sp
    ok = check_spread_proxy(market_type, fallback_proxy)
    return ok, 'proxy_zmiennosci', fallback_proxy

# Rynki
MARKETS = {
    'DAX': {'symbol': '^GDAXI', 'type': 'index', 'session': 'EU'},
    'S&P500': {'symbol': '^GSPC', 'type': 'index', 'session': 'US'},
    'NASDAQ': {'symbol': '^IXIC', 'type': 'index', 'session': 'US'},
    'EUR/USD': {'symbol': 'EURUSD=X', 'type': 'forex', 'session': 'EU_US'},
    'GOLD': {'symbol': 'GC=F', 'type': 'commodity', 'session': '24_7'},
    'OIL WTI': {'symbol': 'CL=F', 'type': 'commodity', 'session': '24_7'},
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

# Klastry korelacji - używane do limitowania jednoczesnej ekspozycji
CORRELATION_CLUSTERS = {
    'DAX': 'indices_eu',
    'S&P500': 'indices_us',
    'NASDAQ': 'indices_us',
    'EUR/USD': 'forex',
    'GOLD': 'metals',
    'OIL WTI': 'energy',
    'BITCOIN': 'crypto',
    'ETHEREUM': 'crypto',
    'SOLANA': 'crypto',
    'APPLE': 'tech_stocks',
    'MICROSOFT': 'tech_stocks',
    'NVIDIA': 'tech_stocks',
    'TESLA': 'tech_stocks',
    'AMAZON': 'tech_stocks',
    'META': 'tech_stocks',
    'GOOGLE': 'tech_stocks',
}

# Interwały. '4h' jest teraz PRAWDZIWYM interwałem 4h - agregowanym
# z rzeczywistych świec 60m (patrz resample_ohlcv), a nie kopią '1h'.
TIMEFRAMES = {
    '5m': {'interval': '5m', 'range': '1d', 'default_weight': 0.15},
    '15m': {'interval': '15m', 'range': '1d', 'default_weight': 0.20},
    '1h': {'interval': '60m', 'range': '5d', 'default_weight': 0.25},
    '4h': {'interval': '60m', 'range': '1mo', 'default_weight': 0.15, 'resample_from_60m': 4},
    '1d': {'interval': '1d', 'range': '3mo', 'default_weight': 0.25},
}

# Punktacja scoringu - jawnie zdefiniowana, żeby total był policzalny
# (patrz sekcja "GŁÓWNA ANALIZA RYNKU" - suma max punktów jednej strony = 10)
TOTAL_SCORE_POINTS = 10.0

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
# HTTP Z RETRY / BACKOFF
# ============================================

def http_get_with_retry(url, params=None, headers=None, timeout=10, retries=3, backoff=1.5):
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            logger.warning(f"HTTP {resp.status_code} dla {url} (próba {attempt + 1}/{retries})")
        except requests.RequestException as e:
            last_exc = e
            logger.warning(f"Błąd sieci dla {url}: {e} (próba {attempt + 1}/{retries})")
        time.sleep(backoff ** attempt)
    if last_exc:
        logger.error(f"Nie udało się pobrać {url} po {retries} próbach: {last_exc}")
    return None


def http_post_with_retry(url, json_payload=None, headers=None, timeout=25, retries=2, backoff=1.5):
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=json_payload, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            logger.warning(f"HTTP {resp.status_code} dla {url} (próba {attempt + 1}/{retries})")
        except requests.RequestException as e:
            last_exc = e
            logger.warning(f"Błąd sieci dla {url}: {e} (próba {attempt + 1}/{retries})")
        time.sleep(backoff ** attempt)
    if last_exc:
        logger.error(f"Nie udało się wysłać do {url} po {retries} próbach: {last_exc}")
    return None

# ============================================
# FILTRY BEZPIECZEŃSTWA
# ============================================

def estimate_spread_proxy(highs, lows, closes):
    """
    UWAGA: Yahoo Finance nie udostępnia realnego spreadu bid/ask.
    To jest PROXY zmienności śróddziennej (średni (high-low)/close z ostatnich
    świec), a NIE prawdziwy spread. Do handlu na żywo podłącz realny spread
    z feedu brokera - to pole jest tylko dodatkowym filtrem "czy rynek jest
    w danej chwili wyjątkowo szeroki/niestabilny", nie zamiennikiem spreadu.
    """
    if len(closes) < 5:
        return None
    window_h = highs[-5:]
    window_l = lows[-5:]
    window_c = closes[-5:]
    ranges = [(h - l) / c for h, l, c in zip(window_h, window_l, window_c) if c]
    if not ranges:
        return None
    return sum(ranges) / len(ranges)


def check_spread_proxy(market_type, spread_proxy):
    max_allowed = {
        'forex': 0.0006,
        'index': 0.003,
        'commodity': 0.004,
        'crypto': 0.01,
        'stock': 0.004,
    }
    if spread_proxy is None:
        # Brak danych - nie blokujemy sygnału, ale to nie jest to samo co "spread OK"
        return True
    return spread_proxy <= max_allowed.get(market_type, 0.004)


def check_extreme_volatility(atr_percent, market_type):
    volatility_limits = {
        'forex': 1.5,
        'index': 3.0,
        'commodity': 5.0,
        'crypto': 8.0,
        'stock': 5.0,
    }
    return atr_percent <= volatility_limits.get(market_type, 3.0)


def check_liquidity(volume, market_type):
    min_volume = {
        'forex': 1000000,
        'index': 100000,
        'commodity': 50000,
        'crypto': 100,
        'stock': 1000000,
    }
    return volume >= min_volume.get(market_type, 100000)

# ============================================
# WSKAŹNIKI TECHNICZNE
# ============================================

def calculate_vwap(prices, volumes):
    if not prices or not volumes or len(volumes) < len(prices):
        return None
    total_volume = sum(volumes)
    if total_volume == 0:
        return None
    return sum(p * v for p, v in zip(prices, volumes)) / total_volume


def calculate_support_resistance(prices, lookback=20):
    if len(prices) < lookback:
        return {'support': None, 'resistance': None, 'nearest_level': None}
    recent = prices[-lookback:]
    support = min(recent)
    resistance = max(recent)
    current = prices[-1]
    distance_to_support = current - support
    distance_to_resistance = resistance - current
    nearest = 'SUPPORT' if distance_to_support < distance_to_resistance else 'RESISTANCE'
    return {'support': support, 'resistance': resistance, 'nearest_level': nearest}


def detect_candlestick_patterns(opens, highs, lows, closes):
    patterns = []
    if len(closes) < 3 or not opens:
        return patterns

    o1, c1 = opens[-1], closes[-1]
    h1, l1 = highs[-1], lows[-1]
    o2, c2 = opens[-2], closes[-2]

    body = abs(c1 - o1)
    lower_shadow = min(o1, c1) - l1
    upper_shadow = h1 - max(o1, c1)
    total_range = h1 - l1 if h1 != l1 else 1

    if lower_shadow > 2 * body and upper_shadow < body:
        patterns.append('MŁOT (byczy)')
    if upper_shadow > 2 * body and lower_shadow < body:
        patterns.append('SPADAJĄCA GWIAZDA (niedźwiedzi)')
    if c2 < o2 and c1 > o1 and c1 > o2 and o1 < c2:
        patterns.append('OBJĘCIE HOSSY (bycze)')
    if c2 > o2 and c1 < o1 and c1 < o2 and o1 > c2:
        patterns.append('OBJĘCIE BESSY (niedźwiedzie)')
    if body < total_range * 0.1:
        patterns.append('DOJI (niezdecydowanie)')

    return patterns


def patterns_directional_bias(patterns):
    """Zwraca 'bull', 'bear' albo None - liczone RAZ, nie per-formacja,
    żeby kilka formacji jednocześnie nie zawyżało score'u sztucznie."""
    has_bull = any('bycz' in p.lower() or 'hossy' in p.lower() for p in patterns)
    has_bear = any('niedźwiedzi' in p.lower() or 'bessy' in p.lower() for p in patterns)
    if has_bull and not has_bear:
        return 'bull'
    if has_bear and not has_bull:
        return 'bear'
    return None


def calculate_order_flow(closes, volumes):
    if len(closes) < 2 or not volumes:
        return {'delta': 0, 'delta_percent': 0}

    buy_volume = 0
    sell_volume = 0
    for i in range(1, len(closes)):
        if i < len(volumes):
            if closes[i] > closes[i - 1]:
                buy_volume += volumes[i]
            elif closes[i] < closes[i - 1]:
                sell_volume += volumes[i]

    delta = buy_volume - sell_volume
    total = buy_volume + sell_volume
    return {'delta': delta, 'delta_percent': (delta / total * 100) if total > 0 else 0}


def calculate_volume_profile(prices, volumes, bins=10):
    if len(prices) < 10 or not volumes:
        return None
    min_price = min(prices)
    max_price = max(prices)
    price_range = max_price - min_price
    if price_range == 0:
        return None

    bin_size = price_range / bins
    profile = {}
    for i in range(bins):
        bin_low = min_price + i * bin_size
        bin_high = min_price + (i + 1) * bin_size
        bin_volume = 0
        for j in range(len(prices)):
            if bin_low <= prices[j] < bin_high and j < len(volumes):
                bin_volume += volumes[j]
        profile[f"{bin_low:.2f}"] = bin_volume

    poc = max(profile, key=profile.get)
    return {'poc': float(poc), 'profile': profile}

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
        except Exception as e:
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return {'lessons': []}

    def save(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.memory, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Nie udało się zapisać {self.file_path}: {e}")

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
    """
    Wagi interwałów, które teraz REALNIE się uczą: po zamknięciu sygnału
    (TP/SL/timeout) update_from_outcome() wzmacnia wagi tych interwałów,
    których trend zgadzał się z trafnym kierunkiem, i osłabia te, które
    się myliły. Wagi są znormalizowane do sumy 1 i przycięte do [0.05, 0.5],
    żeby żaden interwał nie zdominował ani nie zniknął całkowicie.
    """
    MIN_WEIGHT = 0.05
    MAX_WEIGHT = 0.5
    LEARNING_RATE = 0.02

    def __init__(self, file_path=TIMEFRAME_WEIGHTS_FILE):
        self.file_path = file_path
        self.weights = self.load()

    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    loaded = json.load(f)
                    # upewnij się, że wszystkie znane interwały mają wagę
                    for tf, cfg in TIMEFRAMES.items():
                        loaded.setdefault(tf, cfg['default_weight'])
                    return loaded
        except Exception as e:
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return {tf: config['default_weight'] for tf, config in TIMEFRAMES.items()}

    def save(self):
        try:
            with open(self.file_path, 'w') as f:
                json.dump(self.weights, f, indent=2)
        except Exception as e:
            logger.error(f"Nie udało się zapisać {self.file_path}: {e}")

    def get_weights(self):
        return self.weights

    def update_from_outcome(self, signal, outcome):
        if outcome not in ('win', 'loss'):
            return
        tf_trends = signal.get('timeframe_trends', {})
        direction = signal.get('direction')
        expected_trend = 'UP' if direction == 'LONG' else 'DOWN'
        sign = 1 if outcome == 'win' else -1

        for tf, trend in tf_trends.items():
            if tf not in self.weights:
                continue
            if trend == expected_trend:
                delta = sign * self.LEARNING_RATE
            else:
                delta = -sign * self.LEARNING_RATE * 0.5
            self.weights[tf] = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, self.weights[tf] + delta))

        total = sum(self.weights.values())
        if total > 0:
            for tf in self.weights:
                self.weights[tf] /= total
        self.save()
        logger.info(f"Wagi interwałów zaktualizowane po sygnale {signal.get('name')} ({outcome}): {self.weights}")


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
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return {}

    def save_signals(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.signals, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Nie udało się zapisać {self.file_path}: {e}")

    def should_send_signal(self, signal):
        key = f"{signal['name']}_{signal['direction']}"
        if key not in self.signals:
            self._store(key, signal)
            return True
        previous = self.signals[key]
        price_diff = abs(signal['entry'] - previous['entry']) / previous['entry'] * 100
        prev_time = datetime.fromisoformat(previous['timestamp'])
        minutes_diff = (datetime.now(pytz.utc) - prev_time).total_seconds() / 60
        if price_diff >= 0.3 or minutes_diff >= 30:
            self._store(key, signal)
            return True
        return False

    def _store(self, key, signal):
        signal = dict(signal)
        signal['timestamp'] = datetime.now(pytz.utc).isoformat()
        signal['status'] = 'open'
        self.signals[key] = signal
        self.save_signals()

    def evaluate_open_signals(self, tf_weights, max_age_hours=SIGNAL_TIMEOUT_HOURS):
        """Sprawdza otwarte sygnały: czy trafiły TP, SL, albo wygasły.
        Zamyka je i karmi wynikiem TimeframeWeights, żeby wagi realnie się uczyły."""
        now = datetime.now(pytz.utc)
        any_update = False
        for key, sig in list(self.signals.items()):
            if sig.get('status') != 'open':
                continue
            try:
                sig_time = datetime.fromisoformat(sig['timestamp'])
            except Exception:
                continue
            age_hours = (now - sig_time).total_seconds() / 3600

            market_info = MARKETS.get(sig.get('name'))
            if not market_info:
                continue
            data = get_market_data(market_info['symbol'], '15m', '1d')
            if not data or not data['prices']:
                continue
            current_price = data['prices'][-1]
            direction = sig['direction']

            hit_tp = (direction == 'LONG' and current_price >= sig['take_profit']) or \
                     (direction == 'SHORT' and current_price <= sig['take_profit'])
            hit_sl = (direction == 'LONG' and current_price <= sig['stop_loss']) or \
                     (direction == 'SHORT' and current_price >= sig['stop_loss'])

            outcome = None
            if hit_tp:
                outcome = 'win'
            elif hit_sl:
                outcome = 'loss'
            elif age_hours >= max_age_hours:
                outcome = 'timeout'

            if outcome:
                sig['status'] = 'closed'
                sig['outcome'] = outcome
                sig['closed_price'] = current_price
                sig['closed_at'] = now.isoformat()
                tf_weights.update_from_outcome(sig, outcome)
                any_update = True

        if any_update:
            self.save_signals()

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


def is_session_active(session):
    """Naprawione: obsługuje też 'EU_US' i '24_7', które wcześniej
    nie miały żadnej reguły i przechodziły bez ograniczeń godzinowych."""
    now = datetime.now(TIMEZONE)
    hour = now.hour
    if session == '24_7':
        return True
    if session == 'US':
        return 15 <= hour < 22
    if session == 'EU':
        return 9 <= hour < 17
    if session == 'EU_US':
        return 9 <= hour < 22
    return True


def send_telegram(message, add_disclaimer=True):
    if add_disclaimer:
        message = message + DISCLAIMER
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
        resp = http_post_with_retry(url, json_payload=payload, timeout=10, retries=3)
        return resp is not None
    except Exception as e:
        logger.error(f"Błąd Telegram: {e}")
        return False


def get_market_data(symbol, interval='15m', range_period='1d'):
    """
    NAPRAWIONE: wcześniej close/high/low/volume/open filtrowane były osobno,
    więc przy różnych pozycjach None w poszczególnych polach indeksy
    przestawały się zgadzać między tablicami (świeca closes[i] mogła nie
    odpowiadać highs[i]). Teraz wiersze są wyrównywane RAZEM i odrzucane
    tylko wtedy, gdy którekolwiek pole w danym wierszu jest None.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {'interval': interval, 'range': range_period}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    resp = http_get_with_retry(url, params=params, headers=headers, timeout=10)
    if resp is None:
        return None
    try:
        data = resp.json()
        result = data['chart']['result'][0]
        quotes = result['indicators']['quote'][0]
        closes = quotes.get('close', []) or []
        highs = quotes.get('high', []) or []
        lows = quotes.get('low', []) or []
        volumes = quotes.get('volume', []) or []
        opens = quotes.get('open', []) or []

        n = min(len(closes), len(highs), len(lows), len(volumes), len(opens))
        aligned = []
        for i in range(n):
            row = (opens[i], highs[i], lows[i], closes[i], volumes[i])
            if None not in row:
                aligned.append(row)

        if len(aligned) < 10:
            logger.warning(f"Za mało poprawnych, wyrównanych świec dla {symbol}")
            return None

        opens_a, highs_a, lows_a, closes_a, volumes_a = zip(*aligned)
        return {
            'prices': list(closes_a),
            'highs': list(highs_a),
            'lows': list(lows_a),
            'volumes': list(volumes_a),
            'opens': list(opens_a),
        }
    except (KeyError, IndexError, TypeError) as e:
        logger.exception(f"Błąd parsowania danych {symbol}: {e}")
        return None


def resample_ohlcv(data, group_size=4):
    """Agreguje mniejsze świece (np. 60m) w większe (np. 4h), tworząc
    PRAWDZIWE świece 4h zamiast kopiować dane z '1h' pod inną etykietą."""
    if not data or len(data['prices']) < group_size:
        return None
    opens, highs, lows, closes, volumes = (
        data['opens'], data['highs'], data['lows'], data['prices'], data['volumes']
    )
    n = len(closes)
    r_opens, r_highs, r_lows, r_closes, r_volumes = [], [], [], [], []
    for i in range(0, n, group_size):
        c_o = opens[i:i + group_size]
        c_h = highs[i:i + group_size]
        c_l = lows[i:i + group_size]
        c_c = closes[i:i + group_size]
        c_v = volumes[i:i + group_size]
        if not c_c:
            continue
        r_opens.append(c_o[0])
        r_highs.append(max(c_h))
        r_lows.append(min(c_l))
        r_closes.append(c_c[-1])
        r_volumes.append(sum(c_v))
    if len(r_closes) < 5:
        return None
    return {'prices': r_closes, 'highs': r_highs, 'lows': r_lows, 'volumes': r_volumes, 'opens': r_opens}


def calculate_base_indicators(data):
    if not data or len(data['prices']) < 20:
        return None
    prices = data['prices']
    highs = data['highs']
    lows = data['lows']
    current_price = prices[-1]

    def sma(arr, period):
        if len(arr) < period:
            return None
        return sum(arr[-period:]) / period

    def rsi(arr, period=14):
        if len(arr) < period + 1:
            return 50
        gains, losses = [], []
        for i in range(1, len(arr)):
            change = arr[i] - arr[i - 1]
            gains.append(max(0, change))
            losses.append(max(0, -change))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100
        return 100 - (100 / (1 + avg_gain / avg_loss))

    def atr(highs, lows, closes, period=14):
        if len(closes) < period + 1:
            return 0
        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        return sum(trs[-period:]) / period

    sma20 = sma(prices, 20)
    sma50 = sma(prices, 50)
    rsi_val = rsi(prices)
    atr_val = atr(highs, lows, prices)
    atr_percent = (atr_val / current_price) * 100 if current_price else 0
    avg_volume = sum(data['volumes'][-20:]) / min(len(data['volumes']), 20) if data.get('volumes') else 0

    return {
        'price': current_price,
        'sma20': sma20,
        'sma50': sma50,
        'rsi': rsi_val,
        'atr': atr_val,
        'atr_percent': atr_percent,
        'avg_volume': avg_volume,
        'highs': highs,
        'lows': lows,
        'opens': data.get('opens', []),
        'volumes': data.get('volumes', []),
    }


def calculate_full_indicators(data):
    base = calculate_base_indicators(data)
    if not base:
        return None

    prices = data['prices']
    volumes = data.get('volumes', [])
    highs = data['highs']
    lows = data['lows']
    opens = data.get('opens', [])

    vwap = calculate_vwap(prices, volumes)
    sr_levels = calculate_support_resistance(prices)
    patterns = detect_candlestick_patterns(opens, highs, lows, prices)
    order_flow = calculate_order_flow(prices, volumes)
    volume_profile = calculate_volume_profile(prices, volumes)
    spread_proxy = estimate_spread_proxy(highs, lows, prices)

    base['vwap'] = vwap
    base['support'] = sr_levels.get('support')
    base['resistance'] = sr_levels.get('resistance')
    base['nearest_level'] = sr_levels.get('nearest_level')
    base['candlestick_patterns'] = patterns
    base['order_flow'] = order_flow
    base['volume_profile'] = volume_profile
    base['spread_proxy'] = spread_proxy

    return base

# ============================================
# ANALIZA WIELOINTERWAŁOWA
# ============================================

def determine_trend(ind):
    trend_count = 0
    if ind['price'] and ind['sma20'] and ind['price'] > ind['sma20']:
        trend_count += 1
    if ind['price'] and ind['sma50'] and ind['price'] > ind['sma50']:
        trend_count += 1
    if ind['sma20'] and ind['sma50'] and ind['sma20'] > ind['sma50']:
        trend_count += 1
    if trend_count >= 2:
        return 'UP'
    if trend_count == 0:
        return 'DOWN'
    return 'SIDEWAYS'


def analyze_timeframes(symbol, timeframe_weights):
    """NAPRAWIONE: '4h' jest teraz agregowany z tych samych danych 60m
    (resample_ohlcv), a nie pobierany osobno jako duplikat '1h' pod inną nazwą."""
    timeframe_results = {}
    cache_60m = {}
    for tf_name, tf_config in TIMEFRAMES.items():
        if tf_config.get('resample_from_60m'):
            range_key = tf_config['range']
            if range_key not in cache_60m:
                cache_60m[range_key] = get_market_data(symbol, '60m', range_key)
            raw = cache_60m[range_key]
            data = resample_ohlcv(raw, tf_config['resample_from_60m']) if raw else None
        else:
            data = get_market_data(symbol, tf_config['interval'], tf_config['range'])

        if data:
            ind = calculate_base_indicators(data)
            if ind:
                ind['weight'] = timeframe_weights.get(tf_name, tf_config['default_weight'])
                ind['trend'] = determine_trend(ind)
                timeframe_results[tf_name] = ind
    return timeframe_results if timeframe_results else None


def combine_timeframe_analysis(timeframe_results):
    if not timeframe_results:
        return None
    combined = {'trend_score': 0, 'momentum_score': 0, 'total_weight': 0, 'details': {}}
    for tf_name, ind in timeframe_results.items():
        weight = ind.get('weight', 0.2)
        combined['total_weight'] += weight
        trend = 0
        if ind['price'] and ind['sma20'] and ind['price'] > ind['sma20']:
            trend += 1
        if ind['price'] and ind['sma50'] and ind['price'] > ind['sma50']:
            trend += 1
        if ind['sma20'] and ind['sma50'] and ind['sma20'] > ind['sma50']:
            trend += 1
        combined['trend_score'] += (trend / 3) * weight
        momentum = 0.5 if ind['rsi'] > 50 else 0
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
        alignment = '✅' if (trend == 'UP' and rsi > 50) or (trend == 'DOWN' and rsi < 50) else '❌'
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
        resp = http_get_with_retry(url, timeout=10, retries=2)
        if resp is None:
            return []
        root = ET.fromstring(resp.content)
        articles = []
        for item in root.findall('.//item'):
            title_el = item.find('title')
            title = title_el.text if title_el is not None else ''
            if title:
                articles.append({'title': title})
        return articles[:limit]
    except ET.ParseError as e:
        logger.warning(f"Błąd parsowania RSS dla '{query}': {e}")
    except Exception as e:
        logger.warning(f"Błąd pobierania newsów dla '{query}': {e}")
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
    resp = http_post_with_retry(url, json_payload=payload, headers=headers, timeout=25, retries=2)
    if resp is None:
        return None
    try:
        return resp.json()['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as e:
        logger.warning(f"Błąd parsowania odpowiedzi Groq: {e}")
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
            except json.JSONDecodeError as e:
                logger.warning(f"Nie udało się sparsować JSON z analizy newsów: {e}")
    return {'sentiment': 0, 'impact': 'low', 'direction': 'neutral', 'reasoning': ''}

# ============================================
# ZARZĄDZANIE RYZYKIEM
# ============================================

def calculate_position_size(entry, stop_loss, account_size=ACCOUNT_SIZE, risk_percent=RISK_PER_TRADE_PERCENT):
    """Wielkość pozycji tak, by strata przy SL = risk_percent% kapitału."""
    risk_amount = account_size * (risk_percent / 100)
    stop_distance = abs(entry - stop_loss)
    if stop_distance == 0:
        return 0, risk_amount
    return risk_amount / stop_distance, risk_amount

# ============================================
# GŁÓWNA ANALIZA RYNKU
# ============================================

def analyze_market(name, market_info, ai_memory, timeframe_weights, xtb_spreads=None):
    session = market_info.get('session', '24_7')
    if not is_session_active(session):
        return None

    main_data = get_market_data(market_info['symbol'], '15m', '1d')
    if not main_data:
        return None

    ind = calculate_full_indicators(main_data)
    if not ind:
        return None

    market_type = market_info['type']
    current_price = ind['price']
    atr_percent = ind['atr_percent']
    avg_volume = ind['avg_volume']

    # FILTRY BEZPIECZEŃSTWA
    xtb_info = (xtb_spreads or {}).get(name)
    spread_ok, spread_source, spread_value = check_spread_real_or_proxy(
        market_type, xtb_info, ind['spread_proxy']
    )
    if not spread_ok:
        logger.info(f"❌ {name}: spread zbyt wysoki (źródło: {spread_source}, wartość: {spread_value})")
        return None
    if not check_extreme_volatility(atr_percent, market_type):
        logger.info(f"❌ {name}: ekstremalna zmienność (ATR: {atr_percent:.2f}%)")
        return None
    if not check_liquidity(avg_volume, market_type):
        logger.info(f"❌ {name}: za mała płynność (wolumen: {avg_volume:.0f})")
        return None

    timeframe_results = analyze_timeframes(market_info['symbol'], timeframe_weights)
    if not timeframe_results:
        return None

    combined = combine_timeframe_analysis(timeframe_results)
    if not combined:
        return None

    divergences = detect_divergences(timeframe_results)

    articles = fetch_market_news(name)
    news_analysis = analyze_news_with_ai(articles, name, ai_memory.get_context())

    p = ind['price']
    long_score = 0.0
    short_score = 0.0

    # --- Głosy bazowe (każdy głos trafia do JEDNEJ strony - bez podwójnego liczenia) ---
    if ind['sma20']:
        if p > ind['sma20']:
            long_score += 1
        else:
            short_score += 1
    if ind['sma50']:
        if p > ind['sma50']:
            long_score += 1
        else:
            short_score += 1
    if ind['rsi'] > 50:
        long_score += 1
    else:
        short_score += 1
    if ind['vwap']:
        if p > ind['vwap']:
            long_score += 1
        else:
            short_score += 1

    # --- Wsparcia/opory (0.5 do jednej strony) ---
    if ind['nearest_level'] == 'SUPPORT':
        long_score += 0.5
    elif ind['nearest_level'] == 'RESISTANCE':
        short_score += 0.5

    # --- Formacje świecowe (liczone RAZ, nie per formacja) ---
    bias = patterns_directional_bias(ind['candlestick_patterns'])
    if bias == 'bull':
        long_score += 0.5
    elif bias == 'bear':
        short_score += 0.5

    # --- Order Flow ---
    if ind['order_flow']:
        if ind['order_flow']['delta_percent'] > 0:
            long_score += 0.5
        elif ind['order_flow']['delta_percent'] < 0:
            short_score += 0.5

    # --- Volume Profile (POC) ---
    if ind['volume_profile']:
        poc = ind['volume_profile']['poc']
        if p > poc:
            long_score += 0.5
        else:
            short_score += 0.5

    # --- MTF (multi-timeframe trend) ---
    if combined['trend_score'] > 0.6:
        long_score += 2
    elif combined['trend_score'] < 0.4:
        short_score += 2

    # --- Kara za dywergencje (obniża obie strony - sygnał mniej pewny) ---
    if divergences:
        long_score = max(0, long_score - 0.5)
        short_score = max(0, short_score - 0.5)

    # --- Newsy (tylko przy wysokim impakcie) ---
    sentiment = news_analysis.get('sentiment', 0) if news_analysis else 0
    if news_analysis and news_analysis.get('impact') == 'high':
        long_score += max(0, sentiment) * 2
        short_score += max(0, -sentiment) * 2

    # Confidence teraz jest jawnie ograniczone do [0, 1] - wcześniej mogło
    # przekroczyć 100% przy sprzyjających bonusach.
    long_conf = min(1.0, max(0.0, long_score / TOTAL_SCORE_POINTS))
    short_conf = min(1.0, max(0.0, short_score / TOTAL_SCORE_POINTS))

    threshold = 0.7
    if long_conf >= threshold or short_conf >= threshold:
        direction = 'LONG' if long_conf >= short_conf else 'SHORT'
        confidence = max(long_conf, short_conf)

        stop_loss = p - 1.5 * ind['atr'] if direction == 'LONG' else p + 1.5 * ind['atr']
        take_profit = p + 2.5 * ind['atr'] if direction == 'LONG' else p - 2.5 * ind['atr']
        position_size, risk_amount = calculate_position_size(p, stop_loss)

        return {
            'name': name,
            'direction': direction,
            'entry': p,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'position_size': position_size,
            'risk_amount': risk_amount,
            'confidence': confidence,
            'rsi': ind['rsi'],
            'vwap': ind['vwap'],
            'support': ind['support'],
            'resistance': ind['resistance'],
            'candlestick_patterns': ind['candlestick_patterns'],
            'order_flow_delta': ind['order_flow']['delta_percent'] if ind['order_flow'] else 0,
            'volume_profile_poc': ind['volume_profile']['poc'] if ind['volume_profile'] else None,
            'news_sentiment': news_analysis.get('sentiment', 0),
            'news_impact': news_analysis.get('impact', 'low'),
            'news_reasoning': news_analysis.get('reasoning', ''),
            'timeframe_alignment': combined['trend_score'],
            'timeframe_trends': {tf: r.get('trend') for tf, r in timeframe_results.items()},
            'divergences': divergences,
            'heatmap': create_heatmap(timeframe_results),
            'spread_source': spread_source,
            'spread_value': spread_value,
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
    prompt = (
        f"Przeanalizuj globalny sentyment:\n{news_text}\n"
        f'Odpowiedz w JSON: {{"global_sentiment": <-1 do 1>, "risk_appetite": <string>, "summary": <string>}}'
    )
    result = call_groq("Analityk globalnych rynków.", prompt, max_tokens=300)
    if result:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                message = "🌅 *PORANNY RAPORT*\n\n"
                message += f"Sentyment: {data.get('global_sentiment', 0):.2f}\n"
                message += f"Apetyt: {data.get('risk_appetite', 'neutral')}\n\n"
                message += f"{data.get('summary', '')}"
                send_telegram(message)
            except json.JSONDecodeError as e:
                logger.warning(f"Nie udało się sparsować JSON sentymentu: {e}")


def generate_weekly_report():
    sm = SignalManager()
    current_week = datetime.now(TIMEZONE).strftime('%Y-W%V')
    week_signals = []
    wins = losses = 0
    for key, sig in sm.signals.items():
        try:
            sig_time = datetime.fromisoformat(sig.get('timestamp', '2000-01-01T00:00:00+00:00'))
            if sig_time.strftime('%Y-W%V') == current_week:
                week_signals.append(sig)
                if sig.get('outcome') == 'win':
                    wins += 1
                elif sig.get('outcome') == 'loss':
                    losses += 1
        except Exception:
            pass
    win_rate = f"{wins}/{wins+losses}" if (wins + losses) else "brak zamkniętych"
    prompt = (
        f"Raport tygodniowy. Sygnały: {len(week_signals)}. "
        f"Trafność zamkniętych sygnałów: {win_rate}. Napisz po polsku."
    )
    result = call_groq("Analityk rynkowy.", prompt, max_tokens=500)
    if result:
        send_telegram(f"📊 *RAPORT TYGODNIOWY*\n\n{result}")


def generate_monthly_report():
    sm = SignalManager()
    wins = sum(1 for s in sm.signals.values() if s.get('outcome') == 'win')
    losses = sum(1 for s in sm.signals.values() if s.get('outcome') == 'loss')
    prompt = (
        f"Raport miesięczny. Sygnały: {len(sm.signals)}. "
        f"Wygrane: {wins}, przegrane: {losses}. Napisz po polsku."
    )
    result = call_groq("Analityk rynkowy.", prompt, max_tokens=600)
    if result:
        send_telegram(f"📊 *RAPORT MIESIĘCZNY*\n\n{result}")

# ============================================
# GŁÓWNA PĘTLA
# ============================================

def apply_cluster_limits(signals, max_per_cluster=MAX_PER_CLUSTER):
    """Limituje liczbę jednoczesnych sygnałów w tym samym klastrze
    korelacji (np. max 2 z 'tech_stocks' naraz), żeby nie otwierać
    de facto tej samej skorelowanej ekspozycji wielokrotnie."""
    cluster_counts = {}
    result = []
    for s in signals:
        cluster = CORRELATION_CLUSTERS.get(s['name'], 'other')
        if cluster_counts.get(cluster, 0) >= max_per_cluster:
            logger.info(f"Pominięto {s['name']} - limit ekspozycji na klaster '{cluster}' osiągnięty")
            continue
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        result.append(s)
    return result


def main():
    if is_weekend():
        logger.info("Weekend - agent nie pracuje.")
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

    logger.info(f"Analiza rynków: {datetime.now(TIMEZONE)}")
    sm = SignalManager()
    ai_memory = AIMemory()
    tf_weights = TimeframeWeights()

    # Najpierw ocena wcześniej wysłanych, wciąż otwartych sygnałów -
    # to jest to, co realnie zasila naukę wag interwałów.
    sm.evaluate_open_signals(tf_weights)

    # Jedno logowanie do XTB na cały cykl (nie per rynek) - taniej i szybciej.
    # Jeśli XTB_LOGIN/XTB_PASSWORD nie są ustawione albo logowanie się nie
    # powiedzie, zwraca {} i każdy rynek automatycznie spada na proxy zmienności.
    xtb_spreads = fetch_xtb_spreads()
    if xtb_spreads:
        logger.info(f"Pobrano realne spready z XTB dla {len(xtb_spreads)}/{len(XTB_SYMBOLS)} rynków")
    elif XTB_LOGIN and XTB_PASSWORD:
        logger.warning("Dane logowania XTB ustawione, ale nie udało się pobrać spreadów - fallback na proxy")

    potential = []
    for name, info in MARKETS.items():
        signal = analyze_market(name, info, ai_memory, tf_weights.get_weights(), xtb_spreads)
        if signal and sm.should_send_signal(signal):
            potential.append(signal)

    if potential:
        sorted_sigs = sorted(potential, key=lambda x: x['confidence'], reverse=True)
        final_sigs = apply_cluster_limits(sorted_sigs)[:10]

        msg = "🚨 *TOP SYGNAŁY*\n\n"
        for i, s in enumerate(final_sigs, 1):
            emoji = '🟢' if s['direction'] == 'LONG' else '🔴'
            msg += f"{i}. {emoji} {s['name']} ({s['direction']})\n"
            msg += f"   Pewność: {s['confidence']:.0%}\n"
            msg += f"   Wejście: {s['entry']:.4f} | SL: {s['stop_loss']:.4f} | TP: {s['take_profit']:.4f}\n"
            spread_label = "spread XTB (realny)" if s['spread_source'] == 'xtb_real' else "proxy zmienności (przybliżenie)"
            msg += f"   Spread: {s['spread_value']*100:.3f}% [{spread_label}]\n"
            msg += f"   Sugerowana wielkość pozycji: {s['position_size']:.4f} jedn. (ryzyko ~{s['risk_amount']:.2f})\n"
            msg += f"   RSI: {s['rsi']:.1f}\n"
            if s['vwap']:
                msg += f"   VWAP: {s['vwap']:.4f}\n"
            if s['support']:
                msg += f"   Wsparcie: {s['support']:.4f}\n"
            if s['resistance']:
                msg += f"   Opór: {s['resistance']:.4f}\n"
            if s['candlestick_patterns']:
                msg += f"   Formacje: {', '.join(s['candlestick_patterns'][:3])}\n"
            if s['order_flow_delta']:
                msg += f"   Order Flow: {s['order_flow_delta']:.1f}%\n"
            if s['volume_profile_poc']:
                msg += f"   POC: {s['volume_profile_poc']:.4f}\n"
            msg += f"   Sentyment: {s['news_sentiment']:.2f}\n"
            if s['divergences']:
                msg += f"   ⚠️ Dywergencje: {len(s['divergences'])}\n"
            msg += f"\n{s['heatmap']}\n\n"

        send_telegram(msg)
        ai_memory.add_lesson(f"Wysłano {len(final_sigs)} sygnałów")
    else:
        logger.info("Brak sygnałów.")


if __name__ == "__main__":
    main()
