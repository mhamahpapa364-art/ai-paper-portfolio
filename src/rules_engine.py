"""Hard enforcement of the rule book. The AI proposes; this module decides whether the proposal is legal
and turns it into trades. Nothing here trusts the model's arithmetic.
"""
from __future__ import annotations

from datetime import date

from . import portfolio as pf

NO_TRADE_BAND = 0.02  # weight changes smaller than this are ignored (prevents churn from price drift)


def weeks_held(opened: str, asof: date) -> float:
    return (asof - date.fromisoformat(opened)).days / 7


def validate(decision: dict, state: dict, prices: dict, rules: dict, eligible, asof: date,
             initial: bool = False, sector_of=None) -> tuple[list[str], dict]:
    """Return (errors, plan). plan = {ticker: target_weight}, only meaningful when errors is empty.

    `eligible(ticker) -> (bool, reason)` checks the universe (US-listed, not leveraged, has price).
    """
    iron, flex = rules["iron"], rules["flexible"]
    errors: list[str] = []
    if not initial and decision.get("action") == "hold":
        return [], {}
    targets = decision.get("targets") or []
    if not targets:
        return ["action is rebalance/initial but no targets given"], {}

    book = state["books"]["portfolio"]
    cur_w = {} if initial else {t: w for t, w in pf.weights(book, prices).items() if t != "CASH"}
    plan: dict[str, float] = {}
    for tg in targets:
        t = str(tg.get("ticker", "")).upper().strip()
        try:
            w = float(tg.get("weight"))
        except (TypeError, ValueError):
            errors.append(f"{t}: weight is not a number")
            continue
        if t in plan:
            errors.append(f"{t}: listed twice")
            continue
        if w < 0:
            errors.append(f"{t}: negative weight (short selling is forbidden)")
            continue
        if w == 0:
            continue
        if w > iron["max_position_weight"] + 1e-9:
            errors.append(f"{t}: weight {w:.1%} exceeds max {iron['max_position_weight']:.0%}")
        plan[t] = w

    total = sum(plan.values())
    cash = 1 - total
    if total > 1 + 1e-6:
        errors.append(f"weights sum to {total:.1%} (>100%) — leverage is forbidden")
    if cash < flex["cash_min"] - 1e-6 or cash > flex["cash_max"] + 1e-6:
        errors.append(f"cash {cash:.1%} outside allowed {flex['cash_min']:.0%}–{flex['cash_max']:.0%}")
    n = len(plan)
    if not (flex["position_count_min"] <= n <= flex["position_count_max"]):
        errors.append(f"{n} positions, allowed {flex['position_count_min']}–{flex['position_count_max']}")

    # Anti-churn band: keep existing weight when the change is tiny
    for t in list(plan):
        if t in cur_w and abs(plan[t] - cur_w[t]) < NO_TRADE_BAND:
            plan[t] = cur_w[t]

    meta = {str(x.get("ticker", "")).upper(): x for x in targets}
    sells = {str(s.get("ticker", "")).upper(): s for s in (decision.get("sells") or [])}
    for t, w in plan.items():
        increasing = w > cur_w.get(t, 0) + 1e-9
        if increasing:
            m = meta.get(t, {})
            missing = [k for k in iron["every_buy_needs"] if not str(m.get(k, "")).strip()]
            if missing:
                errors.append(f"{t}: buy is missing {', '.join(missing)}")
            if t not in cur_w:
                ok, why = eligible(t)
                if not ok:
                    errors.append(f"{t}: not eligible ({why})")
            if t not in prices:
                errors.append(f"{t}: no fresh price")

    if not initial:
        for t, w0 in cur_w.items():
            w1 = plan.get(t, 0.0)
            if w1 < w0 - 1e-9:
                s = sells.get(t)
                if not s or not str(s.get("reason", "")).strip():
                    errors.append(f"{t}: reduced/sold without a reason in 'sells'")
                    continue
                held = weeks_held(book["holdings"][t]["opened"], asof)
                if held < flex["min_holding_weeks"] and not s.get("thesis_broken"):
                    errors.append(f"{t}: held {held:.1f} weeks < minimum {flex['min_holding_weeks']} "
                                  f"(only allowed if thesis_broken=true)")
        turnover = sum(abs(plan.get(t, 0) - cur_w.get(t, 0)) for t in set(plan) | set(cur_w)) / 2
        if turnover > iron["max_weekly_turnover"] + 1e-6:
            errors.append(f"turnover {turnover:.1%} exceeds weekly max {iron['max_weekly_turnover']:.0%}")
    # Sector concentration (iron)
    cap = iron.get("max_sector_weight")
    if cap and sector_of and plan:
        by_sector: dict[str, float] = {}
        for t, w in plan.items():
            sec = sector_of(t) or f"other:{t}"
            by_sector[sec] = by_sector.get(sec, 0) + w
        for sec, w in by_sector.items():
            if w > cap + 1e-6:
                errors.append(f"sector {sec} = {w:.0%} exceeds max {cap:.0%}")
    return errors, plan


