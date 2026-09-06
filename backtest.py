"""
Backtest silnika scoringu z trading_bot.py na danych historycznych.

WAŻNE OGRANICZENIE: to jest backtest UPROSZCZONY - liczy scoring na
POJEDYNCZYM interwale (świece przekazane w danych wejściowych), a nie pełną
analizę wielointerwałową (5m/15m/1h/4h/1d) jak w analyze_market(). Odtworzenie
prawdziwej analizy MTF w backteście wymagałoby zsynchronizowanych danych
historycznych dla wszystkich pięciu interwałów naraz, co jest dużo trudniejsze
do poprawnego zestawienia bez przecieku danych z przyszłości (look-ahead bias).

Ten skrypt służy do:
1) sprawdzenia, czy scoring oparty o SMA/RSI/VWAP/S-R/formacje/order-flow/POC
   ma jakąkolwiek przewagę statystyczną na danych historycznych,
2) skalibrowania progu pewności i porównania wariantów przed wdrożeniem,
3) jako podstawa do rozbudowy o pełny multi-timeframe backtest, jeśli po tym
   pierwszym etapie coś w ogóle wygląda obiecująco.

Użycie:
    from backtest import run_backtest, generate_synthetic_data
    bars = generate_synthetic_data(500)  # albo własne dane OHLCV
    results = run_backtest(bars)
    print(results)

Dane wejściowe (bars): lista słowników {'open','high','low','close','volume'}
w kolejności chronologicznej.
"""
import random
import numpy as np

import trading_bot as tb


