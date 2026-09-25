"""Weekly run: prices → corporate actions → performance → regime → news/filings → summary → files → Telegram.

Usage:  python -m src.run_weekly [--dry-run]
Exit code != 0 means a critical data problem: the workflow must not commit.
"""
from __future__ import annotations

import argparse
from datetime import date

from . import market_data as md
from . import news as nw
from . import portfolio as pf
from .common import (DOCS_DATA, HISTORY_PATH, STATE_PATH, WEEKLY_DIR, DataError, esc, fail,
                     load_json, log, save_json, settings, telegram, today)
from .regime import read_regime
from .summarize import summarize


def tracked_tickers(state: dict, cfg: dict) -> list[str]:
    held = set()
    for book in state["books"].values():
        held |= set(book["holdings"])
    if state["status"] != "live":
        held |= set(cfg["prelive_watchlist"])
    held.add(cfg["benchmark"])
    return sorted(held)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no Anthropic/Telegram calls")
    args = ap.parse_args(argv)
    dry = args.dry_run

    cfg = settings()
    state = load_json(STATE_PATH)
    history_log = load_json(HISTORY_PATH, default=[])
    asof = today()
    warnings: list[str] = []

    tickers = tracked_tickers(state, cfg)
    rt = cfg["regime_tickers"]
    regime_syms = [rt["market"], rt["vix"], rt["us10y"], *rt["sectors"]]
    fx_sym = cfg["fx_ticker"]

    # 1) Data --------------------------------------------------------------
    log(f"Fetching history for {len(set(tickers + regime_syms + [fx_sym]))} symbols")
    hist = md.fetch_history(sorted(set(tickers + regime_syms + [fx_sym])))
    prices, errors = md.latest_prices(tickers + [fx_sym], hist, cfg["price_max_age_days"], asof)
    try:
        md.require_prices(tickers + [fx_sym], prices, errors)
    except DataError as e:
        fail(str(e), dry)
    fx = prices[fx_sym]["price"]
    if not (20 < fx < 60):  # THB per USD sanity band
        fail(f"อัตราแลกเปลี่ยนผิดปกติ: {fx}", dry)
    market_date = max(p["date"] for t, p in prices.items() if t != fx_sym)

    # 2) Corporate actions + performance (live only) -------------------------
    events, perf = [], None
    if state["status"] == "live":
        after = date.fromisoformat(state["last_events_processed"])
        mdate = date.fromisoformat(market_date)
        for name, book in state["books"].items():
            for ev in pf.apply_corporate_actions(book, hist, after, mdate, cfg["dividend_withholding"]):
                ev["book"] = name
                events.append(ev)
                if name == "portfolio" and ev["type"] == "dividend":
                    state["totals"]["dividends_usd"] = round(state["totals"]["dividends_usd"] + ev["net_usd"], 4)
        state["last_events_processed"] = market_date
        perf = pf.snapshot(state, prices, fx)
        history_log = [h for h in history_log if h["date"] != market_date]
        history_log.append({"date": market_date, "fx": fx,
                            **{k: perf[k]["value_thb"] for k in ("portfolio", "benchmark", "shadow")},
                            "portfolio_usd": perf["portfolio"]["value_usd"]})
        perf["max_drawdown"] = pf.max_drawdown([state["start_capital_thb"]] + [h["portfolio"] for h in history_log])

    # 3) Regime --------------------------------------------------------------
    regime = read_regime(hist, cfg)
    warnings += [f"regime: {n}" for n in regime["notes"]]

    # 4) News, filings, earnings ----------------------------------------------
    news_tickers = [t for t in tickers if t != cfg["benchmark"]] or tickers
    news = nw.company_news(news_tickers, asof, cfg["news_lookback_days"], cfg["max_news_per_ticker"], warnings)
    filings = nw.edgar_filings(news_tickers, asof, cfg["news_lookback_days"], cfg["edgar_forms"], warnings)
    for rows in filings.values():
        for f in rows:
            f["label"] = nw.label_items(f.get("items", ""))
    earnings = nw.earnings_calendar(news_tickers, asof, cfg["earnings_lookahead_days"], warnings)
    profiles = nw.company_profiles(news_tickers, warnings)
    summary = summarize(news, filings, regime, earnings, cfg, warnings, dry, profiles)

    # 1-week price change for display
    for t, p in prices.items():
        df = hist.get(t)
        if df is not None and len(df) > 5:
            p["chg_1w"] = round(float(df["Close"].iloc[-1] / df["Close"].iloc[-6] - 1), 4)

    # 5) Save ----------------------------------------------------------------
    state["last_run"] = asof.isoformat()
    weekly = {
        "run_date": asof.isoformat(), "market_date": market_date, "status": state["status"],
        "fx": fx, "prices": prices, "performance": perf, "events": events, "regime": regime,
        "news": news, "filings": filings, "profiles": profiles, "earnings": earnings, "summary": summary, "warnings": warnings,
    }
    dashboard = {
        **weekly,
        "inception_date": state["inception_date"],
        "start_capital_thb": state["start_capital_thb"],
        "books": state["books"],
        "weights": pf.weights(state["books"]["portfolio"], prices) if state["status"] == "live" else {},
        "totals": state["totals"],
        "history": history_log,
    }
    save_json(STATE_PATH, state)
    save_json(HISTORY_PATH, history_log)
    save_json(WEEKLY_DIR / f"{asof.isoformat()}.json", weekly)
    save_json(DOCS_DATA / "dashboard.json", dashboard)
    log("Files written")

    telegram(format_message(dashboard, cfg), dry)
    return 0


def pct(x, signed=True) -> str:
    return "–" if x is None else (f"{x:+.2%}" if signed else f"{x:.2%}")


def format_message(d: dict, cfg: dict) -> str:
    lines = [f"📊 <b>AI Paper Portfolio</b> — {d['market_date']}"]
    reg = d["regime"]
    emoji = {"risk-on": "🟢", "neutral": "🟡", "risk-off": "🔴"}.get(reg["label"], "⚪")
    lines.append(f"ตลาด: {emoji} {reg['label']} · VIX {reg['signals'].get('vix', '–')} · USD/THB {d['fx']:.2f}")
    p = d["performance"]
    if p:
        r = p["returns"]
        lines += [
            "",
            f"มูลค่า: <b>{p['portfolio']['value_thb']:,.0f} บาท</b> ({pct(r['portfolio_thb'])})",
            f"vs VOO: {pct(r['vs_benchmark'])} · vs พอร์ตเงา: {pct(r['vs_shadow'])}",
            f"ผลจากหุ้น (USD): {pct(r['portfolio_usd'])} · ผลจากค่าเงิน: {pct(r['fx_effect'])}",
            f"ลงจากจุดสูงสุด: {pct(p['drawdown_from_peak'])}",
        ]
    else:
        lines += ["", "สถานะ: <b>pre-live</b> (ยังไม่เริ่มลงทุน — รันทดสอบระบบ)"]
    if d["earnings"]:
        lines.append("")
        lines.append("งบที่จะประกาศ: " + ", ".join(f"{e['ticker']} {e['date']}" for e in d["earnings"][:6]))
    if d["summary"] and d["summary"].get("text"):
        lines += ["", "<b>สรุปข่าว</b>", esc(d["summary"]["text"])]
    if d["warnings"]:
        lines += ["", f"⚠️ คำเตือน {len(d['warnings'])} รายการ (ดูใน dashboard)"]
    lines += ["", cfg["dashboard_url"]]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
