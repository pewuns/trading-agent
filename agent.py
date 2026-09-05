import os
import requests
import json
from datetime import datetime

# Token z GitHub Secrets
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# Rynki do monitorowania
MARKETS = {
    'S&P500': '^GSPC',
    'NASDAQ': '^IXIC',
    'DAX': '^GDAXI',
    'FTSE100': '^FTSE',
    'CAC40': '^FCHI',
    'NIKKEI': '^N225',
    'HANG SENG': '^HSI',
    'WIG20': 'WIG20.WA',
    'EUR/USD': 'EURUSD=X',
    'USD/JPY': 'JPY=X',
    'GOLD': 'GC=F',
    'OIL WTI': 'CL=F',
}

def get_market_data(symbol):
    """Pobierz dane rynkowe z Yahoo Finance"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            'interval': '5m',
            'range': '1d',
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)'
        }
        
        response = requests.get(url, params=params, headers=headers)
        data = response.json()
        
        result = data['chart']['result'][0]
        prices = result['indicators']['quote'][0]['close']
        highs = result['indicators']['quote'][0]['high']
        lows = result['indicators']['quote'][0]['low']
        
        # Usuń None
        prices = [p for p in prices if p is not None]
        highs = [h for h in highs if h is not None]
        lows = [l for l in lows if l is not None]
        
        if not prices:
            return None
        
        current_price = prices[-1]
        
        # Oblicz proste wskaźniki
        def sma(data, period):
            if len(data) < period:
                return None
            return sum(data[-period:]) / period
        
        def rsi(data, period=14):
            if len(data) < period + 1:
                return 50
            gains = []
            losses = []
            for i in range(1, len(data)):
                change = data[i] - data[i-1]
                gains.append(max(0, change))
                losses.append(max(0, -change))
            
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            
            if avg_loss == 0:
                return 100
            
            rs = avg_gain / avg_loss
            return 100 - (100 / (1 + rs))
        
        sma20 = sma(prices, 20)
        sma50 = sma(prices, 50)
        rsi_value = rsi(prices)
        
        # ATR
        tr_values = []
        for i in range(1, len(prices)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - prices[i-1]),
                abs(lows[i] - prices[i-1])
            )
            tr_values.append(tr)
        
        atr = sum(tr_values[-14:]) / 14 if tr_values else current_price * 0.02
        
        return {
            'price': current_price,
            'sma20': sma20,
            'sma50': sma50,
            'rsi': rsi_value,
            'atr': atr,
        }
        
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None

def analyze_market(name, symbol):
    """Analiza rynku"""
    data = get_market_data(symbol)
    if not data:
        return None
    
    if data['sma20'] is None or data['sma50'] is None:
        return None
    
    price = data['price']
    sma20 = data['sma20']
    sma50 = data['sma50']
    rsi = data['rsi']
    atr = data['atr']
    
    # Warunki LONG
    long_conditions = [
        price > sma20,
        price > sma50,
        rsi > 30 and rsi < 70,
        rsi > 50,
        atr > 0,
    ]
    
    # Warunki SHORT
    short_conditions = [
        price < sma20,
        price < sma50,
        rsi > 30 and rsi < 70,
        rsi < 50,
        atr > 0,
    ]
    
    long_score = sum(long_conditions) / len(long_conditions)
    short_score = sum(short_conditions) / len(short_conditions)
    
    if long_score >= 0.8:
        return {
            'name': name,
            'direction': 'LONG',
            'entry': price,
            'stop_loss': price - 1.5 * atr,
            'take_profit': price + 2.5 * atr,
            'confidence': long_score,
            'rsi': rsi,
        }
    elif short_score >= 0.8:
        return {
            'name': name,
            'direction': 'SHORT',
            'entry': price,
            'stop_loss': price + 1.5 * atr,
            'take_profit': price - 2.5 * atr,
            'confidence': short_score,
            'rsi': rsi,
        }
    
    return None

def send_telegram(signal):
    """Wyślij sygnał do Telegram"""
    emoji = '🟢' if signal['direction'] == 'LONG' else '🔴'
    
    message = f"""
{emoji} SYGNAŁ {signal['direction']} {emoji}

📊 Instrument: {signal['name']}
💰 Wejście: {signal['entry']:.4f}
🛑 Stop Loss: {signal['stop_loss']:.4f}
🎯 Take Profit: {signal['take_profit']:.4f}
📈 RSI: {signal['rsi']:.1f}
🎯 Pewność: {signal['confidence']:.0%}
⏰ {datetime.now().strftime('%H:%M:%S')}
"""
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
    }
    
    requests.post(url, json=payload)

def main():
    """Główna funkcja"""
    signals_found = 0
    
    for name, symbol in MARKETS.items():
        signal = analyze_market(name, symbol)
        if signal:
            send_telegram(signal)
            signals_found += 1
    
    print(f"Znaleziono {signals_found} sygnałów o {datetime.now()}")

if __name__ == "__main__":
    main()