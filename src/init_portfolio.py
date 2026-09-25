"""Go-live: open the three books at today's prices.

Used once, at go-live (Part 2). Weights are chosen by the AI at that time.
Usage:  python -m src.init_portfolio '{"ETN":0.2,"LLY":0.2,"CASH":0.1,...}' [--force]
"""
from __future__ import annotations

import argparse
import json

from . import market_data as md
from . import portfolio as pf
from .common import DataError, STATE_PATH, load_json, log, save_json, settings, today


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("weights", help='JSON like {"AAPL":0.2,"CASH":0.1}')
    ap.add_argument("--force", action="store_true", help="re-initialise even if already live")
    args = ap.parse_args(argv)

    cfg = settings()
    state = load_json(STATE_PATH)
    if state["status"] == "live" and not args.force:
        raise SystemExit("already live — refusing to reset (use --force if you really mean it)")
    w = json.loads(args.weights)
    invested = sum(v for k, v in w.items() if k != "CASH")
    if abs(invested + w.get("CASH", 0) - 1) > 1e-6:
        raise SystemExit(f"weights must sum to 1.0 (got {invested + w.get('CASH', 0):.4f})")
    if any(v > 0.25 + 1e-9 for k, v in w.items() if k != "CASH"):
        raise SystemExit("iron rule: max 25% per position")

    tickers = sorted({k for k in w if k != "CASH"} | {cfg["benchmark"]})
    hist = md.fetch_history(tickers + [cfg["fx_ticker"]], period="1mo")
    prices, errors = md.latest_prices(tickers + [cfg["fx_ticker"]], hist, cfg["price_max_age_days"])
    try:
        md.require_prices(tickers + [cfg["fx_ticker"]], prices, errors)
    except DataError as e:
        raise SystemExit(str(e))
    fx = prices[cfg["fx_ticker"]]["price"]
    on = max(p["date"] for t, p in prices.items() if t != cfg["fx_ticker"])
    pf.seed_books(state, w, prices, fx, cfg["fee_rate"], cfg["benchmark"], on)
    save_json(STATE_PATH, state)
    log(f"Live from {on} at USD/THB {fx:.3f}; capital ${state['start_capital_usd']:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
