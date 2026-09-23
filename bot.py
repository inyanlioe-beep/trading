"""Indodax auto-trading bot. PAPER mode by default — no real orders.

Live mode (PAPER=false) uses the Indodax private API (HMAC-SHA512). Use at your own risk.

Config: read from .env in this folder (see .env.example). Real OS env vars override .env.

Env vars:
  PAPER            true|false          (default true)
  PAIR             btc_idr             (Indodax market; comma-separated for multiple, e.g. btc_idr,eth_idr)
    SIGNAL_SYMBOL    BTCUSDT             (unused when the screener drives the signal)
    INTERVAL         1h
    CUT_LOSS_PCT     3.0                 (force sell below entry)
    TAKE_PROFIT_PCT  6.0                 (force sell above entry; 0 = off)
    BUDGET_IDR       1000000             (per position)
    MAX_POSITIONS    3                   (max simultaneous coins)
    POLL_SECONDS     30
    STATE_FILE       paper_state.json
    MIN_ORDER_IDR    10000                (fallback if pair metadata unreachable)
    PAIR_MODE        manual|auto          (auto = follow screener's top pair)
    SCAN_MIN_SCORE   6                    (score >= this => BUY)
    SCAN_SELL_SCORE  0                    (score <= this => SELL)
    SCAN_MIN_VR      1.0                  (min volume ratio for a pair to qualify)
    SCAN_MIN_VOL     300000000            (min 24h IDR volume to be scanned)
    SCAN_REFRESH     3600                 (seconds between re-scans)
  INDODAX_KEY / INDODAX_SECRET         (live only)
"""
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx

from screener import analyse, candidates


