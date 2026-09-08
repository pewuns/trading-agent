"""
Skrypt instalacyjny / aktualizacyjny dla bota tradingowego.
Tworzy i nadpisuje zaktualizowane pliki:
 - data_sources.py
 - config.py
 - modes.py
 - agent.py
"""

import os

FILES = {}

# =====================================================================
# 1. data_sources.py
# =====================================================================
FILES["data_sources.py"] = '''"""
Moduł pobierania danych rynkowych i makroekonomicznych:
- Trading Economics (RSS / Kalendarz)
- Investing.com (Newsy)
- Logowanie błędów pobierania do fetch_errors.jsonl
"""

import json
import logging
from datetime import datetime
import pytz
import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger('trading_bot')
ERROR_LOG_FILE = 'fetch_errors.jsonl'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}


def log_fetch_error(source: str, target: str, error_msg: str, status_code: int = None):
    """Rejestruje każdy błąd pobierania do dedykowanego pliku JSONL."""
    record = {
        'timestamp': datetime.now(pytz.utc).isoformat(),
        'source': source,
        'target': target,
        'status_code': status_code,
        'error': str(error_msg)
    }
    try:
        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\\n')
    except Exception as e:
        logger.error(f"Nie udało się zapisać błędu do {ERROR_LOG_FILE}: {e}")


def fetch_trading_economics_macro() -> list:
    """Pobiera odczyty makroekonomiczne z kanałów RSS Trading Economics."""
    sources = [
        ('TradingEconomics_News', 'https://tradingeconomics.com/rss/news.aspx'),
        ('TradingEconomics_Calendar', 'https://tradingeconomics.com/rss/calendar.aspx')
    ]
    macro_items = []
    for name, url in sources:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                for item in root.findall('.//item'):
                    title = item.find('title')
                    desc = item.find('description')
                    macro_items.append({
                        'title': title.text if title is not None else '',
                        'description': desc.text if desc is not None else '',
                        'source': name
                    })
            else:
                log_fetch_error(name, url, f"Status HTTP {resp.status_code}", resp.status_code)
        except Exception as e:
            log_fetch_error(name, url, str(e))
    return macro_items


def fetch_investing_news() -> list:
    """Pobiera najświeższe nagłówki z Investing.com RSS."""
    url = 'https://www.investing.com/rss/news.rss'
    news = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for item in root.findall('.//item'):
                title = item.find('title')
                if title is not None and title.text:
                    news.append(title.text)
        else:
            log_fetch_error('Investing_RSS', url, f"Status HTTP {resp.status_code}", resp.status_code)
    except Exception as e:
        log_fetch_error('Investing_RSS', url, str(e))
    return news
'''

# =====================================================================
# 2. config.py
# =====================================================================
FILES["config.py"] = '''"""
Konfiguracja trybów pracy, flag funkcjonalności i progów kalibracji.
"""

import os
from enum import Enum


class RunMode(Enum):
    SHADOW = "shadow"       # analiza, logowanie, brak transakcji live
    BACKTEST = "backtest"   # symulacja historyczna
    LIVE = "live"           # rzeczywiste zlecenie


AGENT_MODE = RunMode(os.environ.get('AGENT_MODE', 'shadow').lower())

# Progi sygnałów i kalibracja scoringu
CONFIDENCE_THRESHOLD = 0.60           # Obniżony próg wysyłki sygnałów
MTF_BULLISH_THRESHOLD = 0.55          # Złagodzony warunek UP
MTF_BEARISH_THRESHOLD = 0.45          # Złagodzony warunek DOWN
NEAR_MISS_SAMPLE_WEIGHT = 0.25        # Waga uczenia modeli z near-miss
NEAR_MISS_MIN_AGE_HOURS = 4           # Po ilu godzinach następuje ewaluacja near-miss

# Okna czasowe raportów (czas polski Europe/Warsaw)
MACRO_HOURS = {
    'morning': (8, 0),       # 08:00 - poranek przed sesją EU
    'pre_us': (14, 30),      # 14:30 - przed otwarciem US
    'post_us': (22, 15),     # 22:15 - po zamknięciu sesji US
}
DAILY_SUMMARY_HOUR = (21, 50) # 21:50 - podsumowanie Top 3 "najbliżej progu"

FEATURES = {
    RunMode.SHADOW: {
        'fetch_market_data': True,
        'generate_signals': True,
        'execute_trades': False,
        'send_notifications': True,
        'log_shadow_trades': True,
        'log_near_misses': True,
        'update_learned_weights': False,    # Nie aktualizuj wag ML w trybie shadow
        'record_performance_stats': False,
        'update_adaptive_threshold': False,
    },
    RunMode.LIVE: {
        'fetch_market_data': True,
        'generate_signals': True,
        'execute_trades': True,
        'send_notifications': True,
        'log_shadow_trades': True,
        'log_near_misses': True,
        'update_learned_weights': True,
        'record_performance_stats': True,
        'update_adaptive_threshold': True,
    },
    RunMode.BACKTEST: {
        'fetch_market_data': False,
        'generate_signals': True,
        'execute_trades': False,
        'send_notifications': False,
        'log_shadow_trades': False,
        'log_near_misses': False,
        'update_learned_weights': False,
        'record_performance_stats': False,
        'update_adaptive_threshold': False,
    },
}


def is_mode(target_mode: RunMode) -> bool:
    return AGENT_MODE == target_mode


def is_feature_enabled(feature_name: str) -> bool:
    return FEATURES[AGENT_MODE].get(feature_name, False)


def get_mode_prefix() -> str:
    if AGENT_MODE == RunMode.SHADOW:
        return "🔍 SHADOW: "
    elif AGENT_MODE == RunMode.BACKTEST:
        return "📊 BACKTEST: "
    return ""
'''

