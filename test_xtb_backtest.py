"""
Test backtestu na prawdziwych danych z XTB.

Użycie:
    1. Ustaw zmienne środowiskowe XTB_LOGIN i XTB_PASSWORD (i opcjonalnie XTB_ACCOUNT_TYPE)
    2. Uruchom: python test_xtb_backtest.py

Przykład (Linux/Mac):
    export XTB_LOGIN='twoj_login'
    export XTB_PASSWORD='twoje_haslo'
    export XTB_ACCOUNT_TYPE='demo'
    python test_xtb_backtest.py

Przykład (Windows CMD):
    set XTB_LOGIN=twoj_login
    set XTB_PASSWORD=twoje_haslo
    set XTB_ACCOUNT_TYPE=demo
    python test_xtb_backtest.py
"""

from backtest import fetch_and_backtest_xtb

# ============================================
# KONFIGURACJA TESTÓW
# ============================================

# Lista rynków do przetestowania
# Możesz dodawać/usuwać rynki z tej listy
TEST_MARKETS = [
    # (nazwa rynku, interwał, miesiące wstecz)
    ('GOLD', 'H1', 12),
    ('EUR/USD', 'H1', 12),
    ('BITCOIN', 'H1', 12),
    # ('APPLE', 'H1', 12),
    # ('DAX', 'H1', 12),
    # ('S&P500', 'H1', 12),
    # ('NASDAQ', 'H1', 12),
]

# Progi pewności do przetestowania
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)

# Parametry symulacji trade'a
ATR_SL_MULT = 1.5  # Stop Loss = 1.5 * ATR
ATR_TP_MULT = 2.5  # Take Profit = 2.5 * ATR
MAX_BARS = 96      # Maksymalny czas trwania trade'a (96 świec)
COOLDOWN_BARS = 5  # Cooldown między sygnałami (5 świec)


def main():
    """Główna funkcja testowa."""
    print("=" * 80)
    print("BACKTEST NA DANYCH Z XTB")
    print("=" * 80)
    print()
    
    # Sprawdź czy zmienne środowiskowe są ustawione
    import os
    if not os.environ.get('XTB_LOGIN') or not os.environ.get('XTB_PASSWORD'):
        print("⚠️  OSTRZEŻENIE: XTB_LOGIN lub XTB_PASSWORD nie są ustawione!")
        print()
        print("Ustaw je przed uruchomieniem:")
        print("  Linux/Mac:")
        print("    export XTB_LOGIN='twoj_login'")
        print("    export XTB_PASSWORD='twoje_haslo'")
        print("    export XTB_ACCOUNT_TYPE='demo'")
        print()
        print("  Windows CMD:")
        print("    set XTB_LOGIN=twoj_login")
        print("    set XTB_PASSWORD=twoje_haslo")
        print("    set XTB_ACCOUNT_TYPE=demo")
        print()
        print("Kontynuuję, ale backtest prawdopodobnie się nie powiedzie...")
        print()
    
    for market_name, period, months_back in TEST_MARKETS:
        print("=" * 80)
        print(f"BACKTEST: {market_name} | Interwał: {period} | Okres: {months_back} mies.")
        print("=" * 80)
        print()
        
        try:
            results, bars = fetch_and_backtest_xtb(
                market_name,
                period=period,
                months_back=months_back,
                thresholds=THRESHOLDS,
                atr_sl_mult=ATR_SL_MULT,
                atr_tp_mult=ATR_TP_MULT,
                max_bars=MAX_BARS,
                cooldown_bars=COOLDOWN_BARS,
            )
            
            # Zapisz surowe dane do pliku (opcjonalnie)
            import json
            output_file = f"backtest_results_{market_name.replace('/', '_')}.json"
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'market': market_name,
                    'period': period,
                    'months_back': months_back,
                    'results': results,
                    'n_bars': len(bars),
                }, f, indent=2, ensure_ascii=False)
            print(f"\n✅ Wyniki zapisane do: {output_file}")
            
        except Exception as e:
            print(f"❌ Błąd podczas testu {market_name}: {e}")
        
        print("\n\n")


if __name__ == "__main__":
    main()