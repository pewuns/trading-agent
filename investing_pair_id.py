"""
Automatyczne wyszukiwanie pairId na Investing.com (scraping strony
wyszukiwania + strony instrumentu) - do samo-uzupełniania brakujących ID
w INVESTING_PAIR_IDS bez ręcznego szukania w devtools przeglądarki.

Bazuje na module dostarczonym przez użytkownika, z drobnymi poprawkami:
- timeout i raise_for_status na requestach (żeby cicho nie dostać pustej/
  błędnej strony i nie próbować parsować śmieci),
- dłuższy, bardziej realistyczny User-Agent,
- get_pair_id() zwraca też URL instrumentu (przydatne w logach/ewentualnej
  ręcznej weryfikacji),
- regex na pairId dopuszcza zarówno ':' jak i '=' (różne warianty w kodzie JS).

UWAGA - te same zastrzeżenia co przy fetch_investing_data w agent.py:
niezudokumentowana struktura strony, może przestać działać bez ostrzeżenia po
zmianie HTML/CSS na investing.com (w szczególności selektor
'a.js-inner-all-results-quote-item' to konkretny fragment ich frontendu), i
może być blokowane na poziomie IP w GitHub Actions (Cloudflare/anti-bot) -
dokładnie tak samo jak główny fetch cen. Dlatego wyniki są CACHOWANE (patrz
resolve_missing_pair_ids w agent.py) i próba ponawiana jest rzadko (raz
dziennie), a nie w każdym 10-minutowym cyklu.
"""
import re
import time
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


class InvestingIDFetcher:
    SEARCH_URL = "https://www.investing.com/search/?q={query}"
    BASE_URL = "https://www.investing.com"

    def __init__(self, delay=1.5, timeout=10):
        self.delay = delay
        self.timeout = timeout

    def _get(self, url):
        time.sleep(self.delay)  # nie bombarduj strony - 1 zapytanie na `delay` sekund
        resp = requests.get(url, headers=HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def search_instrument_url(self, name):
        """Zwraca URL strony instrumentu z pierwszego trafienia wyszukiwarki."""
        r = self._get(self.SEARCH_URL.format(query=name))
        soup = BeautifulSoup(r.text, "html.parser")
        result = soup.select_one("a.js-inner-all-results-quote-item")
        if not result or not result.get("href"):
            raise ValueError(f"Nie znaleziono instrumentu w wynikach wyszukiwania: {name!r}")
        href = result.get("href")
        return href if href.startswith("http") else self.BASE_URL + href

    def extract_pair_id(self, instrument_url):
        """Wyciąga pairId z kodu JS osadzonego na stronie instrumentu."""
        r = self._get(instrument_url)
        match = re.search(r"pairId\s*[:=]\s*(\d+)", r.text)
        if not match:
            raise ValueError(f"Nie znaleziono pairId na stronie: {instrument_url}")
        return int(match.group(1))

    def get_pair_id(self, name):
        """Główna metoda: zwraca (pair_id, instrument_url)."""
        url = self.search_instrument_url(name)
        pair_id = self.extract_pair_id(url)
        return pair_id, url


if __name__ == "__main__":
    fetcher = InvestingIDFetcher()
    instruments = ["Apple", "Microsoft", "Nvidia", "Tesla", "Amazon", "Meta", "Alphabet"]
    for inst in instruments:
        try:
            pid, url = fetcher.get_pair_id(inst)
            print(f"{inst}: {pid}  ({url})")
        except Exception as e:
            print(f"Błąd dla {inst}: {e}")
