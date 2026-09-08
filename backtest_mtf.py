"""
Backtest wielointerwałowy (MTF) - pełna analiza 5-interwałowa jak w agent.py

W przeciwieństwie do backtest.py, który analizuje single-timeframe, ten moduł:
1. Pobiera świece ze wszystkich 5 interwałów (5m, 15m, 1h, 4h, 1d)
2. Wyrównuje je do wspólnego momentu (bez look-ahead bias)
3. Wykonuje pełną analizę wielointerwałową (combine_timeframe_analysis)
4. Generuje sygnały tak jak agent.py (z MTF bonusem)
5. Symuluje trade'y i raportuje statystyki

Użycie:
    from backtest_mtf import run_backtest_mtf_yahoo
    results = run_backtest_mtf_yahoo('APPLE', range_period='2y')
"""

import json
import logging
from collections import defaultdict

import agent as tb
from config import RunMode
from modes import log_backtest_results


logger = logging.getLogger('trading_bot')


def fetch_all_timeframes_yahoo(market_name: str, range_period='2y'):
    """
    Pobiera świece ze wszystkich 5 interwałów z Yahoo Finance.
    Zwraca {tf_name: [bars]} wyrównane czasowo (wspólny period).
    """
    market_info = tb.MARKETS.get(market_name)
    if not market_info:
        raise ValueError(f"Nieznany rynek: {market_name}")
    
    all_data = {}
    
    for tf_name, tf_config in tb.TIMEFRAMES.items():
        print(f"  Pobieram {market_name} interwał {tf_name}...")
        
        # Pobierz surowe dane
        data = tb.get_market_data(
            market_info['symbol'],
            interval=tf_config['interval'],
            range_period=range_period
        )
        
        if not data:
            logger.warning(f"Nie udało się pobrać danych dla {tf_name}")
            continue
        
        # Jeśli to interwał 4h, agreguj z 60m
        if tf_config.get('resample_from_60m'):
            if '60m' not in all_data:
                # Pobierz świeżo
                data_60m = tb.get_market_data(
                    market_info['symbol'],
                    interval='60m',
                    range_period=range_period
                )
            else:
                data_60m = all_data.get('_raw_60m')
            
            if data_60m:
                data = tb.resample_ohlcv(data_60m, tf_config['resample_from_60m'])
        
        # Konwertuj na listę słowników {open, high, low, close, volume}
        bars = []
        if data and data.get('prices'):
            for o, h, l, c, v in zip(
                data.get('opens', []),
                data.get('highs', []),
                data.get('lows', []),
                data.get('prices', []),
                data.get('volumes', [])
            ):
                bars.append({
                    'open': o,
                    'high': h,
                    'low': l,
                    'close': c,
                    'volume': v,
                })
        
        if bars:
            all_data[tf_name] = bars
            logger.info(f"  {tf_name}: pobrano {len(bars)} świec")
    
    return all_data


