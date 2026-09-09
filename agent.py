import os
import json
import time
import socket
import ssl
import logging
import requests
import numpy as np
from datetime import datetime, timedelta
import pytz
import re
import xml.etree.ElementTree as ET

from config import (
    AGENT_MODE, RunMode, is_feature_enabled, get_mode_prefix,
    CONFIDENCE_THRESHOLD, MTF_BULLISH_THRESHOLD, MTF_BEARISH_THRESHOLD,
    NEAR_MISS_SAMPLE_WEIGHT, NEAR_MISS_MIN_AGE_HOURS, NEAR_MISS_MAX_AGE_HOURS,
    MACRO_HOURS, DAILY_SUMMARY_HOUR,
)
from modes import (
    log_signal_per_mode, log_trade_outcome, get_notification_message,
    should_update_weights, should_record_stats, should_update_threshold,
    should_send_notifications,
)
from data_sources import (
    fetch_trading_economics_macro, fetch_investing_news, log_fetch_error,
)

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
FEATURE_WEIGHTS_FILE = 'feature_weights.json'
FEATURE_STORE_FILE = 'feature_store.jsonl'
ADAPTIVE_THRESHOLD_FILE = 'adaptive_thresholds.json'
PERFORMANCE_STATS_FILE = 'performance_stats.json'
CIRCUIT_BREAKER_FILE = 'circuit_breaker_state.json'
SHADOW_LOG_FILE = 'shadow_scoring_log.jsonl'

# --- Nowe pliki: błędy pobierania danych, near-miss, dane makro ---
DATA_FETCH_ERRORS_FILE = 'data_fetch_errors.jsonl'     # log błędów każdego źródła danych (Investing/TE/Yahoo)
NEAR_MISS_LOG_FILE = 'near_miss_log.jsonl'             # log KAŻDEJ próby scoringu (append-only, do audytu)
NEAR_MISS_PENDING_FILE = 'near_miss_pending.json'      # sygnały "co by było gdyby" czekające na ewaluację
MACRO_DATA_FILE = 'macro_data_history.jsonl'           # WSZYSTKIE dane makro (append-only, do nauki)
MACRO_STATE_FILE = 'macro_state.json'                  # ostatnie pobrania/analizy makro + cache sentymentu
DAILY_SCORES_CACHE_FILE = 'daily_scores_cache.json'     # max confidence per rynek DZIŚ, do podsumowania Top 3

TIMEZONE = pytz.timezone('Europe/Warsaw')

# Zarządzanie ryzykiem (konfigurowalne przez zmienne środowiskowe)
ACCOUNT_SIZE = float(os.environ.get('ACCOUNT_SIZE', 10000))
RISK_PER_TRADE_PERCENT = float(os.environ.get('RISK_PER_TRADE_PERCENT', 1.0))
MAX_PER_CLUSTER = int(os.environ.get('MAX_PER_CLUSTER', 2))
SIGNAL_TIMEOUT_HOURS = float(os.environ.get('SIGNAL_TIMEOUT_HOURS', 48))

# --- Uczenie się (punkt A) ---
MIN_SAMPLES_FOR_LEARNED_MODEL = int(os.environ.get('MIN_SAMPLES_FOR_LEARNED_MODEL', 30))
MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD = int(os.environ.get('MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD', 15))
MIN_SAMPLES_FOR_KELLY = int(os.environ.get('MIN_SAMPLES_FOR_KELLY', 20))

# --- Zarządzanie ryzykiem / circuit breaker (punkt B) ---
KELLY_FRACTION = float(os.environ.get('KELLY_FRACTION', 0.5))  # domyślnie "pół-Kelly" - bezpieczniej niż pełny Kelly
MAX_RISK_PERCENT_CAP = float(os.environ.get('MAX_RISK_PERCENT_CAP', 2.0))
DAILY_LOSS_LIMIT_R = float(os.environ.get('DAILY_LOSS_LIMIT_R', -3.0))    # w jednostkach R (wielokrotność ryzyka)
WEEKLY_LOSS_LIMIT_R = float(os.environ.get('WEEKLY_LOSS_LIMIT_R', -6.0))
REGIME_ADX_TREND_THRESHOLD = float(os.environ.get('REGIME_ADX_TREND_THRESHOLD', 25))

# --- Near-miss (punkt D): sygnały poniżej progu logowane i ewaluowane później,
# jako dodatkowe (przyciszone) źródło danych treningowych dla FeatureWeights.
# Progi/wagi (NEAR_MISS_MIN_AGE_HOURS, NEAR_MISS_MAX_AGE_HOURS, NEAR_MISS_SAMPLE_WEIGHT)
# są teraz w config.py, żeby kalibrację dało się zmieniać w jednym miejscu. ---
NEAR_MISS_ATR_SL_MULT = 1.5
NEAR_MISS_ATR_TP_MULT = 2.5

# --- Dane makro (Trading Economics + Investing.com, RSS - patrz data_sources.py) ---
# Godziny pobrania (MACRO_HOURS) i próg pewności (CONFIDENCE_THRESHOLD) są w config.py.

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

# ============================================
# Investing.com - GŁÓWNE źródło danych cenowych (świece OHLCV), zgodnie
# z ustaleniem. Yahoo Finance jest fallbackiem (patrz get_price_data),
# używanym automatycznie, gdy Investing.com nie odpowie/zawiedzie.
# ============================================
# `pair_id` = None => rynek na razie pomija Investing.com i idzie prosto na
# Yahoo, dopóki nie uzupełnisz ID (patrz instrukcja w fetch_investing_data).
INVESTING_PAIR_IDS = {
    'DAX': None,
    'S&P500': None,
    'NASDAQ': None,
    'EUR/USD': None,
    'GOLD': None,
    'OIL WTI': None,
    'BITCOIN': None,
    'ETHEREUM': None,
    'SOLANA': None,
    'APPLE': None,
    'MICROSOFT': None,
    'NVIDIA': None,
    'TESLA': None,
    'AMAZON': None,
    'META': None,
    'GOOGLE': None,
}

