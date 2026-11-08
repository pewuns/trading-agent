# Trading Agent

Automatyczny agent do **analizy rynków i wysyłania sygnałów na Telegram**. Łączy wskaźniki techniczne, analizę kilku interwałów, dane o nastrojach z newsów i makro oraz opcjonalną analizę AI. Projekt jest narzędziem badawczym — nie składa zleceń u brokera i nie gwarantuje wyników.

> **Uwaga:** To nie jest porada inwestycyjna. Sygnały i backtesty mogą być niedokładne, a handel wiąże się z ryzykiem utraty kapitału. Zawsze samodzielnie weryfikuj dane i decyzje.

## Funkcje

- Analiza indeksów, par walutowych, surowców, kryptowalut i akcji.
- Wskaźniki techniczne, m.in. SMA, RSI, VWAP, ATR, ADX, poziomy wsparcia i oporu, formacje świecowe, order flow oraz volume profile.
- Analiza trendu na interwałach 5m, 15m, 1h, 4h i 1d, filtrowanie sygnałów m.in. według spreadu, zmienności, płynności i sesji rynku.
- Sygnały LONG/SHORT z poziomami wejścia, SL i TP oraz szacowanym ryzykiem pozycji.
- Opcjonalna analiza newsów i danych makro przez Groq oraz analiza wykresów przez Groq Vision.
- Zapisywanie historii sygnałów i wyników, adaptacja progu i wag w trybie `live`, circuit breaker oraz ograniczanie skorelowanych sygnałów.
- Osobny model sygnałów (Kalman/Supertrend) działający jako model-cień do porównania — jego wynik nie steruje sygnałami produkcyjnymi.
- Skrypty do backtestów jedno- i wielointerwałowych.

## Tryby pracy

Tryb wybiera zmienna `AGENT_MODE`; domyślnie jest to `shadow`.

| Tryb | Działanie |
|---|---|
| `shadow` | Pobiera dane, analizuje rynek i może wysyłać na Telegram powiadomienia z oznaczeniem SHADOW. Zapisuje obserwacje, ale nie aktualizuje wag, statystyk wyników ani adaptacyjnego progu. |
| `live` | Wysyła normalne powiadomienia i włącza aktualizację wag, statystyk oraz progu na podstawie zapisanych wyników. **Nie składa zleceń u brokera** — funkcja wykonania transakcji nie jest zaimplementowana. |
| `backtest` | Tryb zarezerwowany dla logiki testowej; `python agent.py` w tym trybie celowo kończy działanie. Do backtestów używaj osobnych modułów. |

Przed użyciem `live` przetestuj konfigurację w `shadow`. Tryb `shadow` nadal może wysłać wiadomości na Telegram, jeśli skonfigurujesz dane Telegrama.

## Obsługiwane rynki

**Indeksy:** DAX, S&P 500, NASDAQ

**Forex:** EUR/USD, USD/JPY, GBP/USD, USD/CHF, AUD/USD, USD/CAD

**Surowce:** GOLD, OIL WTI

**Kryptowaluty:** BITCOIN, ETHEREUM, SOLANA

**Akcje:** APPLE, MICROSOFT, NVIDIA, TESLA, AMAZON, META, GOOGLE

## Źródła danych i integracje

- **Ceny OHLCV:** Yahoo Finance jest źródłem dla indeksów, surowców i akcji; dla forexu głównym źródłem jest Twelve Data z fallbackiem do Yahoo Finance; dla kryptowalut głównym źródłem jest CryptoCompare z fallbackiem do Yahoo Finance.
- **Spread:** opcjonalne dane bid/ask pobierane z XTB. Bez nich agent stosuje przybliżenie oparte na zmienności. XTB nie dostarcza głównych cen i nie jest używany do składania zleceń.
- **Newsy i makro:** zapytania newsowe oraz kanały RSS Investing.com i Trading Economics.
- **AI:** Groq dla analizy tekstu i — gdy są dostępne wymagane biblioteki — wykresów.
- **Powiadomienia:** Telegram Bot API.