def _score_at_bar_mtf(all_tf_data: dict, bar_idx_15m: int, lookback_per_tf: dict):
    """
    Liczy pełny scoring wielointerwałowy przy indeksie bar_idx_15m (główny interwał).
    
    Dla każdego TF wyciąga ostatnie N świec (zdefiniowano w lookback_per_tf),
    liczy wskaźniki, a następnie wykonuje pełną analizę MTF.
    
    Zwraca: (long_conf, short_conf, timeframe_indicators, combined_mtf) albo None
    """
    if '15m' not in all_tf_data or bar_idx_15m >= len(all_tf_data['15m']):
        return None
    
    tf_indicators = {}
    
    # Dla każdego interwału wyciągnij okno historii
    for tf_name, bars in all_tf_data.items():
        min_bars = lookback_per_tf.get(tf_name, 50)
        if bar_idx_15m < min_bars or len(bars) == 0:
            tf_indicators[tf_name] = None
            continue
        
        # Mapowanie indeksu 15m -> indeks w danym TF
        # Heurystyka: jeśli 15m ma index i, to w 1h mamy index ~i/4 itp.
        tf_size = tb.TIMEFRAMES.get(tf_name, {}).get('interval', '15m')
        ratio = _get_time_ratio(tf_size)
        tf_idx = max(0, bar_idx_15m // ratio)
        tf_idx = min(tf_idx, len(bars) - 1)
        
        # Wyciągnij okno
        window_start = max(0, tf_idx - min_bars)
        window = bars[window_start:tf_idx + 1]
        
        if len(window) < 10:
            continue
        
        # Konwertuj na format danych
        data = {
            'opens': [b['open'] for b in window],
            'highs': [b['high'] for b in window],
            'lows': [b['low'] for b in window],
            'prices': [b['close'] for b in window],
            'volumes': [b['volume'] for b in window],
        }
        
        ind = tb.calculate_base_indicators(data)
        if ind:
            ind['weight'] = tb.TIMEFRAMES[tf_name]['default_weight']
            ind['trend'] = tb.determine_trend(ind)
            tf_indicators[tf_name] = ind
    
    # Połącz analizę wielointerwałową
    combined = tb.combine_timeframe_analysis(tf_indicators)
    if not combined:
        return None
    
    # Reżim rynku (na podstawie głównego TF 15m)
    bars_15m_window = all_tf_data['15m'][max(0, bar_idx_15m - 60):bar_idx_15m + 1] if bar_idx_15m >= 0 and '15m' in all_tf_data else []
    if bars_15m_window:
        highs = [b['high'] for b in bars_15m_window]
        lows = [b['low'] for b in bars_15m_window]
        closes = [b['close'] for b in bars_15m_window]
        regime, adx_value = tb.detect_market_regime(highs, lows, closes)
    else:
        regime, adx_value = 'UNKNOWN', None
    
    # Licz scoring
    ind_15m = tf_indicators.get('15m')
    if not ind_15m:
        return None
    
    p = ind_15m['price']
    long_score = 0.0
    short_score = 0.0
    
    # Głosy bazowe
    if ind_15m['sma20']:
        long_score += 1 if p > ind_15m['sma20'] else 0
        short_score += 1 if p <= ind_15m['sma20'] else 0
    if ind_15m['sma50']:
        long_score += 1 if p > ind_15m['sma50'] else 0
        short_score += 1 if p <= ind_15m['sma50'] else 0
    long_score += 1 if ind_15m['rsi'] > 50 else 0
    short_score += 1 if ind_15m['rsi'] <= 50 else 0
    if ind_15m['vwap']:
        long_score += 1 if p > ind_15m['vwap'] else 0
        short_score += 1 if p <= ind_15m['vwap'] else 0
    
    # Support/Resistance
    sr_weight = 1.0 if regime == 'RANGE' else 0.5
    if ind_15m['nearest_level'] == 'SUPPORT':
        long_score += sr_weight
    elif ind_15m['nearest_level'] == 'RESISTANCE':
        short_score += sr_weight
    
    # Formacje świecowe
    bias = tb.patterns_directional_bias(ind_15m['candlestick_patterns'])
    if bias == 'bull':
        long_score += 0.5
    elif bias == 'bear':
        short_score += 0.5
    
    # Order Flow
    if ind_15m['order_flow']:
        if ind_15m['order_flow']['delta_percent'] > 0:
            long_score += 0.5
        elif ind_15m['order_flow']['delta_percent'] < 0:
            short_score += 0.5
    
    # Volume Profile
    if ind_15m['volume_profile']:
        poc = ind_15m['volume_profile']['poc']
        long_score += 0.5 if p > poc else 0
        short_score += 0.5 if p <= poc else 0
    
    # MTF bonus (RÓŻNICA vs single-TF backtest)
    mtf_bonus = 2.0 if regime != 'RANGE' else 1.0
    if combined['trend_score'] > 0.6:
        long_score += mtf_bonus
    elif combined['trend_score'] < 0.4:
        short_score += mtf_bonus
    
    # Dywergencje (kara)
    divergences = tb.detect_divergences(tf_indicators)
    if divergences:
        long_score = max(0, long_score - 0.5)
        short_score = max(0, short_score - 0.5)
    
    # Confidence
    long_conf = min(1.0, max(0.0, long_score / tb.TOTAL_SCORE_POINTS))
    short_conf = min(1.0, max(0.0, short_score / tb.TOTAL_SCORE_POINTS))
    
    return long_conf, short_conf, tf_indicators, combined


def _get_time_ratio(interval: str) -> int:
    """Zwraca ile interwałów 15m = 1 bar w tym interwale."""
    ratios = {
        '5m': 3,    # 3x 5m = 15m
        '15m': 1,
        '60m': 4,   # 4x 15m = 1h
        '1d': 96,   # 96x 15m = 1d
    }
    return ratios.get(interval, 1)


def _simulate_trade_mtf(bars, entry_idx, direction, entry, stop_loss, take_profit, max_bars=96):
    """Identyczny do backtest.py - symuluje pojedynczy trade."""
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
    
    # timeout
    last_idx = min(entry_idx + max_bars, len(bars) - 1)
    close_price = bars[last_idx]['close']
    raw = (close_price - entry) if direction == 'LONG' else (entry - close_price)
    return 'timeout', raw / risk_distance


def run_backtest_mtf(all_tf_data: dict, thresholds=(0.5, 0.6, 0.7, 0.8, 0.9),
                      atr_sl_mult=1.5, atr_tp_mult=2.5, max_bars=96, cooldown_bars=5):
    """
    Główny backtest MTF.
    - Głównym interwałem jest 15m
    - Na każdej świecy 15m liczy pełny MTF scoring
    - Symuluje trade'y i raportuje statystyki
    """
    if '15m' not in all_tf_data:
        raise ValueError("Brak danych dla interwału 15m")
    
    bars_15m = all_tf_data['15m']
    lookback_per_tf = {
        '5m': 60,
        '15m': 60,
        '1h': 50,
        '4h': 50,
        '1d': 50,
    }
    
    results = {}
    
    for threshold in thresholds:
        trades = []
        last_trade_idx = -cooldown_bars - 1
        
        for i in range(max(lookback_per_tf.values()), len(bars_15m) - 1):
            if i - last_trade_idx < cooldown_bars:
                continue
            
            scored = _score_at_bar_mtf(all_tf_data, i, lookback_per_tf)
            if not scored:
                continue
            
            long_conf, short_conf, tf_ind, combined = scored
            
            if long_conf < threshold and short_conf < threshold:
                continue
            
            direction = 'LONG' if long_conf >= short_conf else 'SHORT'
            confidence = max(long_conf, short_conf)
            entry = bars_15m[i]['close']
            ind_15m = tf_ind.get('15m')
            if not ind_15m or not ind_15m.get('atr'):
                continue
            
            atr = ind_15m['atr']
            stop_loss = entry - atr_sl_mult * atr if direction == 'LONG' else entry + atr_sl_mult * atr
            take_profit = entry + atr_tp_mult * atr if direction == 'LONG' else entry - atr_tp_mult * atr
            
            outcome, r_multiple = _simulate_trade_mtf(bars_15m, i, direction, entry, stop_loss, take_profit, max_bars)
            if outcome == 'invalid':
                continue
            
            trades.append({
                'index': i,
                'direction': direction,
                'confidence': confidence,
                'outcome': outcome,
                'r_multiple': r_multiple,
            })
            last_trade_idx = i
        
        n = len(trades)
        if n == 0:
            results[threshold] = {
                'n_trades': 0,
                'wins': 0,
                'losses': 0,
                'timeouts': 0,
                'win_rate': 0.0,
                'expectancy_r': 0.0,
                'total_r': 0.0,
            }
            continue
        
        wins = [t for t in trades if t['outcome'] == 'win']
        losses = [t for t in trades if t['outcome'] == 'loss']
        timeouts = [t for t in trades if t['outcome'] == 'timeout']
        win_rate = len(wins) / n
        expectancy = sum(t['r_multiple'] for t in trades) / n
        
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


def print_report_mtf(results: dict):
    """Raport z wyników backtestowania MTF."""
    print(f"\n{'Próg':>6} | {'N':>5} | {'Win%':>6} | {'Wygrane':>8} | {'Przegrane':>10} | {'Timeout':>8} | {'Śr. R':>7} | {'Suma R':>8}")
    print("-" * 90)
    for threshold in sorted(results.keys()):
        r = results[threshold]
        print(f"{threshold:>6.0%} | {r['n_trades']:>5} | {r['win_rate']:>6.1%} | "
              f"{r['wins']:>8} | {r['losses']:>10} | {r['timeouts']:>8} | "
              f"{r['expectancy_r']:>7.2f} | {r['total_r']:>8.2f}")


def run_backtest_mtf_yahoo(market_name: str, range_period='2y',
                            thresholds=(0.5, 0.6, 0.7, 0.8, 0.9)):
    """
    Wrapper - pobiera dane z Yahoo Finance i robi backtest MTF.
    
    Użycie:
        results = run_backtest_mtf_yahoo('APPLE', range_period='2y')
        print_report_mtf(results)
    """
    print(f"\nBacktest MTF dla {market_name} ({range_period})...")
    print("Pobieram dane ze wszystkich 5 interwałów...")
    
    all_tf_data = fetch_all_timeframes_yahoo(market_name, range_period)
    
    if not all_tf_data:
        raise RuntimeError("Nie udało się pobrać żadnych danych")
    
    print(f"\nUruchamiam backtest MTF...")
    results = run_backtest_mtf(all_tf_data, thresholds=thresholds)
    
    print_report_mtf(results)
    
    # Loguj wyniki
    log_backtest_results(results, market_name, range_period)
    
    return results


if __name__ == "__main__":
    print("Backtest MTF na Yahoo Finance.")
    print("Uruchomienie: python backtest_mtf.py")
    print("\nPrzyk​ład użycia:")
    print("  from backtest_mtf import run_backtest_mtf_yahoo")
    print("  results = run_backtest_mtf_yahoo('APPLE', range_period='2y')")
