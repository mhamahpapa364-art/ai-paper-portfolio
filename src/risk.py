"""Portfolio risk read-out from price history (no AI, no opinions).

Gives the AI (and the dashboard) numbers the sector cap cannot see:
- correlated groups: holdings that move together even when they sit in different sectors
- beta to VOO, annualised volatility, a bad-week estimate
- rate sensitivity: how the portfolio has reacted to moves in the 10-year Treasury yield
- theme weights: themes the AI itself assigned to each holding
"""
from __future__ import annotations

import math

import pandas as pd

CORR_THRESHOLD = 0.6


def _returns(history: dict, tickers, window: int) -> pd.DataFrame:
    cols = {t: history[t]["Close"] for t in tickers if t in history and history[t] is not None and len(history[t]) > 30}
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).sort_index().ffill().pct_change().iloc[1:].tail(window)


def correlated_groups(rets: pd.DataFrame, weights: dict, threshold: float = CORR_THRESHOLD) -> list[dict]:
    """Union-find over pairs with correlation >= threshold. Only groups of 2+ are returned, heaviest first."""
    tick = [t for t in rets.columns if weights.get(t, 0) > 0]
    if len(tick) < 2:
        return []
    corr = rets[tick].corr()
    parent = {t: t for t in tick}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(tick):
        for b in tick[i + 1:]:
            if corr.loc[a, b] >= threshold:
                parent[find(a)] = find(b)
    groups: dict[str, list] = {}
    for t in tick:
        groups.setdefault(find(t), []).append(t)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        pairs = [corr.loc[a, b] for i, a in enumerate(members) for b in members[i + 1:]]
        out.append({"tickers": sorted(members, key=lambda t: -weights.get(t, 0)),
                    "weight": round(sum(weights.get(t, 0) for t in members), 4),
                    "avg_corr": round(float(sum(pairs) / len(pairs)), 2)})
    return sorted(out, key=lambda g: -g["weight"])


def exposure(weights: dict, history: dict, market: str = "VOO", rate: str = "^TNX",
             themes: dict | None = None, window: int = 126) -> dict | None:
    """weights: {ticker: weight} of invested positions (cash excluded). ~6 months of daily data by default."""
    w = {t: v for t, v in weights.items() if t != "CASH" and v > 0}
    if not w:
        return None
    rets = _returns(history, list(w) + [market], window)
    held = [t for t in w if t in rets.columns]
    if len(rets) < 40 or not held or market not in rets.columns:
        return None
    port = sum(rets[t].fillna(0) * w[t] for t in held)
    mkt = rets[market].fillna(0)
    out = {"window_days": int(len(rets)),
           "beta_voo": round(float(port.cov(mkt) / mkt.var()), 2) if mkt.var() > 0 else None,
           "vol_annual": round(float(port.std() * math.sqrt(252)), 4),
           "voo_vol_annual": round(float(mkt.std() * math.sqrt(252)), 4)}
    week = (1 + port).rolling(5).apply(lambda x: x.prod(), raw=True).dropna() - 1
    if len(week):
        out["bad_week_1_in_20"] = round(float(week.quantile(0.05)), 4)
        out["worst_week"] = round(float(week.min()), 4)

    # Rate sensitivity: regress daily returns on daily change in the 10y yield (percentage points)
    if rate in history and history[rate] is not None and len(history[rate]) > 30:
        dy = history[rate]["Close"].sort_index().diff()
        df = pd.concat([port.rename("p"), dy.rename("dy")] + [rets[t].rename(t) for t in held], axis=1,
                       join="inner").dropna()
        if len(df) >= 40 and df["dy"].var() > 0:
            b = lambda s: float(s.cov(df["dy"]) / df["dy"].var())  # noqa: E731
            out["rate_move_per_25bp"] = round(b(df["p"]) * 0.25, 4)
            per = {t: round(b(df[t]) * 0.25, 4) for t in held}
            out["most_rate_sensitive"] = sorted(per.items(), key=lambda kv: kv[1])[:3]

    out["correlated_groups"] = correlated_groups(rets[held], w)
    if themes:
        tw: dict[str, float] = {}
        for t, v in w.items():
            if themes.get(t):
                tw[themes[t]] = tw.get(themes[t], 0) + v
        out["theme_weights"] = {k: round(v, 4) for k, v in sorted(tw.items(), key=lambda kv: -kv[1])}
    return out