Brak opcjonalnego klucza API może ograniczyć daną funkcję lub uruchomić fallback. Samo pobieranie podstawowych cen z Yahoo Finance nie wymaga klucza API.

## Wymagania

- Python 3.11 (wersja używana w GitHub Actions).
- Dostęp do Internetu.
- Opcjonalnie: klucze API i dane dostępowe wymienione niżej.

## Instalacja i uruchomienie lokalne

```bash
git clone https://github.com/pewuns/trading-agent.git
cd trading-agent

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

W PowerShell aktywuj środowisko poleceniem:

```powershell
.\.venv\Scripts\Activate.ps1
```

Ustaw potrzebne zmienne środowiskowe, a następnie uruchom pojedynczy cykl:

```bash
export AGENT_MODE=shadow
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python agent.py
```

Agent wykonuje **jeden cykl i kończy działanie**. Nie uruchamia własnej pętli ani harmonogramu; kolejne cykle wyzwala GitHub Actions albo zewnętrzny scheduler. Bez danych Telegrama analiza może działać, ale powiadomienia nie zostaną dostarczone.

## Konfiguracja

Sekrety i ustawienia przekazuj przez zmienne środowiskowe. Nie wpisuj kluczy ani haseł do kodu, README ani plików śledzonych przez Git.

| Zmienna | Wymagana | Domyślna | Zastosowanie |
|---|---:|---:|---|
| `AGENT_MODE` | Nie | `shadow` | `shadow`, `live` albo `backtest`. |
| `TELEGRAM_TOKEN` | Nie | — | Token bota Telegram. |
| `TELEGRAM_CHAT_ID` | Nie | — | Odbiorca powiadomień Telegram. |
| `GROQ_API_KEY` | Nie | — | Analiza newsów, makro i wykresów przez Groq. |
| `TWELVEDATA_API_KEY` | Nie | — | Główne źródło danych dla forexu; bez niego używany jest fallback. |
| `CRYPTOCOMPARE_API_KEY` | Nie | — | Dostęp do danych CryptoCompare; w razie niepowodzenia kod próbuje Yahoo Finance. |
| `XTB_LOGIN` / `XTB_PASSWORD` | Nie | — | Logowanie XTB wyłącznie po spread i/lub dane historyczne do backtestu. |
| `XTB_ACCOUNT_TYPE` | Nie | `demo` | Typ połączenia XTB: `demo` lub `real`; nie włącza handlu. |
| `ACCOUNT_SIZE` | Nie | `10000` | Kapitał używany do obliczeń wielkości pozycji. |
| `RISK_PER_TRADE_PERCENT` | Nie | `1.0` | Bazowy procent ryzyka pozycji. |
| `MAX_RISK_PERCENT_CAP` | Nie | `2.0` | Górny limit ryzyka dla obliczeń Kelly'ego. |
| `MAX_PER_CLUSTER` | Nie | `2` | Maksymalna liczba sygnałów z jednego klastra korelacji. |
| `SIGNAL_TIMEOUT_HOURS` | Nie | `48` | Limit czasu dla monitorowanych sygnałów. |
| `CONFIDENCE_THRESHOLD` | Nie | `0.60` | Bazowy próg pewności. |

Wartości progów MTF i uczenia są konfigurowane w `config.py`; wśród ustawień środowiskowych znajdują się m.in. `MTF_BULLISH_THRESHOLD`, `MTF_BEARISH_THRESHOLD`, `NEAR_MISS_SAMPLE_WEIGHT`, `NEAR_MISS_MIN_AGE_HOURS` i `NEAR_MISS_MAX_AGE_HOURS`. Dodatkowe limity ryzyka, circuit breakera i minimalne liczby próbek są ustawiane w `agent.py`.

## GitHub Actions

Workflow [`.github/workflows/agent.yml`](.github/workflows/agent.yml) uruchamia `agent.py` co 15 minut w dni robocze, w szerokim oknie 04:00–21:59 UTC. Kod dodatkowo kontroluje okno handlu 06:00–22:00 w strefie `Europe/Warsaw`. Możliwe jest też uruchomienie ręczne (`workflow_dispatch`) z wyborem trybu `shadow` lub `live`.

W ustawieniach repozytorium dodaj wymagane dla wybranych funkcji sekrety Actions: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `GROQ_API_KEY`, `TWELVEDATA_API_KEY`; opcjonalnie także `XTB_LOGIN` i `XTB_PASSWORD`. Klucz `CRYPTOCOMPARE_API_KEY` jest odczytywany przez kod, ale obecnie nie jest przekazywany przez workflow — dodaj go do sekcji `env` workflow, jeśli ma być używany w Actions.

Workflow zapisuje zmiany danych po każdym uruchomieniu (`git add -A` i commit). Oznacza to, że może wersjonować również wygenerowane pliki stanu i cache. Przed zmianą tego zachowania sprawdź, które pliki runtime powinny być przechowywane między uruchomieniami — agent korzysta z lokalnych plików JSON/JSONL jako pamięci.

## Backtesty i testowanie

- `backtest_mtf.py` udostępnia `run_backtest_mtf_yahoo(market_name, range_period='2y')`, który pobiera dane z Yahoo Finance i wykonuje backtest MTF. Funkcja wymaga połączenia z Internetem; wyniki zapisuje do `backtest_results.json`.
- `signal_engine.py` zawiera niezależny model-cień oraz funkcje generowania sygnałów i symulacji.
- `test_xtb_backtest.py` jest ręcznym skryptem integracyjnym wymagającym danych logowania XTB i nie jest automatycznym zestawem testów.

**Znane ograniczenie:** `backtest.py` importuje moduł `trading_bot`, którego nie ma w repozytorium (główna implementacja znajduje się w `agent.py`). W związku z tym `backtest.py` i oparty na nim `test_xtb_backtest.py` wymagają naprawienia importu przed uruchomieniem. Backtest MTF jest osobnym modułem i importuje `agent.py`.

Backtesty są symulacją historyczną, a nie dowodem przyszłej skuteczności. Zwróć uwagę na ograniczenia źródła danych, synchronizacji interwałów, kosztów transakcyjnych i założeń symulacji. Dane syntetyczne służą wyłącznie do sprawdzenia mechaniki kodu.

## Struktura projektu

| Plik | Rola |
|---|---|
| `agent.py` | Główna logika: źródła danych, wskaźniki, scoring, filtry, raporty i cykl agenta. |
| `config.py` | Tryby pracy, progi i harmonogramy. |
| `modes.py` | Feature flags trybów oraz logowanie sygnałów i wyników. |
| `data_sources.py` | Kanały RSS Trading Economics i Investing.com. |
| `investing_pair_id.py` | Pomocnicze wyszukiwanie identyfikatorów instrumentów na Investing.com. |
| `signal_engine.py` | Niezależny model-cień Kalman/Supertrend i jego backtest. |
| `backtest.py` | Uproszczony backtest pojedynczego interwału; obecnie ma opisany wyżej nieaktualny import. |
| `backtest_mtf.py` | Backtest wielointerwałowy na danych Yahoo Finance. |
| `requirements.txt` | Zależności Pythona. |
| `.github/workflows/agent.yml` | Harmonogram i ręczne uruchamianie agenta w GitHub Actions. |

Pliki JSON i JSONL w katalogu głównym przechowują m.in. historię sygnałów, cache danych, pamięć agenta, wagi modelu, wyniki i logi. To stan runtime, od którego zależą niektóre funkcje uczenia i monitorowania.

## Licencja

W repozytorium nie ma obecnie pliku licencji. Nie zakładaj, że kod może być używany, rozpowszechniany lub modyfikowany na dowolnych warunkach.
