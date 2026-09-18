"""Trading analysis API. Fetch klines from Binance, compute indicators, return signal."""
import sys

import httpx
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BINANCE = "https://data-api.binance.vision/api/v3"  # public mirror; api.binance.com is geo/ISP-blocked here
INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}

app = FastAPI(title="Trading Analyzer")


def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    r = httpx.get(
        f"{BINANCE}/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise HTTPException(404, f"No data for {symbol}")
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbb", "tbq", "ignore",
        ],
    )
    df["time"] = df["open_time"] // 1000
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0).fillna(50.0)
    df["rsi"] = rsi
    return df


def make_signal(df: pd.DataFrame) -> dict:
    if len(df) < 51:
        return {"action": "NEUTRAL", "reason": "insufficient data"}
    prev, last = df.iloc[-2], df.iloc[-1]
    up = prev.ema20 <= prev.ema50 and last.ema20 > last.ema50
    down = prev.ema20 >= prev.ema50 and last.ema20 < last.ema50
    rsi = float(last.rsi)
    hist, phist = float(last.macd_hist), float(prev.macd_hist)

    macd_bull_cross = prev.macd <= prev.macd_signal and last.macd > last.macd_signal
    macd_bear_cross = prev.macd >= prev.macd_signal and last.macd < last.macd_signal
    hist_rising = hist > phist
    hist_falling = hist < phist

    # Confirmed signals: EMA cross + MACD in agreement
    if up and macd_bull_cross and rsi < 70:
        return {"action": "BUY", "reason": f"EMA20>50 + MACD bull cross, RSI {rsi:.1f}"}
    if down and macd_bear_cross and rsi > 30:
        return {"action": "SELL", "reason": f"EMA20<50 + MACD bear cross, RSI {rsi:.1f}"}
    if rsi >= 70 and hist_falling:
        return {"action": "SELL", "reason": f"RSI overbought {rsi:.1f} + MACD hist falling"}
    if rsi <= 30 and hist_rising:
        return {"action": "BUY", "reason": f"RSI oversold {rsi:.1f} + MACD hist rising"}
    if macd_bull_cross:
        return {"action": "BUY", "reason": f"MACD bull cross (RSI {rsi:.1f})"}
    if macd_bear_cross:
        return {"action": "SELL", "reason": f"MACD bear cross (RSI {rsi:.1f})"}
    if up and rsi < 70:
        return {"action": "BUY", "reason": f"EMA20 crossed above EMA50, RSI {rsi:.1f}"}
    if down and rsi > 30:
        return {"action": "SELL", "reason": f"EMA20 crossed below EMA50, RSI {rsi:.1f}"}
    return {"action": "NEUTRAL", "reason": f"No signal, RSI {rsi:.1f}"}


def _downsample(series: pd.Series, n: int = 300) -> list[float | None]:
    vals = series.tail(n).tolist()
    return [None if pd.isna(v) else round(float(v), 6) for v in vals]


@app.get("/api/klines")
def klines(
    symbol: str = Query("BTCUSDT", pattern=r"^[A-Z0-9]{4,20}$"),
    interval: str = Query("1h"),
    limit: int = Query(300, ge=50, le=1000),
):
    if interval not in INTERVALS:
        raise HTTPException(400, f"interval must be one of {sorted(INTERVALS)}")
    df = add_indicators(fetch_klines(symbol, interval, limit))
    return {
        "symbol": symbol,
        "interval": interval,
        "candles": [
            {
                "time": int(r.time),
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
            }
            for r in df.itertuples()
        ],
        "volume": [
            {"time": int(r.time), "value": r.volume, "color": "#26a69a" if r.close >= r.open else "#ef5350"}
            for r in df.itertuples()
        ],
        "ema20": [{"time": int(t), "value": v} for t, v in zip(df.time, df.ema20)],
        "ema50": [{"time": int(t), "value": v} for t, v in zip(df.time, df.ema50)],
        "rsi": [{"time": int(t), "value": round(float(v), 2)} for t, v in zip(df.time, df.rsi)],
        "macd_hist": _downsample(df.macd_hist),
        "price": float(df.close.iloc[-1]),
        "signal": make_signal(df),
    }


app.mount("/", StaticFiles(directory="static", html=True), name="static")


def _selftest() -> None:
    """Runnable check: RSI/EMA on known input."""
    df = pd.DataFrame({"close": [float(i) for i in range(1, 101)]})
    out = add_indicators(df)
    assert out.ema20.iloc[-1] > out.ema50.iloc[-1], "uptrend EMA20 must lead EMA50"
    assert out.rsi.iloc[-1] > 99, "monotonic rise => RSI ~100"
    assert out.macd.iloc[-1] > out.macd_signal.iloc[-1], "uptrend MACD above signal"
    assert out.macd_hist.iloc[-1] > 0, "uptrend hist positive"
    flat = add_indicators(pd.DataFrame({"close": [5.0] * 60}))
    assert abs(flat.rsi.iloc[-1] - 50) < 1e-6, "flat market => RSI 50"
    assert abs(flat.macd_hist.iloc[-1]) < 1e-9, "flat market => hist 0"
    assert make_signal(pd.DataFrame({"close": [1.0] * 10}))["action"] == "NEUTRAL"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        uvicorn.run(app, host="127.0.0.1", port=8000)
