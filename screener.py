"""Scan Indodax IDR pairs for bullish setups.

Liquidity filter first (dead pairs give fake signals), then the same
EMA/RSI/MACD used by the bot. Signal data from Binance USDT klines.

Usage:
  python screener.py                 # top 15 IDR pairs
  python screener.py --min-vol 5e9   # raise liquidity bar (IDR 24h)
  python screener.py --min-vr 1.0    # only pairs with volume x1.0+ vs 20-bar avg
  python screener.py --interval 4h
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from main import add_indicators, fetch_klines, make_signal

QUOTE = "idr"


def tickers() -> dict:
    r = httpx.get("https://indodax.com/api/summaries", timeout=20)
    r.raise_for_status()
    return r.json()["tickers"]


def candidates(min_vol: float) -> list[dict]:
    out = []
    for tid, t in tickers().items():
        if not tid.endswith(f"_{QUOTE}"):
            continue
        try:
            vol = float(t["vol_idr"])
            last = float(t["last"])
        except (KeyError, ValueError):
            continue
        if vol < min_vol or last <= 0:
            continue
        base = tid[: -len(QUOTE) - 1]
        out.append({"pair": tid, "base": base, "vol_idr": vol, "last": last})
    return out


def volume_ratio(df) -> float:
    """Last *closed* bar volume vs prior 20-bar average. >1 = interest rising."""
    avg = df.volume.iloc[-22:-2].mean()
    return float(df.volume.iloc[-2] / avg) if avg > 0 else 0.0


def analyse(c: dict, interval: str) -> dict | None:
    try:
        df = add_indicators(fetch_klines(f"{c['base'].upper()}USDT", interval, 300))
    except Exception:
        return None  # no Binance pair (e.g. idr-only tokens) — skip, don't guess

    sig = make_signal(df)
    prev, last = df.iloc[-2], df.iloc[-1]
    rsi = float(last.rsi)
    vr = volume_ratio(df)

    score = 0
    reasons = []
    if last.ema20 > last.ema50:
        score += 2; reasons.append("EMA20>50")
    if prev.ema20 <= prev.ema50 and last.ema20 > last.ema50:
        score += 3; reasons.append("EMA bull cross")
    if last.macd_hist > 0:
        score += 2; reasons.append("MACD hist+")
    if last.macd_hist > prev.macd_hist:
        score += 1; reasons.append("hist rising")
    if 45 <= rsi <= 68:
        score += 2; reasons.append(f"RSI {rsi:.0f} room")
    elif rsi > 75:
        score -= 2; reasons.append(f"RSI {rsi:.0f} overbought")
    elif rsi < 35:
        score += 1; reasons.append(f"RSI {rsi:.0f} oversold")
    if vr > 1.5:
        score += 2; reasons.append(f"vol x{vr:.1f}")
    elif vr < 0.5:
        score -= 1; reasons.append(f"vol x{vr:.1f} thin")
    if last.close > prev.close:
        score += 1; reasons.append("up bar")
    if sig["action"] == "BUY":
        score += 3; reasons.append("bot BUY")
    elif sig["action"] == "SELL":
        score -= 3; reasons.append("bot SELL")

    chg = (last.close / prev.close - 1) * 100
    closed_prev = df.iloc[-3]
    vol_chg = (df.volume.iloc[-2] / df.volume.iloc[-3] - 1) * 100
    return {**c, "score": score, "rsi": rsi, "vol_ratio": vr, "chg": chg,
            "vol_chg": vol_chg, "action": sig["action"], "reasons": ", ".join(reasons)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--min-vol", type=float, default=1e9, help="min 24h volume in IDR")
    p.add_argument("--min-vr", type=float, default=0.0, help="min volume ratio vs 20-bar avg")
    p.add_argument("--interval", default="1h")
    p.add_argument("--top", type=int, default=15)
    a = p.parse_args()

    pool = candidates(a.min_vol)
    print(f"{len(pool)} pair IDR likuid (vol >= {a.min_vol:,.0f} IDR), analisa {a.interval}...\n")
    if not pool:
        print("Tidak ada pair lolos filter likuiditas. Turunkan --min-vol.")
        return

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = [r for r in ex.map(lambda c: analyse(c, a.interval), pool) if r]
    if a.min_vr > 0:
        kept = [r for r in results if r["vol_ratio"] >= a.min_vr]
        print(f"filter vol x{a.min_vr}: {len(kept)}/{len(results)} lolos\n")
        results = kept
    results.sort(key=lambda r: r["score"], reverse=True)

    print(f"{'PAIR':<12}{'SCORE':>6}{'RSI':>6}{'VOL':>7}{'CHG%':>8}  ALASAN")
    print("-" * 100)
    for r in results[: a.top]:
        print(f"{r['pair']:<12}{r['score']:>6}{r['rsi']:>6.0f}{r['vol_ratio']:>6.1f}x"
              f"{r['chg']:>8.2f}  {r['reasons'][:70]}")
    print(f"\n{len(results)} pair dianalisa dalam {time.time() - t0:.1f}s")
    print("Catatan: skor = heuristik teknikal, BUKAN prediksi. DYOR.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import pandas as pd
        up = add_indicators(pd.DataFrame({"close": [float(i) for i in range(1, 301)],
                                          "volume": [100.0] * 300}))
        assert make_signal(up)["action"] in ("BUY", "NEUTRAL", "SELL")
        assert up.ema20.iloc[-1] > up.ema50.iloc[-1]
        flat = add_indicators(pd.DataFrame({"close": [5.0] * 300, "volume": [1.0] * 300}))
        assert abs(flat.macd_hist.iloc[-1]) < 1e-9
        assert volume_ratio(pd.DataFrame({"volume": [10.0] * 21 + [100.0, 100.0]})) == 10.0
        print("screener selftest ok")
    else:
        main()
