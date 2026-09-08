"""
Konfiguracja trybów pracy agenta, feature flags i progów kalibracji scoringu.

Tryby:
  - SHADOW:   analiza, logowanie, ZERO wpływu na Telegram/wagi ML/statystyki
              (bezpieczne testowanie zmian na żywych danych)
  - BACKTEST: historyczne dane, symulacja (weryfikacja strategii, backtest_2.py)
  - LIVE:     produkcja - normalne powiadomienia, uczenie wag, adaptacja progu

Ustawienie:
  export AGENT_MODE=shadow    # domyślnie shadow - BEZPIECZNY start po zmianach
  export AGENT_MODE=live      # produkcja

WAŻNE: nazwa "LIVE" odnosi się do TEGO bota tak jak zawsze działał - czyli
wysyłki sygnałów analitycznych na Telegram. Agent nigdy nie składał
rzeczywistych zleceń u brokera (execute_trades to feature flag na przyszłość,
obecnie nie jest wpięty w żadną funkcję składania zleceń - jego jedyny
realny efekt dziś to semantyka "to jest tryb produkcyjny").
"""

import os
from enum import Enum


class RunMode(Enum):
    SHADOW = "shadow"
    BACKTEST = "backtest"
    LIVE = "live"


AGENT_MODE = RunMode(os.environ.get('AGENT_MODE', 'shadow').lower())

# --- Progi sygnałów i kalibracja scoringu (patrz agent.py: AdaptiveThreshold,
# mtf_bonus w analyze_market) - scentralizowane tutaj zamiast rozrzucone po
# stałych w agent.py, żeby zmiana kalibracji nie wymagała grzebania w kodzie. ---
CONFIDENCE_THRESHOLD = float(os.environ.get('CONFIDENCE_THRESHOLD', 0.60))      # obniżone z 0.7
MTF_BULLISH_THRESHOLD = float(os.environ.get('MTF_BULLISH_THRESHOLD', 0.55))    # złagodzone z 0.6
MTF_BEARISH_THRESHOLD = float(os.environ.get('MTF_BEARISH_THRESHOLD', 0.45))    # złagodzone z 0.4
NEAR_MISS_SAMPLE_WEIGHT = float(os.environ.get('NEAR_MISS_SAMPLE_WEIGHT', 0.25))
NEAR_MISS_MIN_AGE_HOURS = float(os.environ.get('NEAR_MISS_MIN_AGE_HOURS', 4))
NEAR_MISS_MAX_AGE_HOURS = float(os.environ.get('NEAR_MISS_MAX_AGE_HOURS', 24))

# Okna czasowe raportów makro (czas Europe/Warsaw): rano / przed otwarciem
# sesji US / po zamknięciu głównych sesji - zgodnie z ustaleniami.
MACRO_HOURS = {
    'morning': (7, 0),
    'pre_us': (14, 30),
    'post_us': (22, 0),
}
DAILY_SUMMARY_HOUR = (21, 50)  # podsumowanie Top 3 "najbliżej progu"

# Feature flags per tryb
FEATURES = {
    RunMode.SHADOW: {
        'fetch_market_data': True,
        'generate_signals': True,
        'execute_trades': False,
        'send_notifications': True,          # wysyłaj powiadomienia (z prefixem 🔍) - żeby dało się ocenić jakość na żywo
        'log_shadow_trades': True,
        'log_near_misses': True,
        'update_learned_weights': False,      # NIE zmieniaj wag ML w trybie shadow
        'record_performance_stats': False,    # NIE zbieraj statystyk win/loss do Kelly
        'update_adaptive_threshold': False,   # NIE adaptuj progu
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


if __name__ == "__main__":
    print(f"Tryb pracy: {AGENT_MODE.value}")
    print("Feature flags:")
    for feature, enabled in FEATURES[AGENT_MODE].items():
        print(f"  {'✅' if enabled else '❌'} {feature}")