def load_env_file(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env if present. Real env vars win."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


load_env_file()

INDODAX_PUBLIC = "https://indodax.com/api"
INDODAX_PRIVATE = "https://indodax.com/tapi"

_pairs_cache: dict = {}


def pair_meta(pair: str) -> dict:
    """Indodax pair metadata: min order, taker fee, qty increment. Cached per run."""
    if not _pairs_cache:
        r = httpx.get(f"{INDODAX_PUBLIC}/pairs", timeout=15)
        r.raise_for_status()
        _pairs_cache["all"] = {p["ticker_id"]: p for p in r.json()}
    meta = _pairs_cache["all"].get(pair, {})
    return {
        "min_idr": float(meta.get("trade_min_base_currency") or CFG["MIN_ORDER_IDR"]),
        "fee": float(meta.get("trade_fee_percent_taker") or 0.2) / 100,
        "qty_step": float(meta.get("quantity_increment") or 1e-8),
    }


def round_qty(qty: float, step: float) -> float:
    return math.floor(qty / step) * step


def ensure_liquid(pair: str) -> None:
    """Halt if the market is suspended or in maintenance."""
    pair_meta(pair)  # populate cache
    m = _pairs_cache.get("all", {}).get(pair)
    if not m:
        raise RuntimeError(f"pair {pair} not found in Indodax pairs list")
    if m.get("is_maintenance") or m.get("is_market_suspended"):
        raise RuntimeError(f"{pair} maintenance/suspended — bot halted")


def wallet_balance(currency: str) -> float:
    """Private API balance. Live only."""
    payload = {"method": "getInfo", "nonce": int(time.time() * 1000)}
    req = sign_payload(payload)
    r = httpx.post(INDODAX_PRIVATE, headers=req["headers"], data=req["data"], timeout=15)
    r.raise_for_status()
    out = r.json()
    if not out.get("success"):
        raise RuntimeError(f"Indodax error: {out}")
    return float(out["return"]["balance"].get(currency, 0))


def check_funds(qty: float, price: float, fee: float) -> None:
    """Live only: ensure enough IDR to buy. Refuses rather than over-spend."""
    need = qty * price * (1 + fee)
    have = wallet_balance("idr")
    if have < need:
        raise RuntimeError(f"insufficient IDR: need {need:,.0f}, have {have:,.0f}")


CFG = {
    "PAPER": os.getenv("PAPER", "true").lower() in ("1", "true", "yes"),
    "PAIR": os.getenv("PAIR", "btc_idr"),
    "SIGNAL_SYMBOL": os.getenv("SIGNAL_SYMBOL", "BTCUSDT"),
    "INTERVAL": os.getenv("INTERVAL", "1h"),
    "CUT_LOSS_PCT": float(os.getenv("CUT_LOSS_PCT", "3.0")),
    "TAKE_PROFIT_PCT": float(os.getenv("TAKE_PROFIT_PCT", "6.0")),
    "BUDGET_IDR": float(os.getenv("BUDGET_IDR", "1000000")),
    "MAX_POSITIONS": int(os.getenv("MAX_POSITIONS", "3")),
    "POLL_SECONDS": float(os.getenv("POLL_SECONDS", "30")),
    "STATE_FILE": os.getenv("STATE_FILE", "paper_state.json"),
    "MIN_ORDER_IDR": float(os.getenv("MIN_ORDER_IDR", "10000")),
    "FEE_TAKER": float(os.getenv("FEE_TAKER", "0.002")),
    "PAIR_MODE": os.getenv("PAIR_MODE", "manual").lower(),  # manual | auto
    "SCAN_MIN_VOL": float(os.getenv("SCAN_MIN_VOL", "300000000")),
    "SCAN_MIN_VR": float(os.getenv("SCAN_MIN_VR", "1.0")),
    "SCAN_MIN_SCORE": float(os.getenv("SCAN_MIN_SCORE", "6")),
    "SCAN_SELL_SCORE": float(os.getenv("SCAN_SELL_SCORE", "0")),
    "SCAN_REFRESH": float(os.getenv("SCAN_REFRESH", "3600")),  # seconds between re-scans
}


def validate_cfg() -> None:
    for key in ("CUT_LOSS_PCT", "TAKE_PROFIT_PCT", "BUDGET_IDR", "POLL_SECONDS"):
        v = CFG[key]
        assert math.isfinite(v), f"{key} must be a number"
    assert 0 < CFG["CUT_LOSS_PCT"] < 100, "CUT_LOSS_PCT must be 0..100"
    assert 0 <= CFG["TAKE_PROFIT_PCT"] < 100, "TAKE_PROFIT_PCT must be 0..100"
    assert CFG["BUDGET_IDR"] > 0, "BUDGET_IDR must be positive"
    assert CFG["MAX_POSITIONS"] >= 1, "MAX_POSITIONS must be >= 1"
    assert CFG["MIN_ORDER_IDR"] > 0, "MIN_ORDER_IDR must be positive"
    assert 0 <= CFG["FEE_TAKER"] < 1, "FEE_TAKER must be 0..1"
    assert CFG["POLL_SECONDS"] >= 5, "POLL_SECONDS must be >= 5"
    if not CFG["PAPER"]:
        assert os.getenv("INDODAX_KEY") and os.getenv("INDODAX_SECRET"), \
            "live mode needs INDODAX_KEY and INDODAX_SECRET"
        for p in manual_pairs():
            ensure_liquid(p)
    assert CFG["PAIR_MODE"] in ("manual", "auto"), "PAIR_MODE must be manual or auto"
    assert CFG["SCAN_SELL_SCORE"] < CFG["SCAN_MIN_SCORE"], \
        "SCAN_SELL_SCORE must be below SCAN_MIN_SCORE (no overlap => no flip-flop)"


_scan_cache: dict = {"at": 0.0, "top": None}


def score_action(score: float) -> str:
    """Screener score -> action. Single source of truth for the thresholds."""
    if score >= CFG["SCAN_MIN_SCORE"]:
        return "BUY"
    if score <= CFG["SCAN_SELL_SCORE"]:
        return "SELL"
    return "NEUTRAL"

def market_signal(pair: str) -> dict:
    """Screener score -> BUY/SELL/NEUTRAL. None (no Binance data) = NEUTRAL, never guess."""
    base = pair[: -len("idr") - 1]
    r = analyse({"pair": pair, "base": base, "vol_idr": 0.0, "last": 0.0}, CFG["INTERVAL"])
    if not r:
        return {"action": "NEUTRAL", "reason": f"{pair}: no data", "score": 0}
    score = r["score"]
    return {"action": score_action(score), "reason": f"score {score} [{r['reasons'][:60]}]", "score": score}

def best_pairs() -> list[dict] | None:
    """Highest-score liquid pairs from screener. Cached SCAN_REFRESH seconds.

    Safety: returns None (empty results) => bot keeps current positions and waits.
    """
    now = time.time()
    if not _scan_cache["top"] or now - _scan_cache["at"] >= CFG["SCAN_REFRESH"]:
        pool = candidates(CFG["SCAN_MIN_VOL"])
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = [r for r in ex.map(lambda c: analyse(c, CFG["INTERVAL"]), pool) if r]
        results = [r for r in results if r["vol_ratio"] >= CFG["SCAN_MIN_VR"] and r["score"] >= CFG["SCAN_MIN_SCORE"]]
        results.sort(key=lambda r: r["score"], reverse=True)
        _scan_cache["top"] = results
        _scan_cache["at"] = now
        print(f"[scan] {len(results)} pair lolos (score>={CFG['SCAN_MIN_SCORE']}, "
              f"vol>={CFG['SCAN_MIN_VR']}x) | top: "
              + (", ".join(f"{r['pair']}({r['score']})" for r in results[:3]) or "tidak ada"))
    return _scan_cache["top"] or None


def get_price(pair: str) -> float:
    r = httpx.get(f"{INDODAX_PUBLIC}/{pair}/ticker", timeout=10)
    r.raise_for_status()
    return float(r.json()["ticker"]["last"])


def sign_payload(payload: dict) -> dict:
    form = urllib.parse.urlencode(payload)
    sig = hmac.new(
        os.environ["INDODAX_SECRET"].encode(),
        form.encode(),
        hashlib.sha512,
    ).hexdigest()
    return {"headers": {"Key": os.environ["INDODAX_KEY"], "Sign": sig}, "data": form}


def indodax_order(pair: str, side: str, price: float, amount: float) -> dict:
    """side: 'buy' | 'sell'. Limit order at current price."""
    payload = {
        "method": "trade",
        "pair": pair,
        "type": side,
        "price": price,
        "btc": amount,
        "nonce": int(time.time() * 1000),
    }
    req = sign_payload(payload)
    r = httpx.post(INDODAX_PRIVATE, headers=req["headers"], data=req["data"], timeout=15)
    r.raise_for_status()
    out = r.json()
    if not out.get("success"):
        raise RuntimeError(f"Indodax error: {out}")
    return out["return"]


def active_pairs(state: dict) -> list[str]:
    return list(state.get("positions", {}).keys())

def pair_budget_free(state: dict) -> bool:
    return len(active_pairs(state)) < CFG["MAX_POSITIONS"]

def load_state() -> dict:
    try:
        with open(CFG["STATE_FILE"]) as f:
            s = json.load(f)
        s.setdefault("positions", {})
        s.setdefault("pnl_idr", 0.0)
        s.setdefault("trades", [])
        return s
    except (FileNotFoundError, json.JSONDecodeError):
        return {"positions": {}, "pnl_idr": 0.0, "trades": []}


def save_state(state: dict) -> None:
    with open(CFG["STATE_FILE"], "w") as f:
        json.dump(state, f, indent=2)


def buy(price: float, reason: str, state: dict, pair: str) -> None:
    if not pair_budget_free(state):
        print(f"[skip BUY {pair}] max positions {CFG['MAX_POSITIONS']} reached")
        return
    meta = pair_meta(pair)
    fee = meta["fee"] or CFG["FEE_TAKER"]
    budget = CFG["BUDGET_IDR"]
    # ponytail: assumes budget covers min order; raise instead of silently buying less
    if budget < meta["min_idr"]:
        raise RuntimeError(
            f"BUDGET_IDR {budget:,.0f} < min order {meta['min_idr']:,.0f} for {pair}"
        )
    qty = round_qty(budget / price / (1 + fee), meta["qty_step"])
    if qty <= 0:
        raise RuntimeError(f"computed qty {qty} is zero — budget too small for {pair}")
    if qty * price < meta["min_idr"]:
        raise RuntimeError(f"order value {qty * price:,.0f} < min {meta['min_idr']:,.0f}")

    if not CFG["PAPER"]:
        ensure_liquid(pair)
        check_funds(qty, price, fee)
        indodax_order(pair, "buy", price, qty)

    now = time.time()
    state["positions"][pair] = {
        "entry_price": price, "qty": qty, "entry_time": now,
        "entry_time_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "reason": reason, "fee_paid_idr": qty * price * fee,
        "pair": pair,
    }
    if CFG["PAPER"]:
        state["trades"].append({"side": "BUY", "price": price, "qty": qty, "time": now, "time_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "reason": reason, "pair": pair})
    print(f"[BUY{' PAPER' if CFG['PAPER'] else ''}] {pair} {qty:.8f} @ {price:,.0f} "
          f"= {qty * price:,.0f} IDR — {reason}")


def sell(price: float, reason: str, state: dict, pair: str) -> None:
    pos = state["positions"][pair]
    meta = pair_meta(pair)
    fee = meta["fee"] or CFG["FEE_TAKER"]
    qty = round_qty(pos["qty"], meta["qty_step"])
    if qty <= 0:
        raise RuntimeError(f"sell qty rounds to zero (pos {pos['qty']}, step {meta['qty_step']})")

    if not CFG["PAPER"]:
        ensure_liquid(pair)
        indodax_order(pair, "sell", price, qty)

    gross = (price - pos["entry_price"]) * qty
    fees = pos.get("fee_paid_idr", pos["entry_price"] * qty * fee) + price * qty * fee
    pnl = gross - fees
    pnl_pct = (price / pos["entry_price"] - 1) * 100
    state["pnl_idr"] += pnl
    now = time.time()
    if CFG["PAPER"]:
        state["trades"].append({"side": "SELL", "price": price, "qty": qty, "time": now, "time_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "reason": reason, "pair": pair})
    del state["positions"][pair]
    print(f"[SELL{' PAPER' if CFG['PAPER'] else ''}] {pair} {qty:.8f} @ {price:,.0f} — {reason} | "
          f"PnL {pnl:+,.0f} IDR ({pnl_pct:+.2f}%) after fees {fees:,.0f}")


def manual_pairs() -> list[str]:
    """Parse comma-separated PAIR config into list."""
    return [p.strip() for p in CFG["PAIR"].split(",") if p.strip()]

def step(state: dict) -> None:
    if CFG["PAIR_MODE"] == "auto":
        _step_auto(state)
        return
    for pair in manual_pairs():
        _step_pair(pair, state)

def _step_pair(pair: str, state: dict) -> None:
    try:
        price = get_price(pair)
    except Exception as e:
        print(f"[{pair}] price error: {e}")
        return
    sig = market_signal(pair)
    pos = state["positions"].get(pair)

    print(f"[{time.strftime('%H:%M:%S')}] {pair} {price:,.0f} | {sig['action']} — {sig['reason']}")

    if pos is None:
        if sig["action"] == "BUY":
            buy(price, sig["reason"], state, pair)
        return

    entry = pos["entry_price"]
    if price <= entry * (1 - CFG["CUT_LOSS_PCT"] / 100):
        sell(price, "CUT LOSS", state, pair)
    elif CFG["TAKE_PROFIT_PCT"] > 0 and price >= entry * (1 + CFG["TAKE_PROFIT_PCT"] / 100):
        sell(price, "TAKE PROFIT", state, pair)
    elif sig["action"] == "SELL":
        sell(price, sig["reason"], state, pair)

def _step_auto(state: dict) -> None:
    """Auto mode: manage exits for held pairs, then buy screener top up to MAX_POSITIONS."""
    for pair in list(active_pairs(state)):
        _step_pair(pair, state)
    if not pair_budget_free(state):
        return
    top = best_pairs()
    if not top:
        return
    held = set(active_pairs(state))
    for r in top:
        if not pair_budget_free(state):
            break
        pair = r["pair"]
        if pair in held:
            continue
        try:
            price = get_price(pair)
            buy(price, f"score {r['score']} [{r['reasons'][:60]}]", state, pair)
            held.add(pair)
        except Exception as e:
            print(f"[auto BUY {pair}] error: {e}")


def main() -> None:
    validate_cfg()
    state = load_state()
    pairs_str = ", ".join(manual_pairs()) if CFG["PAIR_MODE"] == "manual" else "auto"
    print(f"PAPER={CFG['PAPER']} pairs={pairs_str} max_positions={CFG['MAX_POSITIONS']} "
          f"cut_loss={CFG['CUT_LOSS_PCT']}% take_profit={CFG['TAKE_PROFIT_PCT']}% "
          f"budget={CFG['BUDGET_IDR']:,.0f}/pos")
    if "--once" in sys.argv:
        step(state)
        save_state(state)
        return
    while True:
        try:
            step(state)
            save_state(state)
        except Exception as e:
            print(f"error: {e}")
        time.sleep(CFG["POLL_SECONDS"])


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert score_action(CFG["SCAN_MIN_SCORE"]) == "BUY"
        assert score_action(CFG["SCAN_SELL_SCORE"]) == "SELL"
        assert score_action((CFG["SCAN_MIN_SCORE"] + CFG["SCAN_SELL_SCORE"]) / 2) == "NEUTRAL"
        _pairs_cache["all"] = {
            "btc_idr": {
                "trade_min_base_currency": 10000,
                "trade_fee_percent_taker": 0.2,
                "quantity_increment": 1e-8,
            },
            "eth_idr": {
                "trade_min_base_currency": 10000,
                "trade_fee_percent_taker": 0.2,
                "quantity_increment": 1e-8,
            },
        }
        assert abs(round_qty(1.234567891, 1e-8) - 1.23456789) < 1e-12
        assert round_qty(1.999999999, 1e-8) == 1.99999999
        state = {"positions": {}, "pnl_idr": 0.0, "trades": []}
        buy(1_000_000.0, "test", state, "btc_idr")
        pos = state["positions"]["btc_idr"]
        assert pos is not None and pos["qty"] > 0, "buy must set a positive qty"
        assert pos["qty"] * 1_000_000.0 >= 10000, "order value below min"
        buy(5_000_000.0, "test2", state, "eth_idr")
        assert len(state["positions"]) == 2, "should hold 2 positions"
        sell(1_060_000.0, "TAKE PROFIT", state, "btc_idr")
        assert "btc_idr" not in state["positions"], "sell must clear position"
        assert "eth_idr" in state["positions"], "eth position should remain"
        assert state["pnl_idr"] > 0, "winning trade must be profitable after fee"
        assert len(state["trades"]) == 3, "two buys + one sell recorded"
        print("bot selftest ok")
    else:
        main()
