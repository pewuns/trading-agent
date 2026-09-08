"""
Konfiguracja trybów pracy agenta i feature flags.

Tryby:
  - SHADOW:   analiza, logowanie, brak transakcji (testowanie live)
  - BACKTEST: historyczne dane, symulacja (weryfikacja strategii)
  - LIVE:     rzeczywiste transakcje (produkcja)

Ustawienie:
  export AGENT_MODE=shadow    # domyślnie shadow
  export AGENT_MODE=live      # produkcja
"""

import os
from enum import Enum


class RunMode(Enum):
    """Tryby pracy agenta."""
    SHADOW = "shadow"       # analiza bez transakcji
    BACKTEST = "backtest"   # symulacja na historii
    LIVE = "live"           # rzeczywiste transakcje


# Główny tryb pracy
AGENT_MODE = RunMode(os.environ.get('AGENT_MODE', 'shadow').lower())

# Feature flags per tryb
FEATURES = {
    RunMode.SHADOW: {
        'fetch_market_data': True,          # pobieraj dane
        'generate_signals': True,           # analizuj i generuj sygnały
        'execute_trades': False,            # ale NIE otwieraj transakcji
        'send_notifications': True,         # wysyłaj powiadomienia (z prefixem 🔍)
        'log_shadow_trades': True,          # loguj "co by się stało"
        'update_learned_weights': False,    # nie zmieniaj wag ML
        'record_performance_stats': False,  # nie aktualizuj historii win/loss
        'update_adaptive_threshold': False, # nie adaptuj progi pewności
    },
    RunMode.LIVE: {
        'fetch_market_data': True,
        'generate_signals': True,
        'execute_trades': True,             # OTWIERAJ TRANSAKCJE
        'send_notifications': True,         # normalne powiadomienia
        'log_shadow_trades': True,          # nadal monitoruj vs baseline
        'update_learned_weights': True,     # ZMIENIAJ WAGI
        'record_performance_stats': True,   # ZBIERAJ STATYSTYKI
        'update_adaptive_threshold': True,  # ADAPTUJ PROGI
    },
    RunMode.BACKTEST: {
        'fetch_market_data': False,         # dane z backtestu
        'generate_signals': True,           # testuj sygnały
        'execute_trades': False,            # symuluj
        'send_notifications': False,        # bez powiadomień
        'log_shadow_trades': False,         # niepotrzebne w backteście
        'update_learned_weights': False,    # nie zmieniaj
        'record_performance_stats': False,  # zbieraj tylko do raportu
        'update_adaptive_threshold': False,
    },
}


def is_mode(target_mode: RunMode) -> bool:
    """Sprawdź czy agent pracuje w danym trybie."""
    return AGENT_MODE == target_mode


def is_feature_enabled(feature_name: str) -> bool:
    """Sprawdź czy feature jest włączony w bieżącym trybie."""
    return FEATURES[AGENT_MODE].get(feature_name, False)


def get_mode_prefix() -> str:
    """Zwraca prefix do powiadomień zależny od trybu."""
    if AGENT_MODE == RunMode.SHADOW:
        return "🔍 SHADOW: "
    elif AGENT_MODE == RunMode.BACKTEST:
        return "📊 BACKTEST: "
    else:
        return ""  # live – bez prefiksu


if __name__ == "__main__":
    print(f"Tryb pracy: {AGENT_MODE.value}")
    print(f"Feature flags:")
    for feature, enabled in FEATURES[AGENT_MODE].items():
        status = "✅" if enabled else "❌"
        print(f"  {status} {feature}")