def generate_synthetic_data(n=500, seed=7, drift=0.0, volatility=0.5):
    """Generuje syntetyczne dane OHLCV (random walk z driftem) WYŁĄCZNIE do
    self-testu tego skryptu. Wyniki na danych syntetycznych NIE mówią nic
    o realnej skuteczności strategii - służą tylko do sprawdzenia, że kod
    backtestu działa mechanicznie poprawnie. Do prawdziwej oceny strategii
    użyj rzeczywistych danych historycznych (np. z Yahoo Finance/XTB)."""
    rnd = random.Random(seed)
    price = 100.0
    bars = []
    for _ in range(n):
        change = rnd.uniform(-volatility, volatility) + drift
        open_p = price
        close_p = max(0.01, price + change)
        high_p = max(open_p, close_p) + rnd.uniform(0, volatility * 0.5)
        low_p = min(open_p, close_p) - rnd.uniform(0, volatility * 0.5)
        volume = rnd.randint(800000, 1200000)
        bars.append({'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p, 'volume': volume})
        price = close_p
    return bars


def _score_at_index(bars, i, lookback=60):
    """Liczy scoring (analogiczny do bazowej części analyze_market, BEZ
    newsów i BEZ prawdziwego MTF - patrz zastrzeżenie w docstringu modułu)
    na oknie [i-lookback, i]. Zwraca (long_conf, short_conf, ind) albo None."""
    if i < lookback:
        return None
    window = bars[i - lookback:i + 1]
    data = {
        'prices': [b['close'] for b in window],
        'highs': [b['high'] for b in window],
        'lows': [b['low'] for b in window],
        'opens': [b['open'] for b in window],
        'volumes': [b['volume'] for b in window],
    }
    ind = tb.calculate_full_indicators(data)
    if not ind:
        return None

    p = ind['price']
    long_score = 0.0
    short_score = 0.0

    if ind['sma20']:
        long_score += 1 if p > ind['sma20'] else 0
        short_score += 1 if p <= ind['sma20'] else 0
    if ind['sma50']:
        long_score += 1 if p > ind['sma50'] else 0
        short_score += 1 if p <= ind['sma50'] else 0
    long_score += 1 if ind['rsi'] > 50 else 0
    short_score += 1 if ind['rsi'] <= 50 else 0
    if ind['vwap']:
        long_score += 1 if p > ind['vwap'] else 0
        short_score += 1 if p <= ind['vwap'] else 0

    if ind['nearest_level'] == 'SUPPORT':
        long_score += 0.5
    elif ind['nearest_level'] == 'RESISTANCE':
        short_score += 0.5

    bias = tb.patterns_directional_bias(ind['candlestick_patterns'])
    if bias == 'bull':
        long_score += 0.5
    elif bias == 'bear':
        short_score += 0.5

    if ind['order_flow']:
        if ind['order_flow']['delta_percent'] > 0:
            long_score += 0.5
        elif ind['order_flow']['delta_percent'] < 0:
            short_score += 0.5

    if ind['volume_profile']:
        if p > ind['volume_profile']['poc']:
            long_score += 0.5
        else:
            short_score += 0.5

    # Bez MTF-bonusu (+2 w produkcyjnym kodzie) - w tym uproszczonym
    # backteście nie mamy prawdziwych wyższych interwałów, więc pomijamy
    # tę część zamiast ją fałszować. Total punktów odpowiednio niższy.
    total_points = 6.0
    long_conf = min(1.0, max(0.0, long_score / total_points))
    short_conf = min(1.0, max(0.0, short_score / total_points))
    return long_conf, short_conf, ind


def _simulate_trade(bars, entry_idx, direction, entry, stop_loss, take_profit, max_bars=96):
    """Symuluje pojedynczy trade krok po kroku od entry_idx+1, aż trafi TP,
    SL, albo upłynie max_bars świec (timeout). Zwraca (outcome, r_multiple)."""
    risk_distance = abs(entry - stop_loss)
    if risk_distance == 0:
        return 'invalid', 0.0

    for j in range(entry_idx + 1, min(entry_idx + 1 + max_bars, len(bars))):
        bar = bars[j]
        if direction == 'LONG':
            if bar['low'] <= stop_loss:
                return 'loss', (stop_loss - entry) / risk_distance
            if bar['high'] >= take_profit:
                return 'win', (take_profit - entry) / risk_distance
        else:
            if bar['high'] >= stop_loss:
                return 'loss', (entry - stop_loss) / risk_distance
            if bar['low'] <= take_profit:
                return 'win', (entry - take_profit) / risk_distance

    # timeout - zamknij po cenie z ostatniej dostępnej świecy w oknie
    last_idx = min(entry_idx + max_bars, len(bars) - 1)
    close_price = bars[last_idx]['close']
    raw = (close_price - entry) if direction == 'LONG' else (entry - close_price)
    return 'timeout', raw / risk_distance


def run_backtest(bars, thresholds=(0.5, 0.6, 0.7, 0.8, 0.9), lookback=60,
                  atr_sl_mult=1.5, atr_tp_mult=2.5, max_bars=96, cooldown_bars=5):
    """Przechodzi po danych historycznych bar po bar, generuje sygnały wg
    _score_at_index, symuluje wynik i agreguje statystyki DLA KAŻDEGO progu
    z `thresholds` osobno (żeby zobaczyć, jak win-rate/expectancy zmieniają
    się wraz z progiem pewności).

    cooldown_bars: po otwarciu sygnału, ile świec pomijamy zanim znów
    rozważymy nowy sygnał na tym samym oknie (żeby nie liczyć dziesiątek
    prawie identycznych, nakładających się sygnałów jako niezależnych prób)."""
    results = {}
    for threshold in thresholds:
        trades = []
        last_trade_idx = -cooldown_bars - 1
        for i in range(lookback, len(bars) - 1):
            if i - last_trade_idx < cooldown_bars:
                continue
            scored = _score_at_index(bars, i, lookback)
            if not scored:
                continue
            long_conf, short_conf, ind = scored
            if long_conf < threshold and short_conf < threshold:
                continue

            direction = 'LONG' if long_conf >= short_conf else 'SHORT'
            confidence = max(long_conf, short_conf)
            entry = ind['price']
            atr = ind['atr'] if ind['atr'] else 0
            if atr == 0:
                continue
            stop_loss = entry - atr_sl_mult * atr if direction == 'LONG' else entry + atr_sl_mult * atr
            take_profit = entry + atr_tp_mult * atr if direction == 'LONG' else entry - atr_tp_mult * atr

            outcome, r_multiple = _simulate_trade(bars, i, direction, entry, stop_loss, take_profit, max_bars)
            if outcome == 'invalid':
                continue
            trades.append({'index': i, 'direction': direction, 'confidence': confidence,
                            'outcome': outcome, 'r_multiple': r_multiple})
            last_trade_idx = i

        n = len(trades)
        wins = [t for t in trades if t['outcome'] == 'win']
        losses = [t for t in trades if t['outcome'] == 'loss']
        timeouts = [t for t in trades if t['outcome'] == 'timeout']
        win_rate = len(wins) / n if n else 0.0
        expectancy = sum(t['r_multiple'] for t in trades) / n if n else 0.0

        results[threshold] = {
            'n_trades': n,
            'wins': len(wins),
            'losses': len(losses),
            'timeouts': len(timeouts),
            'win_rate': win_rate,
            'expectancy_r': expectancy,
            'total_r': sum(t['r_multiple'] for t in trades),
        }
    return results


def print_report(results):
    print(f"{'Próg':>6} | {'N':>5} | {'Win%':>6} | {'Wygrane':>8} | {'Przegrane':>10} | {'Timeout':>8} | {'Śr. R':>7} | {'Suma R':>8}")
    print("-" * 80)
    for threshold, r in sorted(results.items()):
        print(f"{threshold:>6.0%} | {r['n_trades']:>5} | {r['win_rate']:>6.1%} | "
              f"{r['wins']:>8} | {r['losses']:>10} | {r['timeouts']:>8} | "
              f"{r['expectancy_r']:>7.2f} | {r['total_r']:>8.2f}")


def fetch_and_backtest_xtb(market_name, period='H1', months_back=12,
                            thresholds=(0.5, 0.6, 0.7, 0.8, 0.9), **run_backtest_kwargs):
    """
    Pobiera REALNĄ historię świec z XTB dla rynku o nazwie `market_name`
    (klucz z trading_bot.MARKETS, np. 'APPLE', 'EUR/USD', 'BITCOIN') i
    automatycznie odpala na niej run_backtest().

    Wymaga ustawionych zmiennych środowiskowych XTB_LOGIN i XTB_PASSWORD
    (patrz trading_bot.py - domyślnie konto DEMO).

    UWAGA: konwersja świec XTB (open + offset punktowy -> cena bezwzględna)
    w trading_bot.XTBClient.get_chart_history() nie została zweryfikowana na
    żywym połączeniu - zanim zaufasz wynikom, sprawdź na kilku pierwszych
    świecach z `bars`, że ceny wyglądają realistycznie (np. porównaj z
    wykresem w aplikacji XTB dla tego samego okresu).

    Zwraca (results, bars) - `bars` przydaje się do ręcznej weryfikacji.
    """
    xtb_symbol = tb.XTB_SYMBOLS.get(market_name)
    if not xtb_symbol:
        raise ValueError(
            f"Brak mapowania XTB dla '{market_name}'. Dostępne rynki: {list(tb.XTB_SYMBOLS.keys())}"
        )

    print(f"Pobieram historię {market_name} ({xtb_symbol}) z XTB, interwał {period}, ~{months_back} mies. wstecz...")
    bars = tb.fetch_xtb_historical(xtb_symbol, period=period, months_back=months_back)
    if not bars:
        raise RuntimeError(
            "Nie udało się pobrać danych z XTB. Sprawdź: (1) czy XTB_LOGIN/XTB_PASSWORD są "
            "ustawione, (2) połączenie sieciowe, (3) czy symbol istnieje na Twoim koncie "
            "(sprawdź w xStation5 -> Narzędzia -> Specyfikacja instrumentów), (4) czy "
            "months_back nie przekracza dostępnej głębokości historii dla tego interwału."
        )

    print(f"Pobrano {len(bars)} świec"
          f" (od {bars[0].get('timestamp', '?')} do {bars[-1].get('timestamp', '?')}).")
    print("Pierwsze 3 świece (zweryfikuj ręcznie, że wyglądają realistycznie):")
    for b in bars[:3]:
        print(f"  O={b['open']:.5f} H={b['high']:.5f} L={b['low']:.5f} C={b['close']:.5f} V={b['volume']}")

    print("\nUruchamiam backtest...")
    results = run_backtest(bars, thresholds=thresholds, **run_backtest_kwargs)
    print_report(results)
    return results, bars


def fetch_and_backtest_yahoo(market_name, interval='60m', range_period='2y',
                              thresholds=(0.5, 0.6, 0.7, 0.8, 0.9), **run_backtest_kwargs):
    """Odpowiednik fetch_and_backtest_xtb, ale przez Yahoo Finance
    (get_market_data z trading_bot.py) - przydatne do porównania z wynikami
    z XTB, albo gdy nie masz jeszcze skonfigurowanego konta XTB."""
    market_info = tb.MARKETS.get(market_name)
    if not market_info:
        raise ValueError(f"Nieznany rynek '{market_name}'. Dostępne: {list(tb.MARKETS.keys())}")

    print(f"Pobieram historię {market_name} z Yahoo Finance, interwał {interval}, zakres {range_period}...")
    data = tb.get_market_data(market_info['symbol'], interval=interval, range_period=range_period)
    if not data:
        raise RuntimeError("Nie udało się pobrać danych z Yahoo Finance.")

    bars = [
        {'open': o, 'high': h, 'low': l, 'close': c, 'volume': v}
        for o, h, l, c, v in zip(data['opens'], data['highs'], data['lows'], data['prices'], data['volumes'])
    ]
    print(f"Pobrano {len(bars)} świec. Uruchamiam backtest...")
    results = run_backtest(bars, thresholds=thresholds, **run_backtest_kwargs)
    print_report(results)
    return results, bars


if __name__ == "__main__":
    print("Uruchamiam self-test na danych SYNTETYCZNYCH (tylko sprawdzenie mechaniki backtestu).")
    print("Wyniki na danych syntetycznych NIC nie mówią o realnej skuteczności strategii!\n")
    bars = generate_synthetic_data(n=800, seed=7)
    results = run_backtest(bars)
    print_report(results)
    print(
        "\nAby przetestować na prawdziwych danych:\n"
        "  Z XTB (wymaga XTB_LOGIN/XTB_PASSWORD w zmiennych środowiskowych):\n"
        "    from backtest import fetch_and_backtest_xtb\n"
        "    results, bars = fetch_and_backtest_xtb('APPLE', period='H1', months_back=12)\n\n"
        "  Z Yahoo Finance (nie wymaga logowania):\n"
        "    from backtest import fetch_and_backtest_yahoo\n"
        "    results, bars = fetch_and_backtest_yahoo('APPLE', interval='60m', range_period='2y')"
    )
