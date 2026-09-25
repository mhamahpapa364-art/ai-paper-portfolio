"""Intraday snapshot for the dashboard (display only — no AI, no trades, no changes to state).

Usage: python -m src.live_prices OUT_PATH [--force]
Runs only while the US market is open (plus 20 minutes after the close) unless --force.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import requests

from . import portfolio as pf
from .common import STATE_PATH, env, load_json, log, settings

NY = ZoneInfo("America/New_York")


def market_window(now: datetime) -> bool:
    ny = now.astimezone(NY)
    return ny.weekday() < 5 and time(9, 30) <= ny.time() <= time(16, 20)


def finnhub_quote(t: str) -> dict | None:
    key = env("FINNHUB_API_KEY")
    if not key:
        return None
    try:
        j = requests.get("https://finnhub.io/api/v1/quote", params={"symbol": t, "token": key}, timeout=15).json()
    except (requests.RequestException, ValueError):
        return None
    if not j.get("c") or not j.get("pc"):
        return None
    return {"price": float(j["c"]), "prev_close": float(j["pc"]), "ts": int(j.get("t") or 0), "source": "finnhub"}


def yf_quote(t: str) -> dict | None:
    try:
        import yfinance as yf
        fi = yf.Ticker(t).fast_info
        p, pc = fi.get("lastPrice"), fi.get("previousClose")
        if p and pc:
            return {"price": float(p), "prev_close": float(pc), "ts": 0, "source": "yfinance"}
    except Exception as e:  # noqa: BLE001
        log(f"yfinance quote {t}: {e}")
    return None


def session_status(now: datetime) -> str:
    """pre = weekday before the open · open · closed"""
    ny = now.astimezone(NY)
    if ny.weekday() >= 5:
        return "closed"
    if ny.time() < time(9, 30):
        return "pre"
    return "open" if ny.time() <= time(16, 20) else "closed"


def main(argv=None) -> int:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--status" and argv is None:
        print(session_status(datetime.now(timezone.utc)))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    if not args.force and not market_window(now):
        log("US market closed — nothing to do")
        return 3  # tells the workflow loop to stop
    cfg = settings()
    state = load_json(STATE_PATH)
    if state.get("status") != "live":
        log("portfolio not live yet")
        return 0

    tickers = sorted(set().union(*(set(b["holdings"]) for b in state["books"].values())))
    quotes, missing = {}, []
    for t in tickers:
        q = finnhub_quote(t) or yf_quote(t)
        if q and q["price"] > 0:
            quotes[t] = q
        else:
            missing.append(t)
    fxq = yf_quote(cfg["fx_ticker"])
    fx = fxq["price"] if fxq and 20 < fxq["price"] < 60 else state["fx_inception"]
    if missing:
        log(f"missing quotes: {missing}")
        if len(missing) > len(tickers) / 2:
            log("too many missing quotes — not publishing")
            return 1

    # Value books at live prices (missing tickers fall back to their last known close via prev_close = None)
    px = {t: {"price": q["price"]} for t, q in quotes.items()}
    prev = {t: {"price": q["prev_close"]} for t, q in quotes.items()}
    last_weekly = load_json(STATE_PATH.parent.parent / "docs" / "data" / "dashboard.json", default={}).get("prices", {})
    for t in missing:
        if t in last_weekly:
            px[t] = prev[t] = {"price": last_weekly[t]["price"]}

    out = {"updated_utc": now.isoformat(timespec="minutes"), "fx": round(fx, 4), "quotes": {}, "books": {},
           "missing": missing}
    for t, q in quotes.items():
        out["quotes"][t] = {"price": round(q["price"], 4), "chg_day": round(q["price"] / q["prev_close"] - 1, 5),
                            "source": q["source"]}
    start = state["start_capital_thb"]
    for name, book in state["books"].items():
        v, v0 = pf.book_value(book, px), pf.book_value(book, prev)
        out["books"][name] = {"value_thb": round(v * fx, 2), "return_thb": round(v * fx / start - 1, 5),
                              "chg_day": round(v / v0 - 1, 5) if v0 else None}
    b = out["books"]
    out["vs_benchmark"] = round(b["portfolio"]["return_thb"] - b["benchmark"]["return_thb"], 5)
    out["vs_shadow"] = round(b["portfolio"]["return_thb"] - b["shadow"]["return_thb"], 5)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log(f"live snapshot written: portfolio {b['portfolio']['value_thb']:,.0f} THB ({b['portfolio']['chg_day']:+.2%} today)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
