"""Prices, history, dividends and splits.

Primary source: yfinance (daily history incl. dividends/splits).
Fallback for the latest price only: Finnhub /quote.
Every price passes a sanity guard; a bad or stale price raises DataError.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

from .common import DataError, env, log


def _yf():
    import yfinance  # imported lazily so tests can run without it
    return yfinance


def fetch_history(tickers: list[str], period: str = "2y", retries: int = 3) -> dict[str, pd.DataFrame]:
    """Return {ticker: DataFrame[Close, Dividends, Stock Splits]} indexed by date. Missing tickers are omitted."""
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        for attempt in range(1, retries + 1):
            try:
                df = _yf().Ticker(t).history(period=period, auto_adjust=False, actions=True)
                if df is not None and not df.empty and "Close" in df:
                    df = df.copy()
                    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                    for col in ("Dividends", "Stock Splits"):
                        if col not in df:
                            df[col] = 0.0
                    cols = [c for c in ("Open", "Close", "Dividends", "Stock Splits") if c in df]
                    out[t] = df[cols].dropna(subset=["Close"])
                    break
                log(f"yfinance: empty history for {t} (attempt {attempt})")
            except Exception as e:  # yfinance raises many types (rate limits etc.)
                log(f"yfinance error for {t} (attempt {attempt}): {e}")
            time.sleep(2 * attempt)
    return out


def finnhub_quote(ticker: str) -> dict | None:
    key = env("FINNHUB_API_KEY")
    if not key or ticker.startswith("^") or "=" in ticker:
        return None
    try:
        r = requests.get("https://finnhub.io/api/v1/quote",
                         params={"symbol": ticker, "token": key}, timeout=20)
        if r.status_code != 200:
            return None
        j = r.json()
        price, ts = j.get("c"), j.get("t")
        if not price or not ts:
            return None
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        return {"price": float(price), "date": d.isoformat(), "source": "finnhub"}
    except (requests.RequestException, ValueError):
        return None


def latest_prices(tickers: list[str], history: dict[str, pd.DataFrame], max_age_days: int,
                  asof: date | None = None) -> tuple[dict[str, dict], list[str]]:
    """Latest close per ticker with fallback + guard. Returns (prices, errors)."""
    asof = asof or datetime.now(timezone.utc).date()
    prices, errors = {}, []
    for t in tickers:
        p = None
        df = history.get(t)
        if df is not None and not df.empty:
            p = {"price": float(df["Close"].iloc[-1]),
                 "date": df.index[-1].date().isoformat(), "source": "yfinance"}
        if p is None or not _fresh(p, asof, max_age_days):
            q = finnhub_quote(t)
            if q:
                p = q
        if p is None:
            errors.append(f"{t}: no price from any source")
            continue
        if not (p["price"] > 0) or p["price"] != p["price"]:
            errors.append(f"{t}: invalid price {p['price']}")
            continue
        if not _fresh(p, asof, max_age_days):
            errors.append(f"{t}: stale price from {p['date']}")
            continue
        prices[t] = p
    return prices, errors


def _fresh(p: dict, asof: date, max_age_days: int) -> bool:
    return (asof - date.fromisoformat(p["date"])).days <= max_age_days


def require_prices(tickers, prices, errors) -> None:
    missing = [t for t in tickers if t not in prices]
    if missing:
        raise DataError("ดึงราคาไม่ได้/ราคาผิดปกติ: " + "; ".join(errors or missing))


def corporate_actions(df: pd.DataFrame | None, after: date | None, upto: date) -> list[dict]:
    """Dividends and splits with ex-date in (after, upto], oldest first; splits before dividends on the same day."""
    if df is None or df.empty or after is None:
        return []
    events = []
    window = df[(df.index.date > after) & (df.index.date <= upto)]
    for ts, row in window.iterrows():
        d = ts.date().isoformat()
        if row.get("Stock Splits", 0) and row["Stock Splits"] > 0:
            events.append({"date": d, "type": "split", "ratio": float(row["Stock Splits"])})
        if row.get("Dividends", 0) and row["Dividends"] > 0:
            events.append({"date": d, "type": "dividend", "amount": float(row["Dividends"])})
    order = {"split": 0, "dividend": 1}
    return sorted(events, key=lambda e: (e["date"], order[e["type"]]))


def fill_stale(held, prices: dict, history: dict, previous: dict) -> list[str]:
    """Held tickers without a fresh price keep their last known price (flagged stale) instead of
    stopping the whole run. Returns the stale tickers."""
    stale = []
    for t in held:
        if t in prices:
            continue
        df = history.get(t)
        if df is not None and not df.empty:
            prices[t] = {"price": float(df["Close"].iloc[-1]), "date": df.index[-1].date().isoformat(),
                         "source": "stale-history", "stale": True}
        elif t in previous:
            prices[t] = {**previous[t], "source": "stale-previous", "stale": True}
        else:
            continue
        stale.append(t)
    return stale
