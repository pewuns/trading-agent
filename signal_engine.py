"""
Signal Engine — Python re-implementation
=========================================
Kalman-filtered Supertrend + Smart Trail + RSI/Volume filters + trade
management + backtest table, following the spec in the source document.

UŻYCIE W TYM PROJEKCIE: to jest MODEL-CIEŃ (opcja C z rozmowy) - jego
sygnały są logowane obok produkcyjnego scoringu w agent.py wyłącznie do
porównania (patrz compute_signal_engine_shadow w agent.py), NIE wpływają
na to, co faktycznie jest wysyłane na Telegram. Kod poniżej jest wierną
kopią propozycji użytkownika - celowo NIE zmieniałem algorytmu, żeby
porównanie było miarodajne względem tego, co faktycznie zaproponowano.

Educational / research use only — not financial advice.
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Step 1 — Kalman filter
# ---------------------------------------------------------------------------
class KalmanFilter1D:
    """Simple scalar Kalman filter.

    `smoothing` maps to process variance q: lower q -> filter trusts its own
    prediction more (smoother output); higher q -> more reactive to new data.
    """

    def __init__(self, smoothing: float = 0.05, measurement_var: float = 1.0):
        self.q = smoothing
        self.r = measurement_var
        self.estimate = None
        self.error = 1.0

    def update(self, measurement: float) -> float:
        if self.estimate is None:
            self.estimate = measurement
            return self.estimate

        # Predict
        pred_estimate = self.estimate
        pred_error = self.error + self.q

        # Correct
        k_gain = pred_error / (pred_error + self.r)
        self.estimate = pred_estimate + k_gain * (measurement - pred_estimate)
        self.error = (1 - k_gain) * pred_error
        return self.estimate


def kalman_smooth(series: pd.Series, smoothing: float = 0.05) -> pd.Series:
    kf = KalmanFilter1D(smoothing)
    out = np.empty(len(series))
    for i, v in enumerate(series.values):
        out[i] = kf.update(v)
    return pd.Series(out, index=series.index)


# ---------------------------------------------------------------------------
# Helpers: ATR, RSI
# ---------------------------------------------------------------------------
def atr(df: pd.DataFrame, period: int = 10) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


# ---------------------------------------------------------------------------
# Step 2 — Supertrend on Kalman-filtered inputs
# ---------------------------------------------------------------------------
def kalman_supertrend(df: pd.DataFrame, smoothing: float = 0.05,
                       multiplier: float = 3.0, atr_period: int = 10):
    hl2 = (df["high"] + df["low"]) / 2
    raw_atr = atr(df, atr_period)

    k_price = kalman_smooth(hl2, smoothing)
    k_atr = kalman_smooth(raw_atr, smoothing)

    upper = k_price + multiplier * k_atr
    lower = k_price - multiplier * k_atr

    n = len(df)
    direction = np.ones(n)  # 1 = bullish, -1 = bearish
    final_upper = upper.values.copy()
    final_lower = lower.values.copy()
    close = df["close"].values

    for i in range(1, n):
        # band "stickiness" like classic Supertrend
        if close[i - 1] <= final_upper[i - 1]:
            final_upper[i] = min(upper.values[i], final_upper[i - 1])
        else:
            final_upper[i] = upper.values[i]

        if close[i - 1] >= final_lower[i - 1]:
            final_lower[i] = max(lower.values[i], final_lower[i - 1])
        else:
            final_lower[i] = lower.values[i]

        if close[i] > final_upper[i - 1]:
            direction[i] = 1
        elif close[i] < final_lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    return pd.DataFrame({
        "k_price": k_price,
        "k_atr": k_atr,
        "upper": final_upper,
        "lower": final_lower,
        "direction": direction,
    }, index=df.index)


# ---------------------------------------------------------------------------
# Step 3 — Smart Trail (independent ATR trailing stop, ratchets one way)
# ---------------------------------------------------------------------------
def smart_trail(df: pd.DataFrame, direction: np.ndarray,
                 multiplier: float = 2.0, atr_period: int = 10) -> pd.Series:
    a = atr(df, atr_period).values
    close = df["close"].values
    n = len(df)
    trail = np.empty(n)
    trail[0] = close[0] - multiplier * a[0] if direction[0] == 1 else close[0] + multiplier * a[0]

    for i in range(1, n):
        if direction[i] == 1:
            candidate = close[i] - multiplier * a[i]
            trail[i] = max(candidate, trail[i - 1]) if direction[i - 1] == 1 else candidate
        else:
            candidate = close[i] + multiplier * a[i]
            trail[i] = min(candidate, trail[i - 1]) if direction[i - 1] == -1 else candidate

    return pd.Series(trail, index=df.index)


# ---------------------------------------------------------------------------
# Steps 4-7 — build raw signals: momentum filter, volume tier, cooldown
# ---------------------------------------------------------------------------
def generate_signals(df: pd.DataFrame,
                      smoothing: float = 0.05,
                      st_multiplier: float = 3.0,
                      trail_multiplier: float = 2.0,
                      atr_period: int = 10,
                      use_rsi_filter: bool = True,
                      rsi_period: int = 14,
                      use_volume_filter: bool = True,
                      volume_ma_period: int = 20,
                      cooldown_bars: int = 5) -> pd.DataFrame:

    st = kalman_supertrend(df, smoothing, st_multiplier, atr_period)
    direction = st["direction"].values
    trail = smart_trail(df, direction, trail_multiplier, atr_period)

    close = df["close"].values
    flip = np.zeros(len(df), dtype=int)  # +1 bullish flip, -1 bearish flip, 0 none
    for i in range(1, len(df)):
        if direction[i] == 1 and direction[i - 1] == -1:
            flip[i] = 1
        elif direction[i] == -1 and direction[i - 1] == 1:
            flip[i] = -1

    rsi_vals = rsi(df["close"], rsi_period) if use_rsi_filter else None
    vol_ma = df["volume"].rolling(volume_ma_period).mean() if use_volume_filter and "volume" in df else None

    signal = []  # "BUY", "BUY+", "SELL", "SELL+", or None
    last_signal_idx = -cooldown_bars - 1

    for i in range(len(df)):
        sig = None
        if flip[i] == 1 and close[i] > trail.iloc[i]:
            sig = "BUY"
        elif flip[i] == -1 and close[i] < trail.iloc[i]:
            sig = "SELL"

        if sig and use_rsi_filter:
            r, r_prev = rsi_vals.iloc[i], rsi_vals.iloc[i - 1] if i > 0 else rsi_vals.iloc[i]
            if sig == "BUY" and not (r > 50 and r > r_prev):
                sig = None
            if sig == "SELL" and not (r < 50 and r < r_prev):
                sig = None

        if sig and (i - last_signal_idx) < cooldown_bars:
            sig = None

        if sig and use_volume_filter and vol_ma is not None and not pd.isna(vol_ma.iloc[i]):
            strong = df["volume"].iloc[i] > vol_ma.iloc[i]
            sig = sig + ("+" if strong else "")

        if sig:
            last_signal_idx = i

        signal.append(sig)

    out = df.copy()
    out["direction"] = direction
    out["smart_trail"] = trail
    out["signal"] = signal
    return out


# ---------------------------------------------------------------------------
# Trade management + backtest table
# ---------------------------------------------------------------------------
def run_backtest(df: pd.DataFrame,
                  swing_lookback: int = 10,
                  atr_period: int = 10,
                  sl_atr_buffer: float = 0.5,
                  mode: str = "split",  # "tp1", "tp2", or "split"
                  max_trades: int = 500) -> pd.DataFrame:

    a = atr(df, atr_period)
    trades = []
    open_trade = None
    signals_list = df["signal"].tolist()  # kept separate: row-wise iloc access
    # coerces object/None to NaN float when mixed with numeric columns

    for i in range(len(df)):
        row = df.iloc[i]
        sig = signals_list[i]
        if not isinstance(sig, str):
            sig = None

        # manage open trade first
        if open_trade is not None:
            direction = open_trade["direction"]
            hit_tp1 = (row["high"] >= open_trade["tp1"]) if direction == 1 else (row["low"] <= open_trade["tp1"])
            hit_tp2 = (row["high"] >= open_trade["tp2"]) if direction == 1 else (row["low"] <= open_trade["tp2"])
            hit_sl = (row["low"] <= open_trade["sl"]) if direction == 1 else (row["high"] >= open_trade["sl"])

            if mode == "split":
                if not open_trade["tp1_hit"] and hit_tp1:
                    open_trade["tp1_hit"] = True
                if open_trade["tp1_hit"] and hit_tp2:
                    open_trade["outcome"] = "TP2"
                    open_trade["pnl"] = _rr(open_trade, 2.0)
                    trades.append(open_trade); open_trade = None
                elif open_trade["tp1_hit"] and hit_sl:
                    open_trade["outcome"] = "TP1+SL"
                    open_trade["pnl"] = (_rr(open_trade, 1.0) - 1.0) / 2  # half win, half loss ~breakeven
                    trades.append(open_trade); open_trade = None
                elif not open_trade["tp1_hit"] and hit_sl:
                    open_trade["outcome"] = "SL"
                    open_trade["pnl"] = -1.0
                    trades.append(open_trade); open_trade = None
            else:
                target = "tp1" if mode == "tp1" else "tp2"
                rr = 1.0 if mode == "tp1" else 2.0
                if hit_sl:
                    open_trade["outcome"] = "SL"
                    open_trade["pnl"] = -1.0
                    trades.append(open_trade); open_trade = None
                elif (target == "tp1" and hit_tp1) or (target == "tp2" and hit_tp2):
                    open_trade["outcome"] = target.upper()
                    open_trade["pnl"] = rr
                    trades.append(open_trade); open_trade = None

            # early exit on opposite signal while in profit/breakeven
            if open_trade is not None and sig and sig.startswith(
                "SELL" if open_trade["direction"] == 1 else "BUY"
            ):
                current_rr = (row["close"] - open_trade["entry"]) / open_trade["risk"] * open_trade["direction"]
                if current_rr >= 0:
                    open_trade["outcome"] = "Early Exit"
                    open_trade["pnl"] = current_rr
                    trades.append(open_trade); open_trade = None

        # open a new trade
        if open_trade is None and sig and i >= swing_lookback:
            direction = 1 if sig.startswith("BUY") else -1
            entry = row["close"]
            window = df.iloc[i - swing_lookback:i + 1]
            buf = a.iloc[i] * sl_atr_buffer
            if direction == 1:
                sl = window["low"].min() - buf
            else:
                sl = window["high"].max() + buf
            risk = abs(entry - sl)
            tp1 = entry + direction * risk
            tp2 = entry + direction * 2 * risk

            open_trade = dict(
                entry_idx=i, direction=direction, entry=entry, sl=sl,
                tp1=tp1, tp2=tp2, risk=risk, tier="+" in sig,
                signal_type=sig, tp1_hit=False,
            )

        if len(trades) >= max_trades:
            break

    return pd.DataFrame(trades)


def _rr(trade, rr):
    return rr


def summarize_backtest(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    def stats(sub):
        wins = sub[sub["pnl"] > 0]["pnl"].sum()
        losses = -sub[sub["pnl"] < 0]["pnl"].sum()
        n = len(sub)
        winrate = (sub["pnl"] > 0).mean() * 100 if n else 0
        pf = wins / losses if losses > 0 else np.inf
        return pd.Series({
            "trades": n, "win_rate_%": round(winrate, 1),
            "net_pnl_R": round(sub["pnl"].sum(), 2), "profit_factor": round(pf, 2),
        })

    categories = {
        "Overall": trades,
        "All BUY": trades[trades["direction"] == 1],
        "Normal BUY": trades[(trades["direction"] == 1) & (~trades["tier"])],
        "Strong BUY": trades[(trades["direction"] == 1) & (trades["tier"])],
        "All SELL": trades[trades["direction"] == -1],
        "Normal SELL": trades[(trades["direction"] == -1) & (~trades["tier"])],
        "Strong SELL": trades[(trades["direction"] == -1) & (trades["tier"])],
    }
    return pd.DataFrame({name: stats(sub) for name, sub in categories.items()}).T


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    np.random.seed(0)
    n = 1000
    price = 100 + np.cumsum(np.random.randn(n))
    df = pd.DataFrame({
        "close": price,
        "high": price + np.random.rand(n),
        "low": price - np.random.rand(n),
        "volume": np.random.randint(100, 1000, n),
    })

    signals_df = generate_signals(df)
    trades = run_backtest(signals_df)
    print(summarize_backtest(trades))
