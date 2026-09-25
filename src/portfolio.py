"""Book-keeping for the three books: portfolio (AI), benchmark (VOO), shadow (buy-and-hold of the starting picks).

All money inside a book is USD. THB figures are derived with the run's FX rate.
"""
from __future__ import annotations

import copy

from .market_data import corporate_actions


def book_value(book: dict, prices: dict) -> float:
    v = book["cash_usd"]
    for t, h in book["holdings"].items():
        v += h["shares"] * prices[t]["price"]
    return v


def weights(book: dict, prices: dict) -> dict[str, float]:
    total = book_value(book, prices)
    if total <= 0:
        return {}
    w = {t: h["shares"] * prices[t]["price"] / total for t, h in book["holdings"].items()}
    w["CASH"] = book["cash_usd"] / total
    return w


def buy(book: dict, ticker: str, usd: float, price: float, fee_rate: float, on: str) -> float:
    """Spend `usd` (fee included) on ticker. Returns fee paid."""
    if usd <= 0:
        return 0.0
    if usd > book["cash_usd"] + 1e-6:
        raise ValueError(f"buy {ticker}: {usd:.2f} exceeds cash {book['cash_usd']:.2f}")
    fee = usd * fee_rate
    shares = (usd - fee) / price
    h = book["holdings"].setdefault(ticker, {"shares": 0.0, "cost_usd": 0.0, "opened": on})
    h["shares"] += shares
    h["cost_usd"] += usd
    book["cash_usd"] -= usd
    return fee


def sell(book: dict, ticker: str, shares: float, price: float, fee_rate: float) -> float:
    """Sell shares. Returns fee paid."""
    h = book["holdings"][ticker]
    shares = min(shares, h["shares"])
    gross = shares * price
    fee = gross * fee_rate
    frac = shares / h["shares"] if h["shares"] else 1
    h["cost_usd"] -= h["cost_usd"] * frac
    h["shares"] -= shares
    book["cash_usd"] += gross - fee
    if h["shares"] <= 1e-9:
        del book["holdings"][ticker]
    return fee


def apply_corporate_actions(book: dict, history: dict, after, upto, withholding: float) -> list[dict]:
    """Splits multiply shares; dividends go to cash net of withholding. Returns applied events."""
    applied = []
    for t, h in list(book["holdings"].items()):
        for ev in corporate_actions(history.get(t), after, upto):
            if ev["type"] == "split":
                h["shares"] *= ev["ratio"]
                applied.append({"ticker": t, **ev})
            else:
                net = h["shares"] * ev["amount"] * (1 - withholding)
                book["cash_usd"] += net
                applied.append({"ticker": t, **ev, "net_usd": round(net, 4)})
    return applied


def seed_books(state: dict, target_weights: dict[str, float], prices: dict, fx: float,
               fee_rate: float, benchmark: str, on: str) -> None:
    """Go-live: convert THB capital to USD, buy targets in portfolio, all-in benchmark, copy to shadow."""
    cap_usd = state["start_capital_thb"] / fx
    total_w = sum(w for t, w in target_weights.items() if t != "CASH")
    if total_w > 1 + 1e-9:
        raise ValueError("weights exceed 100%")
    port = {"cash_usd": cap_usd, "holdings": {}}
    fees = 0.0
    for t, w in target_weights.items():
        if t == "CASH" or w <= 0:
            continue
        fees += buy(port, t, cap_usd * w, prices[t]["price"], fee_rate, on)
    bench = {"cash_usd": cap_usd, "holdings": {}}
    buy(bench, benchmark, cap_usd, prices[benchmark]["price"], fee_rate, on)
    state["books"] = {"portfolio": port, "benchmark": bench, "shadow": copy.deepcopy(port)}
    state["status"] = "live"
    state["inception_date"] = on
    state["fx_inception"] = fx
    state["start_capital_usd"] = cap_usd
    state["last_events_processed"] = on
    state["totals"]["fees_usd"] = round(fees, 4)
    state["peak_value_thb"] = state["start_capital_thb"]


def snapshot(state: dict, prices: dict, fx: float) -> dict:
    """Performance numbers for the dashboard/history. Only meaningful when live."""
    start_thb, start_usd = state["start_capital_thb"], state["start_capital_usd"]
    out = {"fx": fx}
    raw = {name: book_value(book, prices) for name, book in state["books"].items()}
    for name, usd in raw.items():
        out[name] = {"value_usd": round(usd, 2), "value_thb": round(usd * fx, 2)}
    p = out["portfolio"]
    ret_thb = raw["portfolio"] * fx / start_thb - 1
    ret_usd = raw["portfolio"] / start_usd - 1
    fx_eff = fx / state["fx_inception"] - 1
    out["returns"] = {
        "portfolio_thb": ret_thb,
        "portfolio_usd": ret_usd,          # stock-picking result, FX removed
        "fx_effect": fx_eff,               # THB weakening = positive for us
        "benchmark_thb": raw["benchmark"] * fx / start_thb - 1,
        "shadow_thb": raw["shadow"] * fx / start_thb - 1,
    }
    out["returns"]["vs_benchmark"] = ret_thb - out["returns"]["benchmark_thb"]
    out["returns"]["vs_shadow"] = ret_thb - out["returns"]["shadow_thb"]
    peak = max(state.get("peak_value_thb") or start_thb, p["value_thb"])
    state["peak_value_thb"] = peak
    out["drawdown_from_peak"] = p["value_thb"] / peak - 1
    return out


def max_drawdown(values: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd
