"""
Moduł pobierania danych makro i newsów spoza Yahoo Finance:
- Trading Economics (RSS: newsy + kalendarz)
- Investing.com (RSS: newsy)

Uwaga: to NIE jest źródło cen (OHLCV) - próba pobrania świec z Investing.com
(GŁÓWNE źródło cen, z fallbackiem na Yahoo) jest osobną funkcją w agent.py
(fetch_investing_data/get_price_data), bo korzysta z zupełnie innego,
niezudokumentowanego endpointu tej samej strony, nie z RSS. Ten moduł
(data_sources.py) dostarcza wyłącznie dodatkowy kontekst makro/newsowy do
analyze_macro_with_ai - RSS jest tu wybrany świadomie, bo jest publiczny,
stabilny i nie wymaga klucza API (w przeciwieństwie do JSON API Trading
Economics, gdzie darmowy dostęp 'guest:guest' ogranicza się do garstki
krajów demo).

Każdy błąd pobrania trafia do wspólnego DATA_FETCH_ERRORS_FILE (ten sam plik,
którego używa reszta agent.py do logowania błędów źródeł danych).
"""

import json
import logging
from datetime import datetime
import pytz
import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger('trading_bot')
ERROR_LOG_FILE = 'data_fetch_errors.jsonl'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}


def log_fetch_error(source: str, target: str, error_msg: str, status_code: int = None):
    """Rejestruje każdy błąd pobierania do dedykowanego pliku JSONL - żeby dało
    się ocenić realną skuteczność RSS zamiast zgadywać z samych logów tekstowych."""
    record = {
        'timestamp': datetime.now(pytz.utc).isoformat(),
        'source': source,
        'target': target,
        'status_code': status_code,
        'error': str(error_msg),
    }
    try:
        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.error(f"Nie udało się zapisać błędu do {ERROR_LOG_FILE}: {e}")
    logger.warning(f"[{source}] Błąd pobierania {target}: {error_msg}")


def fetch_trading_economics_macro() -> list:
    """Pobiera odczyty makroekonomiczne z publicznych kanałów RSS Trading
    Economics (newsy + kalendarz). W przeciwieństwie do ich JSON API,
    RSS nie wymaga klucza (guest:guest byłby mocno ograniczony do garstki
    krajów demo) - ale w zamian daje mniej ustrukturyzowane dane (tytuł +
    opis tekstowy zamiast osobnych pól actual/forecast/previous), stąd
    interpretacja przez AI (patrz agent.py: analyze_macro_with_ai) jest tu
    tym bardziej istotna."""
    sources = [
        ('TradingEconomics_News', 'https://tradingeconomics.com/rss/news.aspx'),
        ('TradingEconomics_Calendar', 'https://tradingeconomics.com/rss/calendar.aspx'),
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
                    pub_date = item.find('pubDate')
                    macro_items.append({
                        'title': title.text if title is not None else '',
                        'description': desc.text if desc is not None else '',
                        'pub_date': pub_date.text if pub_date is not None else '',
                        'source': name,
                    })
            else:
                log_fetch_error(name, url, f"Status HTTP {resp.status_code}", resp.status_code)
        except ET.ParseError as e:
            log_fetch_error(name, url, f"Błąd parsowania XML/RSS: {e}")
        except requests.RequestException as e:
            log_fetch_error(name, url, str(e))
    return macro_items


def fetch_investing_news() -> list:
    """Pobiera najświeższe nagłówki z Investing.com RSS - używane jako
    DODATKOWY kontekst newsowy/makro (nie jako źródło cen - patrz docstring
    modułu)."""
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
    except ET.ParseError as e:
        log_fetch_error('Investing_RSS', url, f"Błąd parsowania XML/RSS: {e}")
    except requests.RequestException as e:
        log_fetch_error('Investing_RSS', url, str(e))
    return news