# Mapowanie interwałów agenta na kody Investing.com (resolution w minutach,
# zgodnie z ich wewnętrznym, niezudokumentowanym API - patrz zastrzeżenia
# w fetch_investing_data).
INVESTING_INTERVAL_MAP = {
    '5m': 5, '15m': 15, '60m': 60, '1d': 1440,
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

# Kody interwałów dla getChartLastRequest (w minutach, zgodnie z dokumentacją xAPI).
XTB_PERIODS = {
    'M1': 1, 'M5': 5, 'M15': 15, 'M30': 30,
    'H1': 60, 'H4': 240, 'D1': 1440, 'W1': 10080, 'MN1': 43200,
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

    def get_chart_history(self, symbol, period_minutes, start_ms):
        """
        Pobiera świece historyczne przez getChartLastRequest (dane od `start_ms`
        do teraz, w interwale `period_minutes`).

        UWAGA - WAŻNE ZASTRZEŻENIE: wg mojej pamięci dokumentacji xAPI pola
        `close`/`high`/`low` w odpowiedzi NIE są cenami bezwzględnymi, tylko
        PRZESUNIĘCIEM W PUNKTACH względem `open` tej samej świecy - trzeba je
        przeliczyć jako (open + offset) / 10**digits. Nie mogłem tego
        zweryfikować na żywym połączeniu (brak sieci w środowisku, w którym
        to piszę). Jeśli po pobraniu zobaczysz nierealistyczne świece (np.
        high < low, albo ceny o rzędy wielkości różne od rzeczywistych),
        sprawdź aktualny format w http://developers.xstore.pro/documentation
        i popraw konwersję poniżej - to jedyne miejsce, którego to dotyczy.
        """
        try:
            self._send({
                "command": "getChartLastRequest",
                "arguments": {"info": {"period": period_minutes, "start": start_ms, "symbol": symbol}}
            })
            resp = self._receive(buffer_size=65536, timeout=30)
            if not resp or not resp.get('status'):
                logger.warning(f"XTB getChartLastRequest({symbol}) nieudane: {resp}")
                return None
            data = resp.get('returnData', {})
            digits = data.get('digits', 4)
            scale = 10 ** digits
            rate_infos = data.get('rateInfos', [])
            bars = []
            for r in rate_infos:
                try:
                    open_p = r['open'] / scale
                    close_p = (r['open'] + r['close']) / scale
                    high_p = (r['open'] + r['high']) / scale
                    low_p = (r['open'] + r['low']) / scale
                    bars.append({
                        'timestamp': r.get('ctm'),
                        'open': open_p,
                        'high': high_p,
                        'low': low_p,
                        'close': close_p,
                        'volume': r.get('vol', 0),
                    })
                except (KeyError, TypeError, ZeroDivisionError):
                    continue
            bars.sort(key=lambda b: b['timestamp'] or 0)
            return bars if bars else None
        except (socket.timeout, socket.error, json.JSONDecodeError) as e:
            logger.warning(f"Błąd pobierania historii XTB dla {symbol}: {e}")
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


def fetch_xtb_historical(symbol, period='H1', months_back=12):
    """Loguje się do XTB, pobiera świece historyczne dla JEDNEGO symbolu na
    potrzeby backtestu, wylogowuje się. Zwraca listę słowników
    {'timestamp','open','high','low','close','volume'} w kolejności
    chronologicznej, albo None przy niepowodzeniu.

    Dostępna głębokość historii zależy od interwału i konta - dla niskich
    interwałów (M1/M5) XTB zwykle udostępnia dane tylko z ostatnich
    kilku-kilkunastu miesięcy, dla D1 znacznie dłużej. Jeśli dostaniesz
    pustą listę, spróbuj mniejszego `months_back` albo wyższego `period`.
    """
    if not (XTB_LOGIN and XTB_PASSWORD):
        logger.error("Brak XTB_LOGIN/XTB_PASSWORD - nie można pobrać danych historycznych z XTB.")
        return None
    period_minutes = XTB_PERIODS.get(period)
    if period_minutes is None:
        logger.error(f"Nieznany okres '{period}'. Dostępne: {list(XTB_PERIODS.keys())}")
        return None

    start_dt = datetime.now(pytz.utc) - timedelta(days=int(months_back * 30.44))
    start_ms = int(start_dt.timestamp() * 1000)

    client = XTBClient(XTB_LOGIN, XTB_PASSWORD, XTB_ACCOUNT_TYPE)
    bars = None
    try:
        if not client.login_session():
            return None
        bars = client.get_chart_history(symbol, period_minutes, start_ms)
    except Exception as e:
        logger.error(f"Błąd sesji XTB przy pobieraniu historii: {e}")
    finally:
        client.logout()
    return bars


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


def log_data_error(source, name, symbol, reason):
    """Zapisuje błąd pobierania danych do OSOBNEGO pliku (punkt: priorytet
    źródeł danych). Każde nieudane pobranie z Investing.com / Trading Economics
    / Yahoo trafia tutaj z nazwą źródła, rynkiem, symbolem i powodem - żeby
    dało się później ocenić, które źródło faktycznie zawodzi i jak często,
    zamiast zgadywać na podstawie samych logów tekstowych."""
    try:
        with open(DATA_FETCH_ERRORS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'timestamp': datetime.now(pytz.utc).isoformat(),
                'source': source,
                'name': name,
                'symbol': symbol,
                'reason': str(reason)[:300],
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Nie udało się zapisać do {DATA_FETCH_ERRORS_FILE}: {e}")
    logger.warning(f"[{source}] Błąd danych dla {name} ({symbol}): {reason}")

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


def calculate_adx(highs, lows, closes, period=14):
    """ADX (Average Directional Index) - siła trendu, niezależnie od kierunku.
    Używane do rozróżnienia reżimu TREND (warto podążać za trendem) od RANGE
    (rynek się konsoliduje, lepiej sprawdza się mean-reversion od wsparć/oporów)."""
    if len(closes) < period * 2:
        return None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    def wilder_smooth(arr, period):
        if len(arr) < period:
            return []
        smoothed = [sum(arr[:period])]
        for x in arr[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + x)
        return smoothed

    tr_s = wilder_smooth(trs, period)
    plus_dm_s = wilder_smooth(plus_dm, period)
    minus_dm_s = wilder_smooth(minus_dm, period)
    if not tr_s or not plus_dm_s or not minus_dm_s:
        return None

    n = min(len(tr_s), len(plus_dm_s), len(minus_dm_s))
    plus_di = [100 * (plus_dm_s[i] / tr_s[i]) if tr_s[i] else 0 for i in range(n)]
    minus_di = [100 * (minus_dm_s[i] / tr_s[i]) if tr_s[i] else 0 for i in range(n)]
    dx = [100 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i])
          if (plus_di[i] + minus_di[i]) else 0 for i in range(n)]

    if len(dx) < period:
        return sum(dx) / len(dx) if dx else None
    return sum(dx[-period:]) / period


def detect_market_regime(highs, lows, closes, trend_threshold=REGIME_ADX_TREND_THRESHOLD):
    """Zwraca 'TREND', 'RANGE' albo 'UNKNOWN' (za mało danych)."""
    adx = calculate_adx(highs, lows, closes)
    if adx is None:
        return 'UNKNOWN', None
    return ('TREND' if adx >= trend_threshold else 'RANGE'), adx

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
    Wagi interwałów, które REALNIE się uczą: po zamknięciu sygnału
    (TP/SL/timeout) update_from_outcome() wzmacnia wagi tych interwałów,
    których trend zgadzał się z trafnym kierunkiem, i osłabia te, które
    się myliły. Wagi są znormalizowane do sumy 1 i przycięte do [0.05, 0.5].

    NOWOŚĆ: learning rate maleje wraz z liczbą dotychczasowych aktualizacji
    danego interwału (LEARNING_RATE / sqrt(1+n)) - dzięki temu pojedynczy
    zamknięty sygnał na starcie (mała próbka) nie przesuwa wagi drastycznie,
    a wagi stabilizują się dopiero po wielu obserwacjach (EWMA-podobne
    wygładzanie zamiast gonienia szumu).
    """
    MIN_WEIGHT = 0.05
    MAX_WEIGHT = 0.5
    LEARNING_RATE = 0.03

    def __init__(self, file_path=TIMEFRAME_WEIGHTS_FILE):
        self.file_path = file_path
        state = self.load()
        self.weights = state['weights']
        self.update_counts = state['update_counts']

    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    loaded = json.load(f)
                    # kompatybilność wsteczna: stary format to płaski dict wag
                    if 'weights' not in loaded:
                        loaded = {'weights': loaded, 'update_counts': {}}
                    for tf, cfg in TIMEFRAMES.items():
                        loaded['weights'].setdefault(tf, cfg['default_weight'])
                        loaded['update_counts'].setdefault(tf, 0)
                    return loaded
        except Exception as e:
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return {
            'weights': {tf: config['default_weight'] for tf, config in TIMEFRAMES.items()},
            'update_counts': {tf: 0 for tf in TIMEFRAMES},
        }

    def save(self):
        try:
            with open(self.file_path, 'w') as f:
                json.dump({'weights': self.weights, 'update_counts': self.update_counts}, f, indent=2)
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
            n = self.update_counts.get(tf, 0)
            effective_lr = self.LEARNING_RATE / np.sqrt(1 + n)
            if trend == expected_trend:
                delta = sign * effective_lr
            else:
                delta = -sign * effective_lr * 0.5
            self.weights[tf] = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, self.weights[tf] + delta))
            self.update_counts[tf] = n + 1

        total = sum(self.weights.values())
        if total > 0:
            for tf in self.weights:
                self.weights[tf] /= total
        self.save()
        logger.info(f"Wagi interwałów zaktualizowane po sygnale {signal.get('name')} ({outcome}): {self.weights}")


class FeatureWeights:
    """
    Regresja logistyczna online (SGD) ucząca się wag POSZCZEGÓLNYCH CECH
    technicznych (nie tylko interwałów) na podstawie wyników zamkniętych
    sygnałów. Przewiduje P(ruch w górę) na podstawie wektora cech; dla
    sygnału LONG to jest wprost P(sukces), dla SHORT: 1 - P(ruch w górę).

    Dopóki liczba próbek < MIN_SAMPLES_FOR_LEARNED_MODEL, model NIE jest
    używany do podejmowania decyzji (patrz is_ready()) - do tego czasu
    scoring bazowy ze stałymi wagami pozostaje jedynym źródłem confidence,
    żeby nie uczyć się (i nie ufać) garści przypadkowych wyników.
    """
    FEATURE_NAMES = [
        'sma20', 'sma50', 'rsi', 'vwap', 'support_resistance',
        'pattern', 'order_flow', 'poc', 'mtf_trend', 'news_sentiment',
        'macro_sentiment',
    ]

    def __init__(self, file_path=FEATURE_WEIGHTS_FILE):
        self.file_path = file_path
        state = self.load()
        self.weights = state['weights']
        self.bias = state['bias']
        self.n_samples = state['n_samples']
        self.base_learning_rate = 0.05

    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return {'weights': {f: 0.0 for f in self.FEATURE_NAMES}, 'bias': 0.0, 'n_samples': 0}

    def save(self):
        try:
            with open(self.file_path, 'w') as f:
                json.dump({'weights': self.weights, 'bias': self.bias, 'n_samples': self.n_samples}, f, indent=2)
        except Exception as e:
            logger.error(f"Nie udało się zapisać {self.file_path}: {e}")

    def is_ready(self):
        return self.n_samples >= MIN_SAMPLES_FOR_LEARNED_MODEL

    def predict_proba_up(self, features):
        z = self.bias + sum(self.weights.get(k, 0.0) * v for k, v in features.items())
        z = max(-30.0, min(30.0, z))  # zabezpieczenie przed przepełnieniem exp()
        return 1.0 / (1.0 + np.exp(-z))

    def update(self, features, went_up, weight=1.0):
        """went_up: True jeśli faktyczny ruch rynku był w górę, False jeśli w dół.
        (Ustalane z outcome + direction sygnału - patrz SignalManager.evaluate_open_signals).

        weight: mnożnik kroku gradientu (< 1.0 = próbka uczy słabiej).
        Używane przez near-miss (punkt D) - "co by było gdyby" sygnały poniżej
        progu wciąż karmią model, ale z przyciszoną wagą (NEAR_MISS_SAMPLE_WEIGHT),
        żeby nie mieć takiego samego wpływu jak realnie wysłane, zweryfikowane
        sygnały.

        NAPRAWIONE: n_samples (który steruje zanikaniem learning_rate w czasie)
        rósł wcześniej o 1 przy KAŻDEJ próbce, także near-missach z weight=0.25.
        Skoro near-missów jest z założenia dużo więcej niż realnych sygnałów,
        learning_rate wygasał znacznie szybciej niż powinien względem faktycznej
        liczby "pełnowartościowych" obserwacji. Teraz n_samples rośnie o `weight`,
        więc 4 near-missy (0.25 każdy) liczą się tyle co 1 realny sygnał - spójnie
        z tym, jak słabo pojedynczy near-miss wpływa na same wagi."""
        y = 1.0 if went_up else 0.0
        p = self.predict_proba_up(features)
        error = p - y
        # learning rate maleje z liczbą próbek - mniej gwałtowne zmiany z czasem
        lr = self.base_learning_rate / (1 + self.n_samples / 50) * weight
        for k, v in features.items():
            self.weights[k] = self.weights.get(k, 0.0) - lr * error * v
        self.bias -= lr * error
        self.n_samples += weight
        self.save()


def build_feature_vector(ind, combined, news_analysis, divergences, macro_analysis=None):
    """Cechy w konwencji ZNAKOWANEJ: dodatnie = przechylenie w górę (byczo),
    ujemne = w dół (niedźwiedzio), 0 = brak/neutralne. Dzięki temu ta sama
    regresja logistyczna przewiduje P(ruch w górę) niezależnie od tego, czy
    ostatecznie interesuje nas sygnał LONG czy SHORT."""
    bias_pattern = patterns_directional_bias(ind.get('candlestick_patterns', []))
    pattern_val = 1.0 if bias_pattern == 'bull' else (-1.0 if bias_pattern == 'bear' else 0.0)

    sr_val = 0.0
    if ind.get('nearest_level') == 'SUPPORT':
        sr_val = 0.5  # blisko wsparcia = raczej byczo (odbicie w górę)
    elif ind.get('nearest_level') == 'RESISTANCE':
        sr_val = -0.5

    order_flow_val = 0.0
    if ind.get('order_flow'):
        order_flow_val = max(-1.0, min(1.0, ind['order_flow'].get('delta_percent', 0) / 100))

    news_val = 0.0
    if news_analysis and news_analysis.get('impact') == 'high':
        news_val = max(-1.0, min(1.0, news_analysis.get('sentiment', 0)))

    macro_val = 0.0
    if macro_analysis and macro_analysis.get('impact') in ('high', 'medium'):
        macro_val = max(-1.0, min(1.0, macro_analysis.get('sentiment', 0)))

    return {
        'sma20': 1.0 if (ind.get('sma20') and ind['price'] > ind['sma20']) else (
            -1.0 if ind.get('sma20') else 0.0),
        'sma50': 1.0 if (ind.get('sma50') and ind['price'] > ind['sma50']) else (
            -1.0 if ind.get('sma50') else 0.0),
        'rsi': (ind.get('rsi', 50) - 50) / 50.0,
        'vwap': 1.0 if (ind.get('vwap') and ind['price'] > ind['vwap']) else (
            -1.0 if ind.get('vwap') else 0.0),
        'support_resistance': sr_val,
        'pattern': pattern_val,
        'order_flow': order_flow_val,
        'poc': 1.0 if (ind.get('volume_profile') and ind['price'] > ind['volume_profile']['poc']) else (
            -1.0 if ind.get('volume_profile') else 0.0),
        'mtf_trend': (combined.get('trend_score', 0.5) - 0.5) * 2 if combined else 0.0,
        'news_sentiment': news_val,
        'macro_sentiment': macro_val,
    }


class AdaptiveThreshold:
    """
    Próg pewności (domyślnie 0.7) kalibrowany OSOBNO dla każdego rynku na
    podstawie historii jego sygnałów: dla każdego rynku szuka najniższego
    progu, przy którym win-rate sygnałów >= tego progu utrzymuje się na
    poziomie >= target_win_rate. Dopóki rynek ma mniej niż
    MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD zamkniętych sygnałów, używany jest
    bezpieczny domyślny próg 0.7.
    """
    DEFAULT = CONFIDENCE_THRESHOLD  # z config.py - jedno miejsce do kalibracji (obniżone z 0.7)
    TARGET_WIN_RATE = 0.55
    HISTORY_WINDOW = 200

    def __init__(self, file_path=ADAPTIVE_THRESHOLD_FILE):
        self.file_path = file_path
        self.data = self.load()

    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return {}

    def save(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"Nie udało się zapisać {self.file_path}: {e}")

    def get_threshold(self, market_name):
        entry = self.data.get(market_name)
        if not entry or len(entry.get('history', [])) < MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD:
            return self.DEFAULT
        return entry.get('threshold', self.DEFAULT)

    def record_outcome(self, market_name, confidence, outcome):
        if outcome not in ('win', 'loss'):
            return
        entry = self.data.setdefault(market_name, {'threshold': self.DEFAULT, 'history': []})
        entry['history'].append([confidence, outcome == 'win'])
        entry['history'] = entry['history'][-self.HISTORY_WINDOW:]
        if len(entry['history']) >= MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD:
            entry['threshold'] = self._recalibrate(entry['history'])
        self.save()

    def _recalibrate(self, history):
        candidates = sorted(set(round(c, 2) for c, _ in history))
        best = self.DEFAULT
        for th in candidates:
            subset = [w for c, w in history if c >= th]
            if len(subset) < 5:
                continue
            win_rate = sum(subset) / len(subset)
            if win_rate >= self.TARGET_WIN_RATE:
                best = th
                break
        return best


class PerformanceStats:
    """Śledzi win-rate i średnie R (zysk/strata w jednostkach ryzyka) - dane
    wejściowe do position sizingu metodą fractional Kelly."""

    def __init__(self, file_path=PERFORMANCE_STATS_FILE):
        self.file_path = file_path
        self.records = self.load()

    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return []

    def save(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.records[-1000:], f, indent=2)
        except Exception as e:
            logger.error(f"Nie udało się zapisać {self.file_path}: {e}")

    def record(self, outcome, r_multiple):
        if outcome not in ('win', 'loss'):
            return
        self.records.append({'outcome': outcome, 'r_multiple': r_multiple})
        self.save()

    def get_stats(self, min_samples=MIN_SAMPLES_FOR_KELLY):
        if len(self.records) < min_samples:
            return None
        wins = [r['r_multiple'] for r in self.records if r['outcome'] == 'win']
        losses = [abs(r['r_multiple']) for r in self.records if r['outcome'] == 'loss']
        if not wins or not losses:
            return None
        win_rate = len(wins) / (len(wins) + len(losses))
        avg_win_r = sum(wins) / len(wins)
        avg_loss_r = sum(losses) / len(losses)
        return win_rate, avg_win_r, avg_loss_r


class CircuitBreaker:
    """Wstrzymuje generowanie NOWYCH sygnałów, gdy skumulowana strata
    (w jednostkach R) w ciągu dnia lub tygodnia przekroczy zdefiniowany limit.
    Otwarte pozycje nadal są monitorowane (evaluate_open_signals) - blokowane
    jest tylko otwieranie nowych."""

    def __init__(self, file_path=CIRCUIT_BREAKER_FILE):
        self.file_path = file_path
        self.state = self.load()

    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return {'trades': []}

    def save(self):
        try:
            cutoff = (datetime.now(pytz.utc) - timedelta(days=90)).isoformat()
            self.state['trades'] = [t for t in self.state['trades'] if t['timestamp'] >= cutoff]
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Nie udało się zapisać {self.file_path}: {e}")

    def record_trade(self, r_multiple, timestamp=None):
        ts = timestamp or datetime.now(pytz.utc).isoformat()
        self.state['trades'].append({'timestamp': ts, 'r_multiple': r_multiple})
        self.save()

    def _sum_r_since(self, since_dt):
        total = 0.0
        for t in self.state['trades']:
            try:
                if datetime.fromisoformat(t['timestamp']) >= since_dt:
                    total += t['r_multiple']
            except Exception:
                continue
        return total

    def is_tripped(self):
        now = datetime.now(pytz.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=now.weekday())
        daily_r = self._sum_r_since(day_start)
        weekly_r = self._sum_r_since(week_start)
        if daily_r <= DAILY_LOSS_LIMIT_R:
            return True, f"Dzienny limit strat osiągnięty ({daily_r:.2f}R <= {DAILY_LOSS_LIMIT_R}R)"
        if weekly_r <= WEEKLY_LOSS_LIMIT_R:
            return True, f"Tygodniowy limit strat osiągnięty ({weekly_r:.2f}R <= {WEEKLY_LOSS_LIMIT_R}R)"
        return False, None


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
        signal['risk_distance'] = abs(signal['entry'] - signal['stop_loss'])
        signal['mae_percent'] = 0.0  # najgorszy dotychczasowy ruch przeciwko pozycji (Maximum Adverse Excursion)
        signal['mfe_percent'] = 0.0  # najlepszy dotychczasowy ruch na korzyść (Maximum Favorable Excursion)
        self.signals[key] = signal
        self.save_signals()
        append_feature_store(key, signal.get('features', {}), signal['name'],
                              signal['direction'], signal['confidence'], signal['timestamp'])
        # Log per tryb pracy (SHADOW -> shadow_trades.jsonl, LIVE -> live_trades.jsonl) -
        # dokładnie w momencie, gdy sygnał oficjalnie wchodzi do śledzenia
        # (SIGNALS_FILE), niezależnie od tego, czy zmieści się w limicie
        # Top 10 wysyłanym na Telegram (patrz main()).
        log_signal_per_mode(signal)

    def evaluate_open_signals(self, tf_weights, feature_weights=None, adaptive_threshold=None,
                               performance_stats=None, circuit_breaker=None,
                               max_age_hours=SIGNAL_TIMEOUT_HOURS):
        """Sprawdza otwarte sygnały: aktualizuje MAE/MFE, a gdy trafią TP/SL/timeout
        - zamyka je i karmi wynikiem WSZYSTKIE komponenty uczące się:
        TimeframeWeights, FeatureWeights, AdaptiveThreshold, PerformanceStats
        i CircuitBreaker (żeby limit strat dziennych/tygodniowych był aktualny)."""
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
            data, _src = get_price_data(sig.get('name'), market_info['symbol'], '15m', '1d')
            if not data or not data['prices']:
                continue
            current_price = data['prices'][-1]
            direction = sig['direction']
            entry = sig['entry']

            # --- MAE/MFE: aktualizowane KAŻDY cykl, nie tylko przy zamknięciu ---
            move_percent = ((current_price - entry) / entry * 100 if direction == 'LONG'
                             else (entry - current_price) / entry * 100)
            sig['mfe_percent'] = max(sig.get('mfe_percent', 0.0), move_percent)
            sig['mae_percent'] = min(sig.get('mae_percent', 0.0), move_percent)
            any_update = True

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

                # R-multiple: ruch faktyczny / dystans do SL, ze znakiem zgodnym z kierunkiem
                if sig.get('risk_distance'):
                    raw_move = (current_price - entry) if direction == 'LONG' else (entry - current_price)
                    sig['r_multiple'] = raw_move / sig['risk_distance']
                else:
                    sig['r_multiple'] = 0.0

                if should_update_weights():
                    tf_weights.update_from_outcome(sig, outcome)

                if outcome in ('win', 'loss') and feature_weights is not None and sig.get('features') \
                        and should_update_weights():
                    went_up = (direction == 'LONG' and outcome == 'win') or \
                              (direction == 'SHORT' and outcome == 'loss')
                    feature_weights.update(sig['features'], went_up)

                if adaptive_threshold is not None and should_update_threshold():
                    adaptive_threshold.record_outcome(sig['name'], sig.get('confidence', 0), outcome)

                if outcome in ('win', 'loss') and performance_stats is not None and should_record_stats():
                    performance_stats.record(outcome, sig['r_multiple'])

                if outcome in ('win', 'loss') and circuit_breaker is not None and should_record_stats():
                    circuit_breaker.record_trade(sig['r_multiple'])

                log_trade_outcome(key, outcome, sig.get('r_multiple', 0.0))

                logger.info(
                    f"Zamknięto sygnał {sig['name']} {direction} - {outcome} "
                    f"(R={sig.get('r_multiple', 0):.2f}, MAE={sig['mae_percent']:.2f}%, MFE={sig['mfe_percent']:.2f}%)"
                )

        if any_update:
            self.save_signals()

# ============================================
# FUNKCJE POMOCNICZE
# ============================================

def append_feature_store(signal_key, features, name, direction, confidence, timestamp):
    """Zapisuje pełny wektor cech każdego wysłanego sygnału do osobnego pliku
    JSONL (punkt C - feature store). Nie jest to potrzebne do bieżącego
    działania bota - służy do OFFLINE retreningu/analizy (np. wytrenowania
    pełnoprawnego modelu ML na historii, zamiast prostej regresji online)."""
    try:
        with open(FEATURE_STORE_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'key': signal_key,
                'name': name,
                'direction': direction,
                'confidence': confidence,
                'features': features,
                'timestamp': timestamp,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Nie udało się zapisać do {FEATURE_STORE_FILE}: {e}")


def compute_shadow_score(ind, combined):
    """Prosty, celowo NIEZALEŻNY model 'cieniowy' oparty tylko o RSI i trend
    wielointerwałowy (bez newsów, formacji świecowych, order flow itd.).
    Nie wpływa na decyzje - loguje się go obok produkcyjnego scoringu, żeby
    porównać oba podejścia w czasie i sprawdzić, czy produkcyjny model faktycznie
    dokłada wartość ponad prostszy baseline (punkt C - shadow scoring)."""
    score = 0
    if ind['rsi'] > 55:
        score += 1
    elif ind['rsi'] < 45:
        score -= 1
    if combined and combined.get('trend_score', 0.5) > 0.6:
        score += 1
    elif combined and combined.get('trend_score', 0.5) < 0.4:
        score -= 1
    return score  # zakres -2..2


def log_shadow_comparison(name, production_confidence, production_direction, shadow_score):
    try:
        with open(SHADOW_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'name': name,
                'timestamp': datetime.now(pytz.utc).isoformat(),
                'production_confidence': production_confidence,
                'production_direction': production_direction,
                'shadow_score': shadow_score,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Nie udało się zapisać do {SHADOW_LOG_FILE}: {e}")


def log_near_miss(name, stage, reason, long_conf=None, short_conf=None, direction=None,
                   confidence=None, threshold=None, entry=None, atr=None, features=None,
                   price_source=None, regime=None):
    """Loguje KAŻDĄ próbę scoringu dla danego rynku w tym cyklu - także te
    odrzucone wcześniej przez filtry (spread/zmienność/płynność/sesja/brak
    danych), nie tylko finalny wynik scoringu. To rozszerza dawny
    shadow-scoring-log (który logował tylko niezależny model-cień dla
    rynków, które PRZESZŁY filtry) o pełny audyt każdej próby - potrzebny do
    odpowiedzi na pytanie "dlaczego nic nie wysłano" bez zgadywania.

    Zapis jest zawsze append-only (audyt). Jeśli sygnał realnie nie osiągnął
    progu (stage='scored', reason='ponizej progu') i mamy entry/atr/features,
    dodatkowo trafia do NearMissTracker - do późniejszej ewaluacji "co by
    było gdyby" (patrz evaluate_near_misses)."""
    record = {
        'timestamp': datetime.now(pytz.utc).isoformat(),
        'name': name,
        'stage': stage,
        'reason': reason,
        'long_conf': long_conf,
        'short_conf': short_conf,
        'direction': direction,
        'confidence': confidence,
        'threshold': threshold,
        'regime': regime,
        'price_source': price_source,
    }
    try:
        with open(NEAR_MISS_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Nie udało się zapisać do {NEAR_MISS_LOG_FILE}: {e}")

    if stage == 'scored' and reason == 'ponizej progu' and entry and atr and features is not None:
        NearMissTracker().add(name, direction, confidence, entry, atr, features)

    if stage == 'scored' and confidence is not None:
        update_daily_scores_cache(name, confidence, direction, threshold)


def update_daily_scores_cache(market_name, conf, direction, threshold=None):
    """Trzyma TYLKO max confidence per rynek na DZIŚ, w małym pliku JSON -
    znacznie taniej niż skanowanie całego near_miss_log.jsonl przy każdym
    dziennym podsumowaniu (patrz send_daily_near_threshold_summary)."""
    today = datetime.now(TIMEZONE).strftime('%Y-%m-%d')
    cache = {'date': today, 'scores': {}}
    if os.path.exists(DAILY_SCORES_CACHE_FILE):
        try:
            with open(DAILY_SCORES_CACHE_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                if d.get('date') == today:
                    cache = d
        except Exception:
            pass
    curr = cache['scores'].get(market_name, {'confidence': 0.0, 'direction': 'NONE'})
    if conf >= curr['confidence']:
        cache['scores'][market_name] = {'confidence': conf, 'direction': direction or 'NONE',
                                         'threshold': threshold}
    try:
        with open(DAILY_SCORES_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Nie udało się zapisać {DAILY_SCORES_CACHE_FILE}: {e}")


class NearMissTracker:
    """Przechowuje sygnały odrzucone przez próg pewności ('near-miss'), żeby
    po NEAR_MISS_MIN_AGE_HOURS sprawdzić, co faktycznie zrobiła cena - i
    dokarmić FeatureWeights tym wynikiem z obniżoną wagą
    (NEAR_MISS_SAMPLE_WEIGHT). Osobny, mutowalny plik (JSON) - w
    odróżnieniu od NEAR_MISS_LOG_FILE, który jest tylko append-only audytem.

    UWAGA - ograniczenie: ewaluacja sprawdza TYLKO cenę w momencie
    uruchomienia (jak evaluate_open_signals), a nie pełną ścieżkę świec
    pomiędzy - przy rzadkim odpalaniu (np. raz dziennie) może to przeoczyć
    krótkotrwałe dotknięcie TP/SL w środku okresu. To akceptowalny kompromis
    dla sygnału treningowego o i tak obniżonej wadze, nie dla realnych
    transakcji."""

    def __init__(self, file_path=NEAR_MISS_PENDING_FILE):
        self.file_path = file_path
        self.data = self.load()

    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Nie udało się wczytać {self.file_path}: {e}")
        return {}

    def save(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Nie udało się zapisać {self.file_path}: {e}")

    def add(self, name, direction, confidence, entry, atr, features):
        key = f"{name}_{datetime.now(pytz.utc).isoformat()}"
        self.data[key] = {
            'name': name,
            'direction': direction,
            'confidence': confidence,
            'entry': entry,
            'atr': atr,
            'features': features,
            'created_at': datetime.now(pytz.utc).isoformat(),
        }
        self.save()

    def evaluate(self, feature_weights):
        """Sprawdza dojrzałe próbki (wiek >= NEAR_MISS_MIN_AGE_HOURS), ustala
        hipotetyczny wynik (TP/SL na bazie NEAR_MISS_ATR_*_MULT) i dokarmia
        FeatureWeights z wagą NEAR_MISS_SAMPLE_WEIGHT. Próbki starsze niż
        NEAR_MISS_MAX_AGE_HOURS bez rozstrzygnięcia są porzucane (timeout,
        bez wpływu na naukę - zbyt niejednoznaczne)."""
        now = datetime.now(pytz.utc)
        resolved_keys = []
        n_evaluated = 0
        for key, item in list(self.data.items()):
            try:
                created_at = datetime.fromisoformat(item['created_at'])
            except Exception:
                resolved_keys.append(key)
                continue
            age_hours = (now - created_at).total_seconds() / 3600
            if age_hours < NEAR_MISS_MIN_AGE_HOURS:
                continue

            market_info = MARKETS.get(item['name'])
            if not market_info:
                resolved_keys.append(key)
                continue
            data, _src = get_price_data(item['name'], market_info['symbol'], '15m', '1d')
            if not data or not data['prices']:
                if age_hours >= NEAR_MISS_MAX_AGE_HOURS:
                    resolved_keys.append(key)
                continue
            current_price = data['prices'][-1]

            direction = item['direction']
            entry = item['entry']
            atr = item['atr']
            stop_loss = entry - NEAR_MISS_ATR_SL_MULT * atr if direction == 'LONG' else entry + NEAR_MISS_ATR_SL_MULT * atr
            take_profit = entry + NEAR_MISS_ATR_TP_MULT * atr if direction == 'LONG' else entry - NEAR_MISS_ATR_TP_MULT * atr

            hit_tp = (direction == 'LONG' and current_price >= take_profit) or \
                     (direction == 'SHORT' and current_price <= take_profit)
            hit_sl = (direction == 'LONG' and current_price <= stop_loss) or \
                     (direction == 'SHORT' and current_price >= stop_loss)

            outcome = None
            if hit_tp:
                outcome = 'win'
            elif hit_sl:
                outcome = 'loss'
            elif age_hours >= NEAR_MISS_MAX_AGE_HOURS:
                outcome = 'timeout'

            if outcome in ('win', 'loss'):
                went_up = (direction == 'LONG' and outcome == 'win') or \
                          (direction == 'SHORT' and outcome == 'loss')
                feature_weights.update(item['features'], went_up, weight=NEAR_MISS_SAMPLE_WEIGHT)
                n_evaluated += 1
                try:
                    with open(NEAR_MISS_LOG_FILE, 'a', encoding='utf-8') as f:
                        f.write(json.dumps({
                            'timestamp': now.isoformat(), 'name': item['name'], 'stage': 'evaluated',
                            'reason': outcome, 'direction': direction, 'confidence': item['confidence'],
                        }, ensure_ascii=False) + "\n")
                except Exception as e:
                    logger.error(f"Nie udało się zapisać ewaluacji near-miss: {e}")

            if outcome is not None:
                resolved_keys.append(key)

        for key in resolved_keys:
            self.data.pop(key, None)
        if resolved_keys or n_evaluated:
            self.save()
        if n_evaluated:
            logger.info(f"Near-miss: doewaluowano {n_evaluated} próbek do FeatureWeights (waga {NEAR_MISS_SAMPLE_WEIGHT}).")


def evaluate_near_misses(feature_weights):
    NearMissTracker().evaluate(feature_weights)


def build_daily_near_miss_summary():
    """Buduje ranking top-3 rynków z najwyższym confidence dzisiaj, z lekkiego
    cache'u DAILY_SCORES_CACHE_FILE (aktualizowanego na bieżąco w log_near_miss/
    update_daily_scores_cache) zamiast skanowania całego near_miss_log.jsonl
    przy każdym podsumowaniu."""
    today = datetime.now(TIMEZONE).strftime('%Y-%m-%d')
    if not os.path.exists(DAILY_SCORES_CACHE_FILE):
        return []
    try:
        with open(DAILY_SCORES_CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    except Exception as e:
        logger.warning(f"Nie udało się wczytać {DAILY_SCORES_CACHE_FILE}: {e}")
        return []
    if cache.get('date') != today:
        return []
    ranked = sorted(
        ({'name': name, **data} for name, data in cache.get('scores', {}).items()),
        key=lambda r: r['confidence'], reverse=True
    )
    return ranked[:3]


def rotate_jsonl_log(file_path, days_to_keep=30):
    """Punkt 4: near_miss_log.jsonl i data_fetch_errors.jsonl rosną bez końca
    i są commitowane do repo co 10 minut - po kilku miesiącach mogłoby to
    realnie spowolnić `git add -A && git commit` w workflow. Trzyma pełne
    wpisy z ostatnich `days_to_keep` dni, a starsze zwija do JEDNEJ linii
    dziennego podsumowania (liczba wpisów + rozbicie wg reason/source/stage)
    w <file_path bez '.jsonl'>_summary.jsonl - podobnie jak PerformanceStats/
    backtest_results.json już trzymają tylko ostatnie N wpisów zamiast
    wszystkiego. Wywoływane raz dziennie (patrz is_evening_summary_time),
    nie przy każdym cyklu."""
    if not os.path.exists(file_path):
        return
    cutoff = datetime.now(pytz.utc) - timedelta(days=days_to_keep)
    kept = []
    old_by_day = {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get('timestamp')
                try:
                    dt = datetime.fromisoformat(ts)
                except (TypeError, ValueError):
                    kept.append(rec)  # brak/zły timestamp - nie wywalaj po cichu, zostaw
                    continue
                if dt >= cutoff:
                    kept.append(rec)
                else:
                    day = dt.strftime('%Y-%m-%d')
                    old_by_day.setdefault(day, []).append(rec)
    except Exception as e:
        logger.error(f"Nie udało się przeczytać {file_path} do rotacji: {e}")
        return

    if not old_by_day:
        return  # nic starszego niż days_to_keep - nie ma czego zwijać

    summary_path = file_path[:-len('.jsonl')] + '_summary.jsonl' if file_path.endswith('.jsonl') \
        else file_path + '.summary.jsonl'
    try:
        with open(summary_path, 'a', encoding='utf-8') as f:
            for day, recs in sorted(old_by_day.items()):
                breakdown = {}
                for r in recs:
                    key = r.get('reason') or r.get('source') or r.get('stage') or '?'
                    breakdown[key] = breakdown.get(key, 0) + 1
                f.write(json.dumps({'date': day, 'total': len(recs), 'breakdown': breakdown},
                                    ensure_ascii=False) + '\n')
    except Exception as e:
        logger.error(f"Nie udało się zapisać podsumowania rotacji do {summary_path}: {e}")
        return  # nie ucinaj oryginału, jeśli podsumowanie się nie zapisało - wolimy duplikat niż utratę danych

    n_collapsed = sum(len(v) for v in old_by_day.values())
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            for rec in kept:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.error(f"Nie udało się nadpisać {file_path} po rotacji: {e}")
        return

    logger.info(f"Rotacja {file_path}: zachowano {len(kept)} wpisów z ostatnich {days_to_keep} dni, "
                f"zwinięto {n_collapsed} starszych do {summary_path}.")


def send_daily_near_miss_report():
    if not should_send_notifications():
        return
    top3 = build_daily_near_miss_summary()
    if not top3:
        return
    msg = "📍 *NAJBLIŻEJ PROGU DZIŚ*\n\n_Żaden z poniższych nie osiągnął progu wysyłki, ale były najbliżej:_\n\n"
    for i, r in enumerate(top3, 1):
        th = r.get('threshold')
        th_txt = f"{th:.0%}" if th is not None else f"{CONFIDENCE_THRESHOLD:.0%}"
        msg += f"{i}. {r['name']} ({r.get('direction', '?')}) - {r['confidence']:.0%} (próg: {th_txt})\n"
    send_telegram(get_notification_message(msg), add_disclaimer=False)


def is_weekend():
    return datetime.now(TIMEZONE).weekday() >= 5


def is_friday_evening():
    now = datetime.now(TIMEZONE)
    return now.weekday() == 4 and now.hour == 19 and now.minute < 10


def is_monthly_report_time():
    now = datetime.now(TIMEZONE)
    return now.day == 1 and now.hour == 8 and now.minute < 10


def is_time_for(target_h, target_m, window_m=14):
    """Okno (target_h:target_m .. +window_m minut) - szersze niż jeden cykl
    10-minutowy, żeby nie przegapić wyzwalacza jeśli poprzedni cykl agenta
    się spóźnił/padł."""
    now = datetime.now(TIMEZONE)
    return now.hour == target_h and target_m <= now.minute < (target_m + window_m)


def is_morning_sentiment_time():
    return is_time_for(8, 10, window_m=10)


def is_evening_summary_time():
    """Pora na dzienne podsumowanie 'najbliżej progu' - patrz config.DAILY_SUMMARY_HOUR."""
    return is_time_for(*DAILY_SUMMARY_HOUR, window_m=10)


def is_macro_fetch_time(state):
    """Sprawdza, czy jesteśmy w oknie jednego z config.MACRO_HOURS (rano /
    przed otwarciem US / po zamknięciu głównych sesji) i czy dla TEGO okna
    dziś jeszcze nie pobieraliśmy danych makro (żeby przy cyklu co 10 minut
    nie odpalać pobrania makro kilkukrotnie w tym samym oknie)."""
    now = datetime.now(TIMEZONE)
    today_key = now.strftime('%Y-%m-%d')
    for window_name, (h, m) in MACRO_HOURS.items():
        if is_time_for(h, m, window_m=10):
            last_run = state.get('last_run', {}).get(window_name)
            return last_run != today_key
    return False


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


def fetch_investing_data(name, interval='15m', range_period='1d'):
    """Próba pobrania świec z Investing.com - GŁÓWNE źródło cen, zgodnie
    z ustaleniem. UWAGA - WAŻNE OGRANICZENIE, powtórzone świadomie: Investing.com
    NIE MA oficjalnego, publicznego API do świec OHLCV. Poniższy klient korzysta
    z niezudokumentowanego endpointu używanego przez stronę WWW i może w każdej
    chwili przestać działać bez ostrzeżenia (zmiana struktury odpowiedzi,
    Cloudflare/anti-bot, wymóg nagłówków sesji z przeglądarki itp.) - nie było
    możliwości przetestowania go na żywym połączeniu w tym środowisku (brak
    dostępu do sieci w sandboxie). To jest świadomie zaakceptowane ryzyko: jeśli
    pobranie się nie powiedzie, kod loguje błąd do DATA_FETCH_ERRORS_FILE i
    automatycznie spada na Yahoo Finance - patrz get_price_data(). Warto co
    jakiś czas zerknąć do DATA_FETCH_ERRORS_FILE i sprawdzić, jak często Investing
    faktycznie odpowiada, a jak często agent i tak jedzie na samym Yahoo.

    Endpoint (`/api/financialdata/{pairId}/historical/chart`) i parametry
    (period/interval/pointscount) potwierdzone przez użytkownika na bazie
    kodu źródłowego biblioteki investing-com-api. DOKŁADNY FORMAT wartości
    'interval' (czy to np. 'PT5M' w stylu ISO-8601, czy zwykła liczba minut)
    i dokładny KSZTAŁT odpowiedzi JSON (nazwy pól świec) NIE są potwierdzone -
    nadal nie było możliwości przetestowania na żywym połączeniu. Parsowanie
    poniżej próbuje kilku najbardziej prawdopodobnych wariantów; jeśli żaden
    nie pasuje, błąd trafia do DATA_FETCH_ERRORS_FILE z fragmentem realnej
    odpowiedzi, żeby dało się to poprawić na podstawie faktycznych danych,
    zamiast dalej zgadywać.

    ALTERNATYWA WARTA ROZWAŻENIA: biblioteka `investpy` (PyPI) opakowuje te
    same niezudokumentowane endpointy z gotową obsługą błędów/nagłówków -
    mniej kodu do utrzymania tutaj, ale ma własną, udokumentowaną w jej
    issues historię przestojów po zmianach zabezpieczeń Investing.com, więc
    to nie jest gwarancja stabilności, tylko inny kompromis.

    `pair_id` dla każdego rynku trzeba ustalić ręcznie (patrz
    discover_investing_pair_ids.py) albo w devtools przeglądarki. Wartości
    None = rynek pominie Investing.com i pójdzie od razu na Yahoo, dopóki nie
    uzupełnisz ID w INVESTING_PAIR_IDS poniżej."""
    pair_id = INVESTING_PAIR_IDS.get(name)
    if not pair_id:
        log_data_error('investing', name, None, 'brak zmapowanego pair_id (patrz INVESTING_PAIR_IDS)')
        return None
    resolution = INVESTING_INTERVAL_MAP.get(interval)
    if resolution is None:
        log_data_error('investing', name, pair_id, f'nieznany interwał {interval}')
        return None

    points_count = {'1d': 100, '5d': 300, '1mo': 200, '3mo': 200}.get(range_period, 150)

    url = f"https://api.investing.com/api/financialdata/{pair_id}/historical/chart"
    params = {
        'period': range_period,
        'interval': resolution,       # niepotwierdzony dokładny format - patrz docstring
        'pointscount': points_count,
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Domain-Id': 'www',
    }
    resp = http_get_with_retry(url, params=params, headers=headers, timeout=10, retries=2)
    if resp is None:
        log_data_error('investing', name, pair_id, 'brak odpowiedzi HTTP (sieć/anti-bot/Cloudflare?)')
        return None
    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        log_data_error('investing', name, pair_id, f'odpowiedź nie jest poprawnym JSON: {e} '
                                                     f'(pierwsze 200 znaków: {resp.text[:200]!r})')
        return None

    rows = _extract_investing_candles(payload)
    if rows is None:
        log_data_error('investing', name, pair_id,
                        f'nierozpoznany kształt odpowiedzi JSON (klucze: {list(payload)[:10] if isinstance(payload, dict) else type(payload)})')
        return None

    opens, highs, lows, closes, volumes = [], [], [], [], []
    for row in rows:
        o, h, l, c, v = row
        if None in (o, h, l, c):
            continue
        opens.append(o); highs.append(h); lows.append(l); closes.append(c); volumes.append(v or 0)

    if len(closes) < 10:
        log_data_error('investing', name, pair_id, f'za mało świec w odpowiedzi ({len(closes)})')
        return None
    return {'prices': closes, 'highs': highs, 'lows': lows, 'volumes': volumes, 'opens': opens}


def _extract_investing_candles(payload):
    """Próbuje wydobyć świece z kilku najbardziej prawdopodobnych kształtów
    odpowiedzi tego niezudokumentowanego endpointu (dokładny format nie jest
    potwierdzony - patrz fetch_investing_data). Zwraca listę krotek
    (open, high, low, close, volume) albo None, jeśli żaden znany kształt
    nie pasuje - NIE zgaduje na siłę, żeby nie zwrócić po cichu śmieciowych
    danych."""
    if isinstance(payload, dict):
        candidates = payload.get('data') or payload.get('candles') or payload.get('chart')
    elif isinstance(payload, list):
        candidates = payload
    else:
        candidates = None
    if not candidates:
        return None

    rows = []
    for item in candidates:
        if isinstance(item, dict):
            o = item.get('open_value', item.get('open'))
            h = item.get('high_value', item.get('high'))
            l = item.get('low_value', item.get('low'))
            c = item.get('close_value', item.get('close'))
            v = item.get('volume', 0)
            rows.append((o, h, l, c, v))
        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            # popularny format świec z wykresów: [timestamp, open, high, low, close, volume]
            rows.append((item[1], item[2], item[3], item[4], item[5] if len(item) > 5 else 0))
        else:
            return None
    return rows if rows else None


# --- Circuit breaker PER ŹRÓDŁO DANYCH (punkt 2) - w jednym cyklu agenta
# (16 rynków x do 5 interwałów = do ~80 zapytań) po kilku kolejnych błędach
# Investing.com nie ma sensu dalej w niego dobijać - w praktyce to prawie
# zawsze oznacza, że w TYM cyklu ono po prostu nie działa (blokada
# Cloudflare/anti-bot na cały zakres IP, nie problem z jednym zapytaniem).
# Dalsze próby tylko zaśmiecają DATA_FETCH_ERRORS_FILE i ryzykują twardszą
# blokadę. Stan jest module-level, więc naturalnie resetuje się przy każdym
# nowym uruchomieniu `python agent.py` (GitHub Actions odpala nowy proces co
# cykl) - reset_investing_circuit() istnieje głównie dla jasności/testów. ---
INVESTING_CIRCUIT_THRESHOLD = int(os.environ.get('INVESTING_CIRCUIT_THRESHOLD', 5))
_investing_cycle_failures = 0
_investing_cycle_disabled = False


def reset_investing_circuit():
    global _investing_cycle_failures, _investing_cycle_disabled
    _investing_cycle_failures = 0
    _investing_cycle_disabled = False


def get_price_data(name, symbol, interval='15m', range_period='1d'):
    """Punkt wejścia do pobierania świec: Investing.com jako GŁÓWNE źródło,
    z automatycznym fallbackiem na Yahoo Finance gdy Investing zawiedzie
    (błąd zawsze najpierw logowany do DATA_FETCH_ERRORS_FILE - patrz
    fetch_investing_data/fetch_yahoo_data/log_data_error). Zwraca (data, source)
    gdzie source in {'investing', 'yahoo', None}.

    Jeśli Investing zawiedzie INVESTING_CIRCUIT_THRESHOLD razy z rzędu w tym
    cyklu, dalsze wywołania w tym samym cyklu pomijają Investing całkowicie
    i idą prosto na Yahoo - patrz komentarz przy _investing_cycle_disabled."""
    global _investing_cycle_failures, _investing_cycle_disabled
    if not _investing_cycle_disabled:
        data = fetch_investing_data(name, interval, range_period)
        if data:
            _investing_cycle_failures = 0
            return data, 'investing'
        _investing_cycle_failures += 1
        if _investing_cycle_failures >= INVESTING_CIRCUIT_THRESHOLD:
            _investing_cycle_disabled = True
            logger.warning(
                f"Investing.com: {_investing_cycle_failures} błędów z rzędu w tym cyklu - "
                f"wyłączam Investing do końca cyklu, reszta rynków/interwałów idzie od razu na Yahoo."
            )

    data = fetch_yahoo_data(symbol, interval, range_period)
    if data:
        return data, 'yahoo'
    reason = 'Yahoo zawiodło (Investing wyłączone w tym cyklu po serii błędów)' if _investing_cycle_disabled \
        else 'Investing i Yahoo zawiodły w tym cyklu'
    log_data_error('all_sources', name, symbol, reason)
    return None, None


def fetch_yahoo_data(symbol, interval='15m', range_period='1d'):
    """
    Fallback: Yahoo Finance. Używane, gdy Investing.com nie zwróci danych
    (patrz get_price_data). NAPRAWIONE: wcześniej close/high/low/volume/open
    filtrowane były osobno, więc przy różnych pozycjach None w poszczególnych
    polach indeksy przestawały się zgadzać między tablicami (świeca closes[i]
    mogła nie odpowiadać highs[i]). Teraz wiersze są wyrównywane RAZEM i
    odrzucane tylko wtedy, gdy którekolwiek pole w danym wierszu jest None.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {'interval': interval, 'range': range_period}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    resp = http_get_with_retry(url, params=params, headers=headers, timeout=10)
    if resp is None:
        log_data_error('yahoo', symbol, symbol, 'brak odpowiedzi HTTP')
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
            log_data_error('yahoo', symbol, symbol, f'za mało wyrównanych świec ({len(aligned)})')
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
        log_data_error('yahoo', symbol, symbol, f'błąd parsowania: {e}')
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


def analyze_timeframes(name, symbol, timeframe_weights):
    """NAPRAWIONE: '4h' jest teraz agregowany z tych samych danych 60m
    (resample_ohlcv), a nie pobierany osobno jako duplikat '1h' pod inną nazwą."""
    timeframe_results = {}
    cache_60m = {}
    for tf_name, tf_config in TIMEFRAMES.items():
        if tf_config.get('resample_from_60m'):
            range_key = tf_config['range']
            if range_key not in cache_60m:
                raw, _src = get_price_data(name, symbol, '60m', range_key)
                cache_60m[range_key] = raw
            raw = cache_60m[range_key]
            data = resample_ohlcv(raw, tf_config['resample_from_60m']) if raw else None
        else:
            data, _src = get_price_data(name, symbol, tf_config['interval'], tf_config['range'])

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
# DANE MAKRO (Trading Economics + analiza AI)
# ============================================
# Projekt świadomie NIE deleguje pobierania danych makro do samego agenta AI
# (tj. "zapytaj Groq co się dzieje w gospodarce") - model językowy bez
# realnych danych wejściowych może to zmyślić (halucynacja dat/wartości
# wskaźników), a to jest dokładnie ten rodzaj informacji, gdzie fałszywy
# "fakt" jest gorszy niż jego brak. Zamiast tego: RZECZYWISTE dane pobierane
# są z Trading Economics, a AI służy tylko do ICH interpretacji (wybór
# top 5, ocena wpływu na konkretne rynki) - nie do wymyślania danych.

def fetch_macro_data():
    """Pobiera dane makro z DWÓCH źródeł RSS (patrz data_sources.py):
    Trading Economics (newsy + kalendarz) i Investing.com (newsy). RSS nie
    wymaga klucza API i nie jest ograniczone do krajów demo, w przeciwieństwie
    do darmowego dostępu do JSON API Trading Economics - kosztem mniej
    ustrukturyzowanych danych (tytuł + opis zamiast osobnych pól
    actual/forecast/previous). Każdy błąd jest logowany do
    DATA_FETCH_ERRORS_FILE (patrz data_sources.log_fetch_error - wspólny plik
    z log_data_error() w tym module)."""
    te_items = fetch_trading_economics_macro()
    investing_items = [{'title': t, 'description': '', 'pub_date': '', 'source': 'Investing_News'}
                        for t in fetch_investing_news()]
    return te_items + investing_items


def store_macro_events(events):
    """Zapisuje WSZYSTKIE pobrane wydarzenia/newsy makro (nie tylko top 5
    wysyłane na Telegram) do MACRO_DATA_FILE - to jest materiał do nauki/
    analizy, zgodnie z założeniem 'do nauki użyj wszystkich ważnych danych
    makro'."""
    try:
        ts = datetime.now(pytz.utc).isoformat()
        with open(MACRO_DATA_FILE, 'a', encoding='utf-8') as f:
            for ev in events:
                f.write(json.dumps({'fetched_at': ts, 'event': ev}, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Nie udało się zapisać {MACRO_DATA_FILE}: {e}")


def analyze_macro_with_ai(events):
    """Wysyła RZECZYWISTE newsy/odczyty makro (z RSS) do AI wyłącznie w celu
    interpretacji (wybór top 5 + ocena wpływu na poszczególne rynki z
    MARKETS) - AI nie dostaje zadania 'wymyśl dane makro', tylko konkretną
    listę tytułów/opisów do oceny."""
    if not events:
        return None
    lines = []
    for e in events[:60]:
        title = e.get('title', '')
        desc = (e.get('description') or '')[:160]
        source = e.get('source', '?')
        if not title:
            continue
        lines.append(f"- [{source}] {title}" + (f" — {desc}" if desc else ""))
    if not lines:
        return None
    events_text = "\n".join(lines)
    markets_list = ", ".join(MARKETS.keys())
    prompt = f"""Przeanalizuj poniższe RZECZYWISTE newsy/odczyty makroekonomiczne (z RSS Trading Economics i Investing.com) pod kątem wpływu na rynki finansowe. Nie wymyślaj dodatkowych danych - bazuj wyłącznie na podanych.

Newsy/odczyty:
{events_text}

Rynki do oceny (uwzględnij WSZYSTKIE, nawet z impact="low"/sentiment=0 jeśli brak istotnego wpływu): {markets_list}

Odpowiedz WYŁĄCZNIE w JSON, bez żadnego dodatkowego tekstu:
{{
  "top5": [{{"event": <string>, "country": <string, "?" jeśli nieznany>, "reasoning": <string po polsku>}}],
  "market_impact": {{
     "<dokładna nazwa rynku z listy>": {{"sentiment": <-1 do 1>, "impact": "low"/"medium"/"high"}}
  }},
  "summary": <2-3 zdania po polsku>
}}"""
    result = call_groq("Analityk makroekonomiczny rynków finansowych. Odpowiadaj tylko JSON, bazuj wyłącznie na podanych danych.",
                        prompt, max_tokens=1200)
    if not result:
        return None
    json_match = re.search(r'\{.*\}', result, re.DOTALL)
    if not json_match:
        return None
    try:
        return json.loads(json_match.group())
    except json.JSONDecodeError as e:
        logger.warning(f"Nie udało się sparsować JSON analizy makro: {e}")
        return None


def load_macro_state():
    try:
        if os.path.exists(MACRO_STATE_FILE):
            with open(MACRO_STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Nie udało się wczytać {MACRO_STATE_FILE}: {e}")
    return {'last_run': {}, 'market_impact': {}, 'analyzed_at': None}


def save_macro_state(state):
    try:
        with open(MACRO_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Nie udało się zapisać {MACRO_STATE_FILE}: {e}")


def get_cached_macro_analysis(name, max_age_hours=12):
    """Zwraca ostatnią zapisaną (nie odpytywaną per-rynek per-cykl) analizę
    makro dla danego rynku, o ile nie jest starsza niż max_age_hours -
    starsza analiza jest traktowana jak brak (impact='low' domyślnie w
    analyze_market), żeby nieaktualny sentyment makro nie wpływał cicho na
    scoring przez cały dzień."""
    state = load_macro_state()
    analyzed_at = state.get('analyzed_at')
    if not analyzed_at:
        return None
    try:
        age_hours = (datetime.now(pytz.utc) - datetime.fromisoformat(analyzed_at)).total_seconds() / 3600
    except Exception:
        return None
    if age_hours > max_age_hours:
        return None
    return state.get('market_impact', {}).get(name)


def fetch_and_analyze_macro():
    """Orkiestracja: pobierz REALNE dane makro/newsy (RSS: Trading Economics +
    Investing.com) -> zapisz WSZYSTKIE do MACRO_DATA_FILE (nauka/audyt) ->
    wyślij do AI do interpretacji -> zapisz wynik do cache (MACRO_STATE_FILE,
    używany przez analyze_market przez get_cached_macro_analysis) -> DOPIERO
    PO odebraniu odpowiedzi AI wyślij skrót top 5 na Telegram (jeśli tryb na
    to pozwala - patrz config.py/modes.py)."""
    logger.info("Pobieram dane makro (RSS: Trading Economics + Investing.com)...")
    events = fetch_macro_data()
    if not events:
        logger.warning("Brak danych makro w tym cyklu - poprzednia analiza w cache pozostaje bez zmian.")
        return
    store_macro_events(events)

    analysis = analyze_macro_with_ai(events)
    if not analysis:
        logger.warning("AI nie zwróciło analizy makro w tym cyklu - dane surowe i tak zostały zapisane do nauki.")
        return

    state = load_macro_state()
    now_local = datetime.now(TIMEZONE)
    today_key = now_local.strftime('%Y-%m-%d')
    for window_name, (h, m) in MACRO_HOURS.items():
        if is_time_for(h, m, window_m=10):
            state.setdefault('last_run', {})[window_name] = today_key
            break
    state['analyzed_at'] = datetime.now(pytz.utc).isoformat()
    state['market_impact'] = analysis.get('market_impact', {})
    save_macro_state(state)

    top5 = analysis.get('top5', [])
    if top5 and should_send_notifications():
        msg = "🌍 *MAKRO - TOP 5 CZYNNIKÓW*\n\n"
        for i, item in enumerate(top5[:5], 1):
            msg += f"{i}. [{item.get('country', '?')}] {item.get('event', '?')}\n   {item.get('reasoning', '')}\n\n"
        if analysis.get('summary'):
            msg += f"_{analysis['summary']}_"
        send_telegram(get_notification_message(msg), add_disclaimer=False)

# ============================================
# ZARZĄDZANIE RYZYKIEM
# ============================================

def calculate_position_size(entry, stop_loss, account_size=ACCOUNT_SIZE, risk_percent=RISK_PER_TRADE_PERCENT):
    """Stała wielkość pozycji (ryzyko = risk_percent% kapitału) - używana jako
    bezpieczny fallback, dopóki nie ma wystarczających danych historycznych
    dla fractional Kelly (patrz calculate_position_size_kelly)."""
    risk_amount = account_size * (risk_percent / 100)
    stop_distance = abs(entry - stop_loss)
    if stop_distance == 0:
        return 0, risk_amount
    return risk_amount / stop_distance, risk_amount


def calculate_position_size_kelly(confidence, performance_stats, entry, stop_loss,
                                   account_size=ACCOUNT_SIZE,
                                   max_risk_percent=MAX_RISK_PERCENT_CAP,
                                   kelly_fraction=KELLY_FRACTION):
    """
    Position sizing metodą FRACTIONAL KELLY, dodatkowo skalowany pewnością
    sygnału (punkt B). Kelly f* = W - (1-W)/R, gdzie W = win-rate, R = średni
    zysk/średnia strata w jednostkach R - liczone z faktycznej historii
    zamkniętych sygnałów (PerformanceStats), NIE z założeń.

    Dopóki nie ma wystarczających danych historycznych (patrz
    MIN_SAMPLES_FOR_KELLY), spada na stały % ryzyka (calculate_position_size)
    - bo Kelly liczony na garści przykładów jest bardziej szkodliwy niż
    pomocny.

    kelly_fraction < 1 (domyślnie 0.5, czyli "pół-Kelly") to standardowe
    zabezpieczenie - pełny Kelly jest teoretycznie optymalny, ale w praktyce
    bardzo wrażliwy na błędy oszacowania W i R i generuje duże obsunięcia.
    """
    stop_distance = abs(entry - stop_loss)
    if stop_distance == 0:
        return 0, 0, 'brak_danych'

    stats = performance_stats.get_stats() if performance_stats else None
    if stats is None:
        size, risk_amount = calculate_position_size(entry, stop_loss, account_size)
        return size, risk_amount, 'stały_procent_ryzyka (za mało historii na Kelly)'

    win_rate, avg_win_r, avg_loss_r = stats
    if avg_loss_r <= 0:
        size, risk_amount = calculate_position_size(entry, stop_loss, account_size)
        return size, risk_amount, 'stały_procent_ryzyka (nieprawidłowe dane historyczne)'

    b = avg_win_r / avg_loss_r
    kelly_f = win_rate - (1 - win_rate) / b if b > 0 else 0
    kelly_f = max(0.0, kelly_f) * kelly_fraction

    # dodatkowe skalowanie pewnością sygnału (0.7 confidence -> mniejsza pozycja niż 0.95)
    scaled_risk_percent = min(max_risk_percent, kelly_f * 100) * confidence
    if scaled_risk_percent <= 0:
        # Kelly sugeruje brak krawędzi - i tak wchodzimy minimalną, ostrożną wielkością
        scaled_risk_percent = RISK_PER_TRADE_PERCENT * 0.25

    risk_amount = account_size * (scaled_risk_percent / 100)
    return risk_amount / stop_distance, risk_amount, 'fractional_kelly'

# ============================================
# GŁÓWNA ANALIZA RYNKU
# ============================================

def analyze_market(name, market_info, ai_memory, timeframe_weights, xtb_spreads=None,
                    feature_weights=None, adaptive_threshold=None, performance_stats=None):
    session = market_info.get('session', '24_7')
    if not is_session_active(session):
        return None

    main_data, price_source = get_price_data(name, market_info['symbol'], '15m', '1d')
    if not main_data:
        log_near_miss(name, stage='no_data', reason='brak danych cenowych z żadnego źródła')
        return None

    ind = calculate_full_indicators(main_data)
    if not ind:
        log_near_miss(name, stage='bad_indicators', reason='za mało danych do policzenia wskaźników')
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
        log_near_miss(name, stage='filtered_spread', reason=f'spread {spread_value} ({spread_source})')
        return None
    if not check_extreme_volatility(atr_percent, market_type):
        logger.info(f"❌ {name}: ekstremalna zmienność (ATR: {atr_percent:.2f}%)")
        log_near_miss(name, stage='filtered_volatility', reason=f'ATR% {atr_percent:.2f}')
        return None
    if not check_liquidity(avg_volume, market_type):
        logger.info(f"❌ {name}: za mała płynność (wolumen: {avg_volume:.0f})")
        log_near_miss(name, stage='filtered_liquidity', reason=f'wolumen {avg_volume:.0f}')
        return None

    timeframe_results = analyze_timeframes(name, market_info['symbol'], timeframe_weights)
    if not timeframe_results:
        log_near_miss(name, stage='no_timeframe_data', reason='brak danych MTF')
        return None

    combined = combine_timeframe_analysis(timeframe_results)
    if not combined:
        log_near_miss(name, stage='no_combined', reason='combine_timeframe_analysis zwróciło None')
        return None

    divergences = detect_divergences(timeframe_results)

    # --- Reżim rynku (punkt B): TREND vs RANGE, na bazie ADX z danych 15m ---
    regime, adx_value = detect_market_regime(main_data['highs'], main_data['lows'], main_data['prices'])

    articles = fetch_market_news(name)
    news_analysis = analyze_news_with_ai(articles, name, ai_memory.get_context())

    # --- Dane makro (punkt: priorytety danych + makro w analizie/uczeniu).
    # Pobierane osobno, max 3x dziennie (patrz fetch_and_analyze_macro / main()),
    # tutaj tylko odczytujemy ostatnią ZAPISANĄ analizę AI z MACRO_STATE_FILE -
    # żeby nie odpytywać Trading Economics/Groq per rynek per cykl (10 min). ---
    macro_analysis = get_cached_macro_analysis(name)

    # --- Cena "na żywo": jeśli mamy bid/ask z XTB, użyj mid-price zamiast
    # ostatniego (delikatnie opóźnionego) zamknięcia z Yahoo do entry/SL/TP.
    # UWAGA: to wciąż jest odpytywanie request/response raz na cykl, nie
    # prawdziwy streaming - patrz zastrzeżenie w opisie funkcji fetch_xtb_spreads. ---
    p = ind['price']
    if xtb_info and xtb_info.get('bid') is not None and xtb_info.get('ask') is not None:
        p = (xtb_info['bid'] + xtb_info['ask']) / 2

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

    # --- Wsparcia/opory. W reżimie RANGE wzmacniamy tę wagę (mean-reversion
    # sprawdza się lepiej niż podążanie za trendem, gdy rynek się konsoliduje). ---
    sr_weight = 1.0 if regime == 'RANGE' else 0.5
    if ind['nearest_level'] == 'SUPPORT':
        long_score += sr_weight
    elif ind['nearest_level'] == 'RESISTANCE':
        short_score += sr_weight

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

    # --- MTF (multi-timeframe trend). W reżimie RANGE ten bonus jest mniej
    # wiarygodny (trend na wyższych interwałach częściej się załamuje w
    # konsolidacji) - zmniejszamy go zamiast ufać mu tak samo jak w TREND.
    # Bonus obniżony z 2.0/1.0 do 1.5/0.75, a próg aktywacji złagodzony
    # z 0.6/0.4 do MTF_BULLISH_THRESHOLD/MTF_BEARISH_THRESHOLD (0.55/0.45,
    # patrz config.py) - żeby bonus włączał się częściej (przy solidnej, ale
    # nie idealnej zgodności interwałów), zamiast być rzadko trafianym
    # "wszystko albo nic". Dzięki temu CONFIDENCE_THRESHOLD (0.6-0.7) jest
    # realnie osiągalny przy silnym trendzie bez potrzeby formacji świecowej
    # ani newsów o wysokim wpływie. ---
    mtf_bonus = 1.5 if regime != 'RANGE' else 0.75
    if combined['trend_score'] > MTF_BULLISH_THRESHOLD:
        long_score += mtf_bonus
    elif combined['trend_score'] < MTF_BEARISH_THRESHOLD:
        short_score += mtf_bonus

    # --- Kara za dywergencje (obniża obie strony - sygnał mniej pewny) ---
    if divergences:
        long_score = max(0, long_score - 0.5)
        short_score = max(0, short_score - 0.5)

    # --- Newsy (tylko przy wysokim impakcie) ---
    sentiment = news_analysis.get('sentiment', 0) if news_analysis else 0
    if news_analysis and news_analysis.get('impact') == 'high':
        long_score += max(0, sentiment) * 2
        short_score += max(0, -sentiment) * 2

    # --- Makro (tylko przy wysokim/średnim wpływie - patrz fetch_and_analyze_macro).
    # Waga celowo mniejsza niż newsy specyficzne dla rynku (max 1.0 zamiast 2.0),
    # bo to sentyment GLOBALNY/sektorowy z analizy 3x/dzień, nie świeża
    # informacja dla tego konkretnego instrumentu. ---
    macro_sentiment = 0.0
    if macro_analysis and macro_analysis.get('impact') in ('high', 'medium'):
        macro_sentiment = macro_analysis.get('sentiment', 0.0)
        macro_weight = 1.0 if macro_analysis.get('impact') == 'high' else 0.5
        long_score += max(0, macro_sentiment) * macro_weight
        short_score += max(0, -macro_sentiment) * macro_weight

    # Confidence bazowe, jawnie ograniczone do [0, 1]
    long_conf = min(1.0, max(0.0, long_score / TOTAL_SCORE_POINTS))
    short_conf = min(1.0, max(0.0, short_score / TOTAL_SCORE_POINTS))

    # --- Punkt A: model uczący się (regresja logistyczna na cechach).
    # Dopóki nie ma wystarczająco zamkniętych sygnałów (is_ready()==False),
    # NIE wpływa na confidence - baseline działa samodzielnie. ---
    features = build_feature_vector(ind, combined, news_analysis, divergences, macro_analysis)
    model_used = False
    if feature_weights is not None and feature_weights.is_ready():
        p_up = feature_weights.predict_proba_up(features)
        model_long_conf = p_up
        model_short_conf = 1 - p_up
        # Blend 50/50 z baseline - model nie przejmuje pełnej kontroli od razu,
        # tylko stopniowo koryguje, w miarę jak zbiera więcej doświadczenia.
        long_conf = 0.5 * long_conf + 0.5 * model_long_conf
        short_conf = 0.5 * short_conf + 0.5 * model_short_conf
        model_used = True

    # --- Punkt C: shadow scoring - logowane zawsze, nigdy nie wpływa na decyzję ---
    shadow_score = compute_shadow_score(ind, combined)
    log_shadow_comparison(name, max(long_conf, short_conf),
                           'LONG' if long_conf >= short_conf else 'SHORT', shadow_score)

    # --- Punkt B: próg pewności skalibrowany per rynek (domyślnie AdaptiveThreshold.DEFAULT) ---
    threshold = adaptive_threshold.get_threshold(name) if adaptive_threshold else AdaptiveThreshold.DEFAULT
    confidence_for_log = max(long_conf, short_conf)
    direction_for_log = 'LONG' if long_conf >= short_conf else 'SHORT'

    # --- Near-miss (punkt D): logujemy KAŻDĄ próbę scoringu, niezależnie od
    # tego, czy przekroczyła próg - z pełnym wektorem cech, żeby dało się
    # to później ewaluować względem rzeczywistego ruchu ceny (patrz
    # evaluate_near_misses) i dokarmić FeatureWeights próbkami "co by było
    # gdyby", z niższą wagą niż prawdziwe sygnały. ---
    log_near_miss(
        name, stage='scored',
        reason='ponizej progu' if confidence_for_log < threshold else 'wyslany',
        long_conf=long_conf, short_conf=short_conf, direction=direction_for_log,
        confidence=confidence_for_log, threshold=threshold, entry=p, atr=ind['atr'],
        features=features, price_source=price_source, regime=regime,
    )

    if long_conf >= threshold or short_conf >= threshold:
        direction = 'LONG' if long_conf >= short_conf else 'SHORT'
        confidence = max(long_conf, short_conf)

        stop_loss = p - 1.5 * ind['atr'] if direction == 'LONG' else p + 1.5 * ind['atr']
        take_profit = p + 2.5 * ind['atr'] if direction == 'LONG' else p - 2.5 * ind['atr']

        position_size, risk_amount, sizing_method = calculate_position_size_kelly(
            confidence, performance_stats, p, stop_loss
        )

        return {
            'name': name,
            'direction': direction,
            'entry': p,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'position_size': position_size,
            'risk_amount': risk_amount,
            'sizing_method': sizing_method,
            'confidence': confidence,
            'threshold_used': threshold,
            'regime': regime,
            'adx': adx_value,
            'model_used': model_used,
            'features': features,
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
            'price_source': price_source,
            'macro_sentiment': macro_sentiment,
            'macro_impact': macro_analysis.get('impact', 'low') if macro_analysis else 'low',
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

    if AGENT_MODE == RunMode.BACKTEST:
        logger.warning(
            "AGENT_MODE=backtest, ale main() nie jest przeznaczone do symulacji historycznej - "
            "użyj backtest_2.py. Przerywam, żeby nie pobierać danych live pod złą etykietą."
        )
        return

    logger.info(f"Tryb pracy: {AGENT_MODE.value}{' (bez wpływu na wagi/próg/statystyki)' if AGENT_MODE == RunMode.SHADOW else ''}")

    if is_monthly_report_time():
        generate_monthly_report()
        return

    if is_friday_evening():
        generate_weekly_report()
        return

    if is_morning_sentiment_time():
        analyze_market_sentiment()
        return

    if is_evening_summary_time():
        send_daily_near_miss_report()
        rotate_jsonl_log(NEAR_MISS_LOG_FILE, days_to_keep=30)
        rotate_jsonl_log(DATA_FETCH_ERRORS_FILE, days_to_keep=30)
        return

    # Dane makro: max 3x dziennie (rano / przed otwarciem / po zamknięciu
    # głównych sesji - patrz config.MACRO_HOURS), niezależnie od cyklu 10-min
    # analizy rynków, żeby nie zalewać Trading Economics/Investing.com/Groq zapytaniami.
    macro_state = load_macro_state()
    if is_macro_fetch_time(macro_state):
        fetch_and_analyze_macro()

    logger.info(f"Analiza rynków: {datetime.now(TIMEZONE)}")
    reset_investing_circuit()
    sm = SignalManager()
    ai_memory = AIMemory()
    tf_weights = TimeframeWeights()
    feature_weights = FeatureWeights()
    adaptive_threshold = AdaptiveThreshold()
    performance_stats = PerformanceStats()
    circuit_breaker = CircuitBreaker()

    # Najpierw ocena wcześniej wysłanych, wciąż otwartych sygnałów - to jest to,
    # co realnie zasila naukę wag interwałów, wag cech, progu i statystyk Kelly
    # (a w trybie SHADOW/BACKTEST te aktualizacje są wyłączone - patrz gating
    # w evaluate_open_signals przez should_update_weights/should_update_threshold/
    # should_record_stats z modes.py).
    sm.evaluate_open_signals(tf_weights, feature_weights, adaptive_threshold,
                              performance_stats, circuit_breaker)

    # Near-miss (punkt D): dojrzałe próbki "co by było gdyby" (odrzucone przez
    # próg) trafiają do FeatureWeights z obniżoną wagą - też tylko gdy tryb na to pozwala.
    if should_update_weights():
        evaluate_near_misses(feature_weights)

    # --- Circuit breaker (punkt B): jeśli dzienny/tygodniowy limit strat
    # w R jest przekroczony, NIE generujemy nowych sygnałów w tym cyklu.
    # Otwarte pozycje nadal są monitorowane (patrz evaluate_open_signals wyżej). ---
    tripped, reason = circuit_breaker.is_tripped()
    if tripped:
        logger.warning(f"⛔ Circuit breaker aktywny: {reason}. Pomijam generowanie nowych sygnałów.")
        if should_send_notifications():
            send_telegram(get_notification_message(f"⛔ *CIRCUIT BREAKER*\n\n{reason}\n\nNowe sygnały wstrzymane do końca okresu."))
        return

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
        signal = analyze_market(name, info, ai_memory, tf_weights.get_weights(), xtb_spreads,
                                 feature_weights, adaptive_threshold, performance_stats)
        if signal and sm.should_send_signal(signal):
            potential.append(signal)

    if potential:
        sorted_sigs = sorted(potential, key=lambda x: x['confidence'], reverse=True)
        final_sigs = apply_cluster_limits(sorted_sigs)[:10]

        msg = "🚨 *TOP SYGNAŁY*\n\n"
        for i, s in enumerate(final_sigs, 1):
            emoji = '🟢' if s['direction'] == 'LONG' else '🔴'
            msg += f"{i}. {emoji} {s['name']} ({s['direction']})\n"
            msg += f"   Pewność: {s['confidence']:.0%} (próg: {s['threshold_used']:.0%})\n"
            msg += f"   Reżim rynku: {s['regime']}" + (f" (ADX {s['adx']:.1f})" if s['adx'] else "") + "\n"
            if s['model_used']:
                msg += f"   ℹ️ Uwzględniono nauczony model cech\n"
            msg += f"   Wejście: {s['entry']:.4f} | SL: {s['stop_loss']:.4f} | TP: {s['take_profit']:.4f}\n"
            spread_label = "spread XTB (realny)" if s['spread_source'] == 'xtb_real' else "proxy zmienności (przybliżenie)"
            msg += f"   Spread: {s['spread_value']*100:.3f}% [{spread_label}]\n"
            msg += f"   Pozycja: {s['position_size']:.4f} jedn. (ryzyko ~{s['risk_amount']:.2f}, metoda: {s['sizing_method']})\n"
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
            if s.get('macro_impact', 'low') != 'low':
                msg += f"   Makro: {s['macro_sentiment']:+.2f} (wpływ: {s['macro_impact']})\n"
            if s['divergences']:
                msg += f"   ⚠️ Dywergencje: {len(s['divergences'])}\n"
            msg += f"\n{s['heatmap']}\n\n"

        if should_send_notifications():
            send_telegram(get_notification_message(msg))
        ai_memory.add_lesson(f"Wysłano {len(final_sigs)} sygnałów")
    else:
        logger.info("Brak sygnałów.")


if __name__ == "__main__":
    main()