# =====================================================================
# 3. modes.py
# =====================================================================
FILES["modes.py"] = '''"""
Helpery do logowania i obsługi trybów pracy agenta.
"""

import json
from datetime import datetime
from pathlib import Path
import logging
import pytz

from config import AGENT_MODE, RunMode, is_feature_enabled, get_mode_prefix

logger = logging.getLogger('trading_bot')

SHADOW_TRADES_LOG = 'shadow_trades.jsonl'
LIVE_TRADES_LOG = 'live_trades.jsonl'
BACKTEST_RESULTS_LOG = 'backtest_results.json'


def log_signal_per_mode(signal: dict, mode: RunMode = None):
    if mode is None:
        mode = AGENT_MODE

    if mode == RunMode.SHADOW:
        _log_to_file(SHADOW_TRADES_LOG, signal)
        logger.info(f"[SHADOW] Zarejestrowano sygnał: {signal['name']} {signal['direction']}")
    elif mode == RunMode.LIVE:
        _log_to_file(LIVE_TRADES_LOG, signal)
        logger.info(f"[LIVE] Otwarto transakcję: {signal['name']} {signal['direction']}")


def log_trade_outcome(signal_key: str, outcome: str, r_multiple: float, mode: RunMode = None):
    if mode is None:
        mode = AGENT_MODE

    record = {
        'timestamp': datetime.now(pytz.utc).isoformat(),
        'signal_key': signal_key,
        'outcome': outcome,
        'r_multiple': r_multiple,
        'mode': mode.value,
    }

    if mode == RunMode.LIVE:
        _log_to_file(LIVE_TRADES_LOG, record)
    elif mode == RunMode.SHADOW:
        _log_to_file(SHADOW_TRADES_LOG, record)


def log_backtest_results(results: dict, market_name: str, period: str):
    record = {
        'timestamp': datetime.now(pytz.utc).isoformat(),
        'market': market_name,
        'period': period,
        'results': results,
    }
    history = []
    if Path(BACKTEST_RESULTS_LOG).exists():
        try:
            with open(BACKTEST_RESULTS_LOG, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(record)
    try:
        with open(BACKTEST_RESULTS_LOG, 'w', encoding='utf-8') as f:
            json.dump(history[-100:], f, indent=2)
    except Exception as e:
        logger.error(f"Błąd zapisu {BACKTEST_RESULTS_LOG}: {e}")


def _log_to_file(filepath: str, record: dict):
    try:
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\\n')
    except Exception as e:
        logger.error(f"Błąd zapisu do {filepath}: {e}")


def get_notification_message(base_message: str) -> str:
    return get_mode_prefix() + base_message


def should_update_weights() -> bool:
    return is_feature_enabled('update_learned_weights')


def should_record_stats() -> bool:
    return is_feature_enabled('record_performance_stats')
'''

