"""
Helpery do logowania i obsługi trybów pracy agenta.
Integruje się z config.py.
"""

import json
from datetime import datetime
from pathlib import Path
import logging

import pytz
from config import AGENT_MODE, RunMode, is_feature_enabled, get_mode_prefix


logger = logging.getLogger('trading_bot')

SHADOW_TRADES_LOG = 'shadow_trades.jsonl'     # sygnały które by zostały otwarte w LIVE
LIVE_TRADES_LOG = 'live_trades.jsonl'         # rzeczywiste transakcje
BACKTEST_RESULTS_LOG = 'backtest_results.json'


def log_signal_per_mode(signal: dict, mode: RunMode = None):
    """
    Loguje sygnał w zależności od trybu.
    - SHADOW: zapisz do shadow_trades.jsonl
    - LIVE: zapisz do live_trades.jsonl
    - BACKTEST: nie loguj (backtest_2.py robi to osobno)
    """
    if mode is None:
        mode = AGENT_MODE

    if mode == RunMode.SHADOW:
        _log_to_file(SHADOW_TRADES_LOG, signal)
        logger.info(f"[SHADOW] Zarejestrowano sygnał: {signal['name']} {signal['direction']}")
    elif mode == RunMode.LIVE:
        _log_to_file(LIVE_TRADES_LOG, signal)
        logger.info(f"[LIVE] Wysłano sygnał: {signal['name']} {signal['direction']}")


def log_trade_outcome(signal_key: str, outcome: str, r_multiple: float, mode: RunMode = None):
    """Loguje wynik zamkniętego sygnału."""
    if mode is None:
        mode = AGENT_MODE

    record = {
        'timestamp': datetime.now(pytz.utc).isoformat(),
        'signal_key': signal_key,
        'outcome': outcome,  # 'win', 'loss', 'timeout'
        'r_multiple': r_multiple,
        'mode': mode.value,
    }

    if mode == RunMode.LIVE:
        _log_to_file(LIVE_TRADES_LOG, record)
    elif mode == RunMode.SHADOW:
        _log_to_file(SHADOW_TRADES_LOG, record)


def log_backtest_results(results: dict, market_name: str, period: str):
    """Loguje wyniki backtestowania do JSON."""
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
            json.dump(history[-100:], f, indent=2)  # trzymaj ostatnie 100 backtestów
    except Exception as e:
        logger.error(f"Błąd zapisu {BACKTEST_RESULTS_LOG}: {e}")


def _log_to_file(filepath: str, record: dict):
    """Zapisuje rekord do JSONL (jeden JSON per linia)."""
    try:
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.error(f"Błąd zapisu do {filepath}: {e}")


def get_notification_message(base_message: str) -> str:
    """Dodaje prefix do powiadomień zależny od trybu."""
    return get_mode_prefix() + base_message


def should_update_weights() -> bool:
    return is_feature_enabled('update_learned_weights')


def should_record_stats() -> bool:
    return is_feature_enabled('record_performance_stats')


def should_update_threshold() -> bool:
    return is_feature_enabled('update_adaptive_threshold')


def should_send_notifications() -> bool:
    return is_feature_enabled('send_notifications')


if __name__ == "__main__":
    print(f"Tryb: {AGENT_MODE.value}")
    print(f"Update weights: {should_update_weights()}")
    print(f"Record stats: {should_record_stats()}")
