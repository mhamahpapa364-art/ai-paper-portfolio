"""Market regime reader: risk-on / neutral / risk-off from free yfinance data.

The regime only guides cash level and position size (Part 2). It is never a reason
by itself to sell a holding whose thesis is intact.
"""
from __future__ import annotations

import pandas as pd


def _ret(s: pd.Series, days: int) -> float | None:
    if s is None or len(s) <= days:
        return None
    return float(s.iloc[-1] / s.iloc[-1 - days] - 1)


def read_regime(history: dict[str, pd.DataFrame], cfg: dict) -> dict:
    rt = cfg["regime_tickers"]
    out: dict = {"signals": {}, "score": 0, "label": "unknown", "notes": []}

    mkt = history.get(rt["market"])
    if mkt is not None and len(mkt) >= 200:
        c = mkt["Close"]
        sma50, sma200 = c.rolling(50).mean().iloc[-1], c.rolling(200).mean().iloc[-1]
        above = float(c.iloc[-1] / sma200 - 1)
        out["signals"]["market_vs_200dma"] = round(above, 4)
        out["signals"]["sma50_above_sma200"] = bool(sma50 > sma200)
        out["signals"]["market_4w"] = _ret(c, 20)
        out["score"] += 1 if above > 0 else -1
        out["score"] += 1 if sma50 > sma200 else -1
    else:
        out["notes"].append("ข้อมูล VOO ไม่พอคำนวณเส้น 200 วัน")

    vix = history.get(rt["vix"])
    if vix is not None and not vix.empty:
        v = float(vix["Close"].iloc[-1])
        out["signals"]["vix"] = round(v, 2)
        if v < 18:
            out["score"] += 1
        elif v > 25:
            out["score"] -= 1
            if v > 35:
                out["score"] -= 1
    else:
        out["notes"].append("ไม่มีข้อมูล VIX")

    tnx = history.get(rt["us10y"])
    if tnx is not None and len(tnx) > 20:
        c = tnx["Close"]
        out["signals"]["us10y"] = round(float(c.iloc[-1]), 3)
        out["signals"]["us10y_4w_change_pts"] = round(float(c.iloc[-1] - c.iloc[-21]), 3)
        # A fast rise in yields (>0.4pt in 4 weeks) pressures valuations
        if c.iloc[-1] - c.iloc[-21] > 0.4:
            out["score"] -= 1

    sectors = []
    for etf, name in rt["sectors"].items():
        df = history.get(etf)
        r = _ret(df["Close"], 20) if df is not None else None
        if r is not None:
            sectors.append({"etf": etf, "name": name, "ret_4w": round(r, 4)})
    sectors.sort(key=lambda s: s["ret_4w"], reverse=True)
    out["sectors_4w"] = sectors
    if sectors:
        defensive = {"XLP", "XLU", "XLV"}
        top3 = {s["etf"] for s in sectors[:3]}
        out["signals"]["defensives_leading"] = len(top3 & defensive) >= 2
        if out["signals"]["defensives_leading"]:
            out["score"] -= 1

    if out["signals"]:
        s = out["score"]
        out["label"] = "risk-on" if s >= 2 else "risk-off" if s <= -2 else "neutral"
    return out