def execute(state: dict, plan: dict[str, float], prices: dict, fee_rate: float, on: str,
            fx: float | None = None) -> list[dict]:
    """Move the portfolio book to target weights at `prices`. Sells first, then buys (scaled to cash)."""
    book = state["books"]["portfolio"]
    total = pf.book_value(book, prices)
    cur = {t: h["shares"] * prices[t]["price"] / total for t, h in book["holdings"].items()}
    trades, fees = [], 0.0
    for t in sorted(set(cur) | set(plan)):
        d = plan.get(t, 0.0) - cur.get(t, 0.0)
        if d < -1e-9:
            px = prices[t]["price"]
            shares = book["holdings"][t]["shares"] if plan.get(t, 0) == 0 else (-d * total) / px
            f = pf.sell(book, t, shares, px, fee_rate)
            fees += f
            trades.append({"date": on, "ticker": t, "side": "sell", "shares": round(shares, 6), "price": px,
                           "usd": round(shares * px, 2), "fee": round(f, 4)})
    buys = {t: (plan[t] - cur.get(t, 0.0)) * total for t in plan if plan[t] - cur.get(t, 0.0) > 1e-9}
    need = sum(buys.values())
    scale = min(1.0, book["cash_usd"] / need) if need > 0 else 1.0
    for t, usd in sorted(buys.items()):
        usd *= scale
        if usd < 1:
            continue
        px = prices[t]["price"]
        f = pf.buy(book, t, usd, px, fee_rate, on, fx)
        fees += f
        trades.append({"date": on, "ticker": t, "side": "buy", "shares": round((usd - f) / px, 6), "price": px,
                       "usd": round(usd, 2), "fee": round(f, 4)})
    state["totals"]["fees_usd"] = round(state["totals"]["fees_usd"] + fees, 4)
    return trades


def validate_rule_changes(changes: dict, rules: dict, asof: date) -> tuple[list[str], dict]:
    """Monthly flexible-rule changes proposed by the AI. Returns (errors, accepted_changes)."""
    errors, ok = [], {}
    last = rules.get("flexible_last_changed")
    if changes and last and (asof - date.fromisoformat(last)).days < rules["iron"]["flexible_rule_change_min_days"]:
        return [f"flexible rules were changed on {last}; must wait {rules['iron']['flexible_rule_change_min_days']} days"], {}
    bounds = rules["flexible_bounds"]
    for k, v in (changes or {}).items():
        if k not in bounds:
            errors.append(f"{k}: not a flexible rule")
            continue
        lo, hi = bounds[k]
        try:
            v = type(rules["flexible"][k])(v)
        except (TypeError, ValueError):
            errors.append(f"{k}: bad value {v}")
            continue
        if not (lo <= v <= hi):
            errors.append(f"{k}: {v} outside bounds {lo}–{hi}")
            continue
        ok[k] = v
    merged = {**rules["flexible"], **ok}
    if merged["position_count_min"] > merged["position_count_max"] or merged["cash_min"] > merged["cash_max"]:
        return errors + ["changes make min > max"], {}
    return errors, ok