# =====================================================================
# 4. agent.py
# =====================================================================
FILES["agent.py"] = '''import os
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
    NEAR_MISS_SAMPLE_WEIGHT, NEAR_MISS_MIN_AGE_HOURS, MACRO_HOURS, DAILY_SUMMARY_HOUR
)
from modes import (
    log_signal_per_mode, log_trade_outcome, get_notification_message,
    should_update_weights, should_record_stats
)
from data_sources import (
    fetch_trading_economics_macro, fetch_investing_news, log_fetch_error
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('trading_bot')

# Tokeny
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# Pliki danych i logów
SIGNALS_FILE = 'signals_history.json'
AI_MEMORY_FILE = 'ai_memory.json'
TIMEFRAME_WEIGHTS_FILE = 'timeframe_weights.json'
FEATURE_WEIGHTS_FILE = 'feature_weights.json'
FEATURE_STORE_FILE = 'feature_store.jsonl'
ADAPTIVE_THRESHOLD_FILE = 'adaptive_thresholds.json'
PERFORMANCE_STATS_FILE = 'performance_stats.json'
CIRCUIT_BREAKER_FILE = 'circuit_breaker_state.json'
SHADOW_LOG_FILE = 'shadow_scoring_log.jsonl'
NEAR_MISS_LOG_FILE = 'near_miss_log.jsonl'
DAILY_SCORES_CACHE = 'daily_scores_cache.json'
MACRO_STATE_FILE = 'macro_state.json'

TIMEZONE = pytz.timezone('Europe/Warsaw')

# Zarządzanie ryzykiem
ACCOUNT_SIZE = float(os.environ.get('ACCOUNT_SIZE', 10000))
RISK_PER_TRADE_PERCENT = float(os.environ.get('RISK_PER_TRADE_PERCENT', 1.0))
MAX_PER_CLUSTER = int(os.environ.get('MAX_PER_CLUSTER', 2))
SIGNAL_TIMEOUT_HOURS = float(os.environ.get('SIGNAL_TIMEOUT_HOURS', 48))

MIN_SAMPLES_FOR_LEARNED_MODEL = int(os.environ.get('MIN_SAMPLES_FOR_LEARNED_MODEL', 30))
MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD = int(os.environ.get('MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD', 15))
MIN_SAMPLES_FOR_KELLY = int(os.environ.get('MIN_SAMPLES_FOR_KELLY', 20))

KELLY_FRACTION = float(os.environ.get('KELLY_FRACTION', 0.5))
MAX_RISK_PERCENT_CAP = float(os.environ.get('MAX_RISK_PERCENT_CAP', 2.0))
DAILY_LOSS_LIMIT_R = float(os.environ.get('DAILY_LOSS_LIMIT_R', -3.0))
WEEKLY_LOSS_LIMIT_R = float(os.environ.get('WEEKLY_LOSS_LIMIT_R', -6.0))
REGIME_ADX_TREND_THRESHOLD = float(os.environ.get('REGIME_ADX_TREND_THRESHOLD', 25))

# Przeliczona suma wag scoringu
TOTAL_SCORE_POINTS = 8.0

DISCLAIMER = (
    "\\n\\n⚠️ _To automatyczny, niebacktestowany system analityczny. "
    "Nie jest to porada inwestycyjna. Handel wiąże się z ryzykiem straty kapitału._"
)

# ============================================
# XTB xAPI
# ============================================
XTB_LOGIN = os.environ.get('XTB_LOGIN')
XTB_PASSWORD = os.environ.get('XTB_PASSWORD')
XTB_ACCOUNT_TYPE = os.environ.get('XTB_ACCOUNT_TYPE', 'demo')

XTB_HOSTS = {
    'demo': ('xapi.xtb.com', 5124),
    'real': ('xapi.xtb.com', 5112),
}

XTB_SYMBOLS = {
    'DAX': 'DE30', 'S&P500': 'US500', 'NASDAQ': 'US100', 'EUR/USD': 'EURUSD',
    'GOLD': 'GOLD', 'OIL WTI': 'OIL.WTI', 'BITCOIN': 'BITCOIN', 'ETHEREUM': 'ETHEREUM',
    'SOLANA': 'SOLANA', 'APPLE': 'APPLE.US', 'MICROSOFT': 'MICROSOFT.US',
    'NVIDIA': 'NVIDIA.US', 'TESLA': 'TESLA.US', 'AMAZON': 'AMAZON.US',
    'META': 'META.US', 'GOOGLE': 'ALPHABET.US',
}

MAX_REAL_SPREAD_PERCENT = {
    'forex': 0.0004, 'index': 0.0015, 'commodity': 0.002,
    'crypto': 0.006, 'stock': 0.0015,
}

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

CORRELATION_CLUSTERS = {
    'DAX': 'indices_eu', 'S&P500': 'indices_us', 'NASDAQ': 'indices_us',
    'EUR/USD': 'forex', 'GOLD': 'metals', 'OIL WTI': 'energy',
    'BITCOIN': 'crypto', 'ETHEREUM': 'crypto', 'SOLANA': 'crypto',
    'APPLE': 'tech_stocks', 'MICROSOFT': 'tech_stocks', 'NVIDIA': 'tech_stocks',
    'TESLA': 'tech_stocks', 'AMAZON': 'tech_stocks', 'META': 'tech_stocks',
    'GOOGLE': 'tech_stocks',
}

TIMEFRAMES = {
    '5m': {'interval': '5m', 'range': '1d', 'default_weight': 0.15},
    '15m': {'interval': '15m', 'range': '1d', 'default_weight': 0.20},
    '1h': {'interval': '60m', 'range': '5d', 'default_weight': 0.25},
    '4h': {'interval': '60m', 'range': '1mo', 'default_weight': 0.15, 'resample_from_60m': 4},
    '1d': {'interval': '1d', 'range': '3mo', 'default_weight': 0.25},
}


class XTBClient:
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
        payload = json.dumps(command_dict) + "\\n"
        self.sock.sendall(payload.encode('utf-8'))

    def _receive(self, buffer_size=8192, timeout=10):
        self.sock.settimeout(timeout)
        chunks = []
        while True:
            chunk = self.sock.recv(buffer_size)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b'\\n'):
                break
        raw = b''.join(chunks).decode('utf-8').strip()
        return json.loads(raw) if raw else None

    def login_session(self):
        try:
            self.connect()
            self._send({"command": "login", "arguments": {"userId": self.login, "password": self.password}})
            resp = self._receive()
            return bool(resp and resp.get('status'))
        except Exception as e:
            logger.error(f"Błąd logowania XTB: {e}")
            return False

    def get_symbol_spread(self, symbol):
        try:
            self._send({"command": "getSymbol", "arguments": {"symbol": symbol}})
            resp = self._receive()
            if not resp or not resp.get('status'):
                return None
            data = resp.get('returnData', {})
            bid, ask = data.get('bid'), data.get('ask')
            if bid is None or ask is None or (bid + ask) == 0:
                return None
            spread_abs = ask - bid
            return {'bid': bid, 'ask': ask, 'spread_abs': spread_abs, 'spread_percent': spread_abs / ((bid + ask) / 2)}
        except Exception:
            return None

    def logout(self):
        try:
            self._send({"command": "logout"})
            self._receive(timeout=3)
        except Exception:
            pass
        finally:
            if self.sock:
                try: self.sock.close()
                except Exception: pass


def fetch_xtb_spreads():
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
            time.sleep(0.15)
    except Exception as e:
        logger.error(f"Błąd sesji XTB: {e}")
    finally:
        client.logout()
    return results


def check_spread_real_or_proxy(market_type, xtb_spread_info, fallback_proxy):
    if xtb_spread_info and xtb_spread_info.get('spread_percent') is not None:
        sp = xtb_spread_info['spread_percent']
        return (sp <= MAX_REAL_SPREAD_PERCENT.get(market_type, 0.002)), 'xtb_real', sp
    ok = check_spread_proxy(market_type, fallback_proxy)
    return ok, 'proxy_zmiennosci', (fallback_proxy or 0.0)


def estimate_spread_proxy(highs, lows, closes):
    if len(closes) < 5: return None
    ranges = [(h - l) / c for h, l, c in zip(highs[-5:], lows[-5:], closes[-5:]) if c]
    return sum(ranges) / len(ranges) if ranges else None


def check_spread_proxy(market_type, spread_proxy):
    max_allowed = {'forex': 0.0006, 'index': 0.003, 'commodity': 0.004, 'crypto': 0.01, 'stock': 0.004}
    return True if spread_proxy is None else (spread_proxy <= max_allowed.get(market_type, 0.004))


def check_extreme_volatility(atr_percent, market_type):
    volatility_limits = {'forex': 1.5, 'index': 3.0, 'commodity': 5.0, 'crypto': 8.0, 'stock': 5.0}
    return atr_percent <= volatility_limits.get(market_type, 3.0)


def check_liquidity(volume, market_type):
    min_volume = {'forex': 1000000, 'index': 100000, 'commodity': 50000, 'crypto': 100, 'stock': 1000000}
    return volume >= min_volume.get(market_type, 100000)


def http_get_with_retry(url, params=None, headers=None, timeout=10, retries=3, backoff=1.5):
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            log_fetch_error('http_get', url, f"Status code {resp.status_code}", resp.status_code)
        except requests.RequestException as e:
            last_exc = e
            log_fetch_error('http_get', url, str(e))
        time.sleep(backoff ** attempt)
    return None


def http_post_with_retry(url, json_payload=None, headers=None, timeout=25, retries=2, backoff=1.5):
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=json_payload, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
        except requests.RequestException:
            pass
        time.sleep(backoff ** attempt)
    return None


def get_market_data(symbol, interval='15m', range_period='1d'):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {'interval': interval, 'range': range_period}
    headers = {'User-Agent': 'Mozilla/5.0'}
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

        aligned = [
            (opens[i], highs[i], lows[i], closes[i], volumes[i])
            for i in range(min(len(closes), len(highs), len(lows), len(volumes), len(opens)))
            if None not in (opens[i], highs[i], lows[i], closes[i], volumes[i])
        ]
        if len(aligned) < 10:
            return None
        o_a, h_a, l_a, c_a, v_a = zip(*aligned)
        return {'prices': list(c_a), 'highs': list(h_a), 'lows': list(l_a), 'volumes': list(v_a), 'opens': list(o_a)}
    except Exception as e:
        log_fetch_error('Yahoo_Parse', symbol, str(e))
        return None


def resample_ohlcv(data, group_size=4):
    if not data or len(data['prices']) < group_size:
        return None
    opens, highs, lows, closes, volumes = data['opens'], data['highs'], data['lows'], data['prices'], data['volumes']
    r_opens, r_highs, r_lows, r_closes, r_volumes = [], [], [], [], []
    for i in range(0, len(closes), group_size):
        c_o, c_h, c_l, c_c, c_v = opens[i:i+group_size], highs[i:i+group_size], lows[i:i+group_size], closes[i:i+group_size], volumes[i:i+group_size]
        if not c_c: continue
        r_opens.append(c_o[0]); r_highs.append(max(c_h)); r_lows.append(min(c_l)); r_closes.append(c_c[-1]); r_volumes.append(sum(c_v))
    return {'prices': r_closes, 'highs': r_highs, 'lows': r_lows, 'volumes': r_volumes, 'opens': r_opens} if len(r_closes) >= 5 else None


def calculate_base_indicators(data):
    if not data or len(data['prices']) < 20: return None
    prices, highs, lows = data['prices'], data['highs'], data['lows']
    current_price = prices[-1]

    def sma(arr, p): return sum(arr[-p:]) / p if len(arr) >= p else None
    def rsi(arr, p=14):
        if len(arr) < p + 1: return 50
        gains = [max(0, arr[i] - arr[i-1]) for i in range(1, len(arr))]
        losses = [max(0, -(arr[i] - arr[i-1])) for i in range(1, len(arr))]
        avg_g, avg_l = sum(gains[-p:]) / p, sum(losses[-p:]) / p
        return 100 if avg_l == 0 else (100 - (100 / (1 + avg_g / avg_l)))
    def atr(h, l, c, p=14):
        if len(c) < p + 1: return 0
        trs = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(c))]
        return sum(trs[-p:]) / p

    atr_val = atr(highs, lows, prices)
    return {
        'price': current_price, 'sma20': sma(prices, 20), 'sma50': sma(prices, 50),
        'rsi': rsi(prices), 'atr': atr_val,
        'atr_percent': (atr_val / current_price) * 100 if current_price else 0,
        'avg_volume': sum(data['volumes'][-20:]) / min(len(data['volumes']), 20) if data.get('volumes') else 0,
        'highs': highs, 'lows': lows, 'opens': data.get('opens', []), 'volumes': data.get('volumes', [])
    }


def calculate_full_indicators(data):
    base = calculate_base_indicators(data)
    if not base: return None
    prices, volumes, highs, lows, opens = data['prices'], data.get('volumes', []), data['highs'], data['lows'], data.get('opens', [])

    vwap = (sum(p * v for p, v in zip(prices, volumes)) / sum(volumes)) if (volumes and sum(volumes) > 0) else None
    sr = {'support': min(prices[-20:]), 'resistance': max(prices[-20:]), 'nearest_level': 'SUPPORT' if (prices[-1] - min(prices[-20:])) < (max(prices[-20:]) - prices[-1]) else 'RESISTANCE'} if len(prices) >= 20 else {}

    patterns = []
    if len(prices) >= 3 and opens:
        o1, c1, h1, l1, o2, c2 = opens[-1], prices[-1], highs[-1], lows[-1], opens[-2], prices[-2]
        body = abs(c1 - o1)
        if (min(o1, c1) - l1) > 2 * body: patterns.append('MŁOT (byczy)')
        if (h1 - max(o1, c1)) > 2 * body: patterns.append('SPADAJĄCA GWIAZDA (niedźwiedzi)')
        if c2 < o2 and c1 > o1 and c1 > o2 and o1 < c2: patterns.append('OBJĘCIE HOSSY (bycze)')
        if c2 > o2 and c1 < o1 and c1 < o2 and o1 > c2: patterns.append('OBJĘCIE BESSY (niedźwiedzie)')

    order_flow = {'delta_percent': 0}
    if len(prices) >= 2 and volumes:
        buy_v = sum(volumes[i] for i in range(1, len(prices)) if prices[i] > prices[i-1] and i < len(volumes))
        sell_v = sum(volumes[i] for i in range(1, len(prices)) if prices[i] < prices[i-1] and i < len(volumes))
        tot = buy_v + sell_v
        order_flow['delta_percent'] = ((buy_v - sell_v) / tot * 100) if tot > 0 else 0

    base.update({
        'vwap': vwap, 'support': sr.get('support'), 'resistance': sr.get('resistance'),
        'nearest_level': sr.get('nearest_level'), 'candlestick_patterns': patterns,
        'order_flow': order_flow, 'volume_profile': {'poc': prices[-1]},
        'spread_proxy': estimate_spread_proxy(highs, lows, prices)
    })
    return base


def patterns_directional_bias(patterns):
    has_bull = any('bycz' in p.lower() or 'hossy' in p.lower() for p in patterns)
    has_bear = any('niedźwiedzi' in p.lower() or 'bessy' in p.lower() for p in patterns)
    return 'bull' if has_bull and not has_bear else ('bear' if has_bear and not has_bull else None)


def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2: return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        up, down = highs[i] - highs[i-1], lows[i-1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0)
        minus_dm.append(down if (down > up and down > 0) else 0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))

    def wilder(arr, p):
        if len(arr) < p: return []
        s = [sum(arr[:p])]
        for x in arr[p:]: s.append(s[-1] - (s[-1] / p) + x)
        return s

    tr_s, p_s, m_s = wilder(trs, period), wilder(plus_dm, period), wilder(minus_dm, period)
    if not tr_s or not p_s or not m_s: return None
    n = min(len(tr_s), len(p_s), len(m_s))
    dx = [100 * abs(p_s[i] - m_s[i]) / (p_s[i] + m_s[i]) if (p_s[i] + m_s[i]) else 0 for i in range(n)]
    return sum(dx[-period:]) / period if len(dx) >= period else None


def detect_market_regime(highs, lows, closes):
    adx = calculate_adx(highs, lows, closes)
    return (('TREND' if adx >= REGIME_ADX_TREND_THRESHOLD else 'RANGE'), adx) if adx else ('UNKNOWN', None)


# ============================================
# KLASY ML, STATYSTYK I PAMIĘCI
# ============================================

class AIMemory:
    def __init__(self, file_path=AI_MEMORY_FILE):
        self.file_path = file_path
        self.memory = self.load()

    def load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f: return json.load(f)
        except Exception: pass
        return {'lessons': []}

    def save(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f: json.dump(self.memory, f, indent=2, ensure_ascii=False)
        except Exception: pass

    def add_lesson(self, lesson):
        self.memory['lessons'].append({'timestamp': datetime.now(pytz.utc).isoformat(), 'lesson': lesson})
        self.memory['lessons'] = self.memory['lessons'][-300:]
        self.save()

    def get_context(self):
        return "\\n".join([f"- {l['lesson'][:150]}" for l in self.memory['lessons'][-10:]]) if self.memory['lessons'] else "Brak."


class TimeframeWeights:
    def __init__(self, file_path=TIMEFRAME_WEIGHTS_FILE):
        self.file_path = file_path
        self.weights = {tf: cfg['default_weight'] for tf, cfg in TIMEFRAMES.items()}
        self.load()

    def load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f:
                    d = json.load(f)
                    self.weights.update(d.get('weights', d))
            except Exception: pass

    def save(self):
        try:
            with open(self.file_path, 'w') as f: json.dump({'weights': self.weights}, f, indent=2)
        except Exception: pass

    def get_weights(self): return self.weights

    def update_from_outcome(self, signal, outcome):
        if outcome not in ('win', 'loss'): return
        expected = 'UP' if signal.get('direction') == 'LONG' else 'DOWN'
        sign = 1 if outcome == 'win' else -1
        for tf, trend in signal.get('timeframe_trends', {}).items():
            if tf in self.weights:
                delta = 0.02 * sign if trend == expected else -0.01 * sign
                self.weights[tf] = max(0.05, min(0.5, self.weights[tf] + delta))
        tot = sum(self.weights.values())
        if tot > 0:
            for k in self.weights: self.weights[k] /= tot
        self.save()


class FeatureWeights:
    FEATURE_NAMES = ['sma20', 'sma50', 'rsi', 'vwap', 'support_resistance', 'pattern', 'order_flow', 'mtf_trend', 'macro_boost']

    def __init__(self, file_path=FEATURE_WEIGHTS_FILE):
        self.file_path = file_path
        self.weights = {f: 0.0 for f in self.FEATURE_NAMES}
        self.bias = 0.0
        self.n_samples = 0
        self.base_learning_rate = 0.05
        self.load()

    def load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f:
                    d = json.load(f)
                    self.weights = d.get('weights', self.weights)
                    self.bias = d.get('bias', 0.0)
                    self.n_samples = d.get('n_samples', 0)
            except Exception: pass

    def save(self):
        try:
            with open(self.file_path, 'w') as f:
                json.dump({'weights': self.weights, 'bias': self.bias, 'n_samples': self.n_samples}, f, indent=2)
        except Exception: pass

    def is_ready(self): return self.n_samples >= MIN_SAMPLES_FOR_LEARNED_MODEL

    def predict_proba_up(self, features):
        z = self.bias + sum(self.weights.get(k, 0.0) * features.get(k, 0.0) for k in self.FEATURE_NAMES)
        return 1.0 / (1.0 + np.exp(-max(-20.0, min(20.0, z))))

    def update(self, features, went_up):
        y = 1.0 if went_up else 0.0
        p = self.predict_proba_up(features)
        err = p - y
        lr = self.base_learning_rate / (1 + self.n_samples / 50)
        for k in self.FEATURE_NAMES:
            self.weights[k] -= lr * err * features.get(k, 0.0)
        self.bias -= lr * err
        self.n_samples += 1
        self.save()


class AdaptiveThreshold:
    def __init__(self, file_path=ADAPTIVE_THRESHOLD_FILE):
        self.file_path = file_path
        self.data = self.load()

    def load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f: return json.load(f)
            except Exception: pass
        return {}

    def save(self):
        try:
            with open(self.file_path, 'w') as f: json.dump(self.data, f, indent=2)
        except Exception: pass

    def get_threshold(self, name):
        entry = self.data.get(name)
        return entry.get('threshold', CONFIDENCE_THRESHOLD) if (entry and len(entry.get('history', [])) >= MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD) else CONFIDENCE_THRESHOLD

    def record_outcome(self, name, conf, outcome):
        if outcome not in ('win', 'loss'): return
        entry = self.data.setdefault(name, {'threshold': CONFIDENCE_THRESHOLD, 'history': []})
        entry['history'].append([conf, outcome == 'win'])
        entry['history'] = entry['history'][-150:]
        if len(entry['history']) >= MIN_SAMPLES_FOR_ADAPTIVE_THRESHOLD:
            for th in sorted(set(round(c, 2) for c, _ in entry['history'])):
                sub = [w for c, w in entry['history'] if c >= th]
                if len(sub) >= 5 and (sum(sub) / len(sub)) >= 0.55:
                    entry['threshold'] = th
                    break
        self.save()


class PerformanceStats:
    def __init__(self, file_path=PERFORMANCE_STATS_FILE):
        self.file_path = file_path
        self.records = []
        self.load()

    def load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f: self.records = json.load(f)
            except Exception: pass

    def save(self):
        try:
            with open(self.file_path, 'w') as f: json.dump(self.records[-500:], f, indent=2)
        except Exception: pass

    def record(self, outcome, r_mult):
        if outcome in ('win', 'loss'):
            self.records.append({'outcome': outcome, 'r_multiple': r_mult})
            self.save()

    def get_stats(self):
        if len(self.records) < MIN_SAMPLES_FOR_KELLY: return None
        wins = [r['r_multiple'] for r in self.records if r['outcome'] == 'win']
        losses = [abs(r['r_multiple']) for r in self.records if r['outcome'] == 'loss']
        return (len(wins) / len(self.records), sum(wins)/len(wins), sum(losses)/len(losses)) if (wins and losses) else None


class CircuitBreaker:
    def __init__(self, file_path=CIRCUIT_BREAKER_FILE):
        self.file_path = file_path
        self.trades = []
        self.load()

    def load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f: self.trades = json.load(f).get('trades', [])
            except Exception: pass

    def save(self):
        try:
            with open(self.file_path, 'w') as f: json.dump({'trades': self.trades[-200:]}, f, indent=2)
        except Exception: pass

    def record_trade(self, r_mult):
        self.trades.append({'timestamp': datetime.now(pytz.utc).isoformat(), 'r_multiple': r_mult})
        self.save()

    def is_tripped(self):
        now = datetime.now(pytz.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_r = sum(t['r_multiple'] for t in self.trades if datetime.fromisoformat(t['timestamp']) >= day_start)
        return (True, f"Dzienny limit strat ({daily_r:.2f}R)") if daily_r <= DAILY_LOSS_LIMIT_R else (False, None)


class SignalManager:
    def __init__(self, file_path=SIGNALS_FILE):
        self.file_path = file_path
        self.signals = self.load_signals()

    def load_signals(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f: return json.load(f)
            except Exception: pass
        return {}

    def save_signals(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f: json.dump(self.signals, f, indent=2, ensure_ascii=False)
        except Exception: pass

    def should_send_signal(self, signal):
        key = f"{signal['name']}_{signal['direction']}"
        if key not in self.signals:
            self._store(key, signal)
            return True
        prev = self.signals[key]
        price_diff = abs(signal['entry'] - prev['entry']) / prev['entry'] * 100
        mins = (datetime.now(pytz.utc) - datetime.fromisoformat(prev['timestamp'])).total_seconds() / 60
        if price_diff >= 0.3 or mins >= 30:
            self._store(key, signal)
            return True
        return False

    def _store(self, key, signal):
        s = dict(signal)
        s['timestamp'] = datetime.now(pytz.utc).isoformat()
        s['status'] = 'open'
        s['risk_distance'] = abs(s['entry'] - s['stop_loss'])
        self.signals[key] = s
        self.save_signals()

    def evaluate_open_signals(self, tf_weights, feature_weights, adaptive_threshold, performance_stats, circuit_breaker):
        now = datetime.now(pytz.utc)
        any_up = False
        for key, sig in list(self.signals.items()):
            if sig.get('status') != 'open': continue
            market_info = MARKETS.get(sig.get('name'))
            if not market_info: continue
            data = get_market_data(market_info['symbol'], '15m', '1d')
            if not data or not data['prices']: continue
            cp = data['prices'][-1]
            d, entry = sig['direction'], sig['entry']
            age_h = (now - datetime.fromisoformat(sig['timestamp'])).total_seconds() / 3600

            hit_tp = (d == 'LONG' and cp >= sig['take_profit']) or (d == 'SHORT' and cp <= sig['take_profit'])
            hit_sl = (d == 'LONG' and cp <= sig['stop_loss']) or (d == 'SHORT' and cp >= sig['stop_loss'])
            outcome = 'win' if hit_tp else ('loss' if hit_sl else ('timeout' if age_h >= SIGNAL_TIMEOUT_HOURS else None))

            if outcome:
                sig['status'] = 'closed'
                sig['outcome'] = outcome
                raw_m = (cp - entry) if d == 'LONG' else (entry - cp)
                sig['r_multiple'] = raw_m / sig['risk_distance'] if sig.get('risk_distance') else 0.0
                any_up = True

                log_trade_outcome(key, outcome, sig['r_multiple'])

                if should_update_weights():
                    tf_weights.update_from_outcome(sig, outcome)
                    if outcome in ('win', 'loss') and feature_weights and sig.get('features'):
                        feature_weights.update(sig['features'], went_up=(d == 'LONG' and outcome == 'win') or (d == 'SHORT' and outcome == 'loss'))

                if is_feature_enabled('update_adaptive_threshold') and adaptive_threshold:
                    adaptive_threshold.record_outcome(sig['name'], sig.get('confidence', 0), outcome)

                if should_record_stats() and outcome in ('win', 'loss'):
                    if performance_stats: performance_stats.record(outcome, sig['r_multiple'])
                    if circuit_breaker: circuit_breaker.record_trade(sig['r_multiple'])
        if any_up:
            self.save_signals()


# ============================================
# POMOCNIKI SHADOW, NEAR-MISS I MAKRO
# ============================================

def log_shadow_attempt(name: str, status: str, reason: str, confidence: float = 0.0, direction: str = 'NONE', raw_score: float = 0.0, features: dict = None):
    record = {
        'timestamp': datetime.now(pytz.utc).isoformat(),
        'name': name, 'status': status, 'reason': reason,
        'confidence': confidence, 'direction': direction,
        'raw_score': raw_score, 'features': features or {}
    }
    try:
        with open(SHADOW_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\\n')
    except Exception: pass


def log_near_miss(candidate: dict):
    record = {
        'timestamp': datetime.now(pytz.utc).isoformat(),
        'name': candidate['name'], 'direction': candidate['direction'],
        'confidence': candidate['confidence'], 'entry_price': candidate['entry'],
        'atr': candidate.get('atr', 0), 'features': candidate.get('features', {}),
        'status': 'pending'
    }
    try:
        with open(NEAR_MISS_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\\n')
    except Exception: pass


def evaluate_near_misses(feature_weights, adaptive_threshold):
    if not os.path.exists(NEAR_MISS_LOG_FILE): return
    now = datetime.now(pytz.utc)
    updated, needs_rewrite = [], False

    with open(NEAR_MISS_LOG_FILE, 'r', encoding='utf-8') as f: lines = f.readlines()
    for line in lines:
        if not line.strip(): continue
        rec = json.loads(line)
        if rec.get('status') != 'pending':
            updated.append(rec)
            continue
        if (now - datetime.fromisoformat(rec['timestamp'])).total_seconds() / 3600 >= NEAR_MISS_MIN_AGE_HOURS:
            market_info = MARKETS.get(rec['name'])
            if not market_info:
                rec['status'] = 'expired'; updated.append(rec); needs_rewrite = True; continue
            data = get_market_data(market_info['symbol'], '15m', '1d')
            if not data or not data['prices']:
                updated.append(rec); continue

            cp = data['prices'][-1]
            orig_lr = feature_weights.base_learning_rate
            feature_weights.base_learning_rate = orig_lr * NEAR_MISS_SAMPLE_WEIGHT
            feature_weights.update(rec['features'], went_up=(cp > rec['entry_price']))
            feature_weights.base_learning_rate = orig_lr

            rec['status'] = 'evaluated'
            rec['final_price'] = cp
            needs_rewrite = True
        updated.append(rec)

    if needs_rewrite:
        with open(NEAR_MISS_LOG_FILE, 'w', encoding='utf-8') as f:
            for r in updated[-500:]: f.write(json.dumps(r, ensure_ascii=False) + '\\n')


def update_daily_scores_cache(market_name, conf, direction):
    today = datetime.now(TIMEZONE).strftime('%Y-%m-%d')
    cache = {'date': today, 'scores': {}}
    if os.path.exists(DAILY_SCORES_CACHE):
        try:
            with open(DAILY_SCORES_CACHE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                if d.get('date') == today: cache = d
        except Exception: pass
    curr = cache['scores'].get(market_name, {'confidence': 0.0, 'direction': 'NONE'})
    if conf > curr['confidence']:
        cache['scores'][market_name] = {'confidence': conf, 'direction': direction}
    with open(DAILY_SCORES_CACHE, 'w', encoding='utf-8') as f: json.dump(cache, f, indent=2)


def send_daily_near_threshold_summary():
    if not os.path.exists(DAILY_SCORES_CACHE): return
    try:
        with open(DAILY_SCORES_CACHE, 'r', encoding='utf-8') as f: cache = json.load(f)
        top3 = sorted(cache.get('scores', {}).items(), key=lambda x: x[1]['confidence'], reverse=True)[:3]
        if not top3: return
        msg = "📋 *PODSUMOWANIE DNIA: NAJBLIŻEJ PROGU*\\n\\n"
        for i, (name, data) in enumerate(top3, 1):
            emoji = '🟢' if data['direction'] == 'LONG' else '🔴'
            msg += f"{i}. {emoji} *{name}*: {data['confidence']:.1%} ({data['direction']}) [Próg: {CONFIDENCE_THRESHOLD:.0%}]\\n"
        send_telegram(msg, add_disclaimer=False)
    except Exception: pass


def run_macro_analysis_cycle(ai_memory):
    logger.info("Uruchamianie cyklu makro...")
    macro_items = fetch_trading_economics_macro()
    investing_news = fetch_investing_news()
    context = "\\n".join([f"- {m['title']}: {m.get('description','')[:90]}" for m in macro_items[:12]])
    context += "\\n" + "\\n".join([f"- Investing: {n}" for n in investing_news[:8]])
    if not context.strip(): return

    prompt = f"""Przeanalizuj poniższe dane makro pod kątem rynków finansowych:
{context}

Wybierz TOP 5 najważniejszych odczytów i określ wpływ na rynki.
Odpowiedz wyłącznie w JSON:
{{
  "top_5_factors": ["1. ...", "2. ...", "3. ...", "4. ...", "5. ..."],
  "macro_sentiment": 0.1,
  "market_impacts": {{"S&P500": 0.2, "GOLD": -0.1, "BITCOIN": 0.0, "OIL WTI": 0.2, "EUR/USD": -0.1}}
}}"""
    res = call_groq("Analityk makroekonomiczny.", prompt, max_tokens=600)
    if res:
        m = re.search(r'\\{.*\\}', res, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
                with open(MACRO_STATE_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
                top_msg = "\\n".join(data.get('top_5_factors', []))
                send_telegram(f"🌐 *RAPORT MAKRO (Top 5 Czynników)*\\n\\nSentyment: {data.get('macro_sentiment', 0):+.2f}\\n\\n{top_msg}", add_disclaimer=False)
                ai_memory.add_lesson(f"Makro sentyment: {data.get('macro_sentiment', 0)}")
            except Exception: pass


# ============================================
# GŁÓWNA LOGIKA ANALIZY I SYSTEMU
# ============================================

def call_groq(system_prompt, user_prompt, max_tokens=600):
    if not GROQ_API_KEY: return None
    payload = {"model": "llama-3.3-70b-versatile", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], "temperature": 0.3, "max_tokens": max_tokens}
    resp = http_post_with_retry("https://api.groq.com/openai/v1/chat/completions", json_payload=payload, headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"})
    try: return resp.json()['choices'][0]['message']['content'] if resp else None
    except Exception: return None


def is_session_active(session):
    h = datetime.now(TIMEZONE).hour
    return True if session == '24_7' else ((15 <= h < 22) if session == 'US' else ((9 <= h < 17) if session == 'EU' else (9 <= h < 22)))


def send_telegram(message, add_disclaimer=True):
    msg = get_notification_message(message) + (DISCLAIMER if add_disclaimer else "")
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return False
    try:
        resp = http_post_with_retry(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json_payload={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})
        return resp is not None
    except Exception: return False


def analyze_timeframes(symbol, tf_weights):
    results = {}
    for tf, cfg in TIMEFRAMES.items():
        data = get_market_data(symbol, cfg['interval'], cfg['range'])
        if cfg.get('resample_from_60m'): data = resample_ohlcv(data, cfg['resample_from_60m'])
        ind = calculate_base_indicators(data)
        if ind:
            t = 0
            if ind['price'] and ind['sma20'] and ind['price'] > ind['sma20']: t += 1
            if ind['price'] and ind['sma50'] and ind['price'] > ind['sma50']: t += 1
            ind['trend'] = 'UP' if t == 2 else ('DOWN' if t == 0 else 'SIDEWAYS')
            ind['weight'] = tf_weights.get(tf, cfg['default_weight'])
            results[tf] = ind
    return results or None


def combine_timeframe_analysis(tf_results):
    if not tf_results: return None
    t_score, tot_w = 0.0, 0.0
    for tf, ind in tf_results.items():
        w = ind.get('weight', 0.2)
        tot_w += w
        t_score += (1.0 if ind.get('trend') == 'UP' else (0.0 if ind.get('trend') == 'DOWN' else 0.5)) * w
    return {'trend_score': t_score / tot_w if tot_w else 0.5}


def detect_divergences(tf_results):
    divs = []
    if tf_results and '15m' in tf_results and '1d' in tf_results:
        t_s, t_l = tf_results['15m'].get('trend'), tf_results['1d'].get('trend')
        if t_s == 'UP' and t_l == 'DOWN': divs.append("15m UP vs 1d DOWN")
        elif t_s == 'DOWN' and t_l == 'UP': divs.append("15m DOWN vs 1d UP")
    return divs


def calculate_position_size_kelly(conf, stats, entry, sl):
    dist = abs(entry - sl)
    if dist == 0: return 0, 0, 'brak_danych'
    st = stats.get_stats() if stats else None
    if not st: return (ACCOUNT_SIZE * 0.01) / dist, ACCOUNT_SIZE * 0.01, 'stały_1%'
    w, avg_w, avg_l = st
    b = avg_w / avg_l if avg_l > 0 else 1
    f = max(0.0, w - (1 - w) / b) * KELLY_FRACTION
    risk_pct = min(MAX_RISK_PERCENT_CAP, f * 100) * conf
    risk_amt = ACCOUNT_SIZE * (risk_pct / 100)
    return risk_amt / dist, risk_amt, 'fractional_kelly'


def analyze_market(name, info, ai_memory, tf_weights, xtb_spreads, feature_weights, adaptive_threshold, performance_stats):
    if not is_session_active(info.get('session', '24_7')):
        log_shadow_attempt(name, 'FILTERED_OUT', 'session_inactive'); return None

    main_data = get_market_data(info['symbol'], '15m', '1d')
    if not main_data:
        log_shadow_attempt(name, 'FILTERED_OUT', 'data_fetch_failed'); return None

    ind = calculate_full_indicators(main_data)
    if not ind: return None

    xtb_info = (xtb_spreads or {}).get(name)
    ok_sp, sp_src, sp_val = check_spread_real_or_proxy(info['type'], xtb_info, ind['spread_proxy'])
    if not ok_sp: log_shadow_attempt(name, 'FILTERED_OUT', f'high_spread_{sp_val:.4f}'); return None
    if not check_extreme_volatility(ind['atr_percent'], info['type']): log_shadow_attempt(name, 'FILTERED_OUT', 'extreme_volatility'); return None
    if not check_liquidity(ind['avg_volume'], info['type']): log_shadow_attempt(name, 'FILTERED_OUT', 'low_liquidity'); return None

    tf_res = analyze_timeframes(info['symbol'], tf_weights)
    if not tf_res: return None
    combined = combine_timeframe_analysis(tf_res)
    regime, adx_val = detect_market_regime(main_data['highs'], main_data['lows'], main_data['prices'])

    macro_boost = 0.0
    if os.path.exists(MACRO_STATE_FILE):
        try:
            with open(MACRO_STATE_FILE, 'r', encoding='utf-8') as f:
                macro_boost = json.load(f).get('market_impacts', {}).get(name, 0.0)
        except Exception: pass

    p = ((xtb_info['bid'] + xtb_info['ask']) / 2) if (xtb_info and xtb_info.get('bid') and xtb_info.get('ask')) else ind['price']
    long_score, short_score = 0.0, 0.0

    # Przeliczone wagi bazowe
    if ind['sma20']: (long_score := long_score + 1.0) if p > ind['sma20'] else (short_score := short_score + 1.0)
    if ind['sma50']: (long_score := long_score + 1.0) if p > ind['sma50'] else (short_score := short_score + 1.0)
    if ind['rsi'] > 50: long_score += 1.0
    else: short_score += 1.0
    if ind['vwap']: (long_score := long_score + 1.0) if p > ind['vwap'] else (short_score := short_score + 1.0)

    sr_w = 1.0 if regime == 'RANGE' else 0.75
    if ind['nearest_level'] == 'SUPPORT': long_score += sr_w
    elif ind['nearest_level'] == 'RESISTANCE': short_score += sr_w

    bias = patterns_directional_bias(ind['candlestick_patterns'])
    if bias == 'bull': long_score += 0.50
    elif bias == 'bear': short_score += 0.50

    if ind['order_flow']['delta_percent'] > 0: long_score += 0.50
    elif ind['order_flow']['delta_percent'] < 0: short_score += 0.50

    # Złagodzony bonus MTF
    mtf_bonus = 1.25 if regime != 'RANGE' else 0.75
    if combined['trend_score'] >= MTF_BULLISH_THRESHOLD: long_score += mtf_bonus
    elif combined['trend_score'] <= MTF_BEARISH_THRESHOLD: short_score += mtf_bonus

    if macro_boost > 0: long_score += min(0.5, macro_boost)
    elif macro_boost < 0: short_score += min(0.5, abs(macro_boost))

    long_conf = min(1.0, max(0.0, long_score / TOTAL_SCORE_POINTS))
    short_conf = min(1.0, max(0.0, short_score / TOTAL_SCORE_POINTS))

    features = {'sma20': 1.0 if p > (ind['sma20'] or p) else -1.0, 'rsi': (ind['rsi']-50)/50, 'macro_boost': macro_boost}
    model_used = False
    if feature_weights and feature_weights.is_ready():
        p_up = feature_weights.predict_proba_up(features)
        long_conf = 0.6 * long_conf + 0.4 * p_up
        short_conf = 0.6 * short_conf + 0.4 * (1 - p_up)
        model_used = True

    chosen_d = 'LONG' if long_conf >= short_conf else 'SHORT'
    chosen_c = max(long_conf, short_conf)
    th = adaptive_threshold.get_threshold(name) if adaptive_threshold else CONFIDENCE_THRESHOLD

    update_daily_scores_cache(name, chosen_c, chosen_d)

    cand = {
        'name': name, 'direction': chosen_d, 'entry': p,
        'stop_loss': p - 1.5 * ind['atr'] if chosen_d == 'LONG' else p + 1.5 * ind['atr'],
        'take_profit': p + 2.5 * ind['atr'] if chosen_d == 'LONG' else p - 2.5 * ind['atr'],
        'atr': ind['atr'], 'confidence': chosen_c, 'threshold_used': th,
        'regime': regime, 'model_used': model_used, 'features': features,
        'spread_value': sp_val, 'rsi': ind['rsi'], 'vwap': ind['vwap']
    }

    log_near_miss(cand)
    log_shadow_attempt(name, 'ANALYZED', 'passed' if chosen_c >= th else 'below_threshold', chosen_c, chosen_d, max(long_score, short_score), features)

    if chosen_c >= th:
        ps, r_amt, sz_m = calculate_position_size_kelly(chosen_c, performance_stats, p, cand['stop_loss'])
        cand.update({'position_size': ps, 'risk_amount': r_amt, 'sizing_method': sz_m})
        return cand
    return None


def is_time_for(target_h, target_m, window_m=14):
    now = datetime.now(TIMEZONE)
    return now.hour == target_h and target_m <= now.minute < (target_m + window_m)


def main():
    if datetime.now(TIMEZONE).weekday() >= 5: return

    ai_memory = AIMemory()

    # Sprawdzenie okien publikacji makro
    for _, (h, m) in MACRO_HOURS.items():
        if is_time_for(h, m):
            run_macro_analysis_cycle(ai_memory)
            break

    # Sprawdzenie podsumowania dziennego Top 3
    if is_time_for(DAILY_SUMMARY_HOUR[0], DAILY_SUMMARY_HOUR[1]):
        send_daily_near_threshold_summary()

    sm = SignalManager()
    tf_weights = TimeframeWeights()
    feature_weights = FeatureWeights()
    adaptive_threshold = AdaptiveThreshold()
    performance_stats = PerformanceStats()
    circuit_breaker = CircuitBreaker()

    sm.evaluate_open_signals(tf_weights, feature_weights, adaptive_threshold, performance_stats, circuit_breaker)
    evaluate_near_misses(feature_weights, adaptive_threshold)

    tripped, reason = circuit_breaker.is_tripped()
    if tripped:
        logger.warning(f"Circuit breaker: {reason}")
        return

    xtb_spreads = fetch_xtb_spreads()
    potential = []
    for name, info in MARKETS.items():
        sig = analyze_market(name, info, ai_memory, tf_weights.get_weights(), xtb_spreads, feature_weights, adaptive_threshold, performance_stats)
        if sig and sm.should_send_signal(sig): potential.append(sig)

    if potential:
        sigs = sorted(potential, key=lambda x: x['confidence'], reverse=True)[:5]
        for s in sigs: log_signal_per_mode(s)
        if is_feature_enabled('send_notifications'):
            msg = "🚨 *TOP SYGNAŁY*\\n\\n"
            for i, s in enumerate(sigs, 1):
                em = '🟢' if s['direction'] == 'LONG' else '🔴'
                msg += f"{i}. {em} {s['name']} ({s['direction']})\\n   Pewność: {s['confidence']:.0%} (próg: {s['threshold_used']:.0%})\\n   Wejście: {s['entry']:.4f} | SL: {s['stop_loss']:.4f} | TP: {s['take_profit']:.4f}\\n\\n"
            send_telegram(msg)


if __name__ == "__main__":
    main()
'''

def main():
    print("Rozpoczynam tworzenie/aktualizację plików...")
    for filename, content in FILES.items():
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  [+] Utworzono pomyślnie: {filename}")
    print("\nGotowe! Wszystkie pliki zostały zaktualizowane.")

if __name__ == "__main__":
    main()
