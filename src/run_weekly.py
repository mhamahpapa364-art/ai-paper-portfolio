"""Weekly run: data → corporate actions → regime → news → AI decision → trades → performance → files → Telegram.

Usage:
  python -m src.run_weekly                    weekly run (AI decides if live)
  python -m src.run_weekly --dry-run          no AI / no Telegram
  python -m src.run_weekly --go-live --preview   AI proposes the starting portfolio; nothing is saved
  python -m src.run_weekly --go-live          AI builds the starting portfolio and the experiment starts
Exit code != 0 means a critical problem: the workflow must not commit.
"""
from __future__ import annotations

import argparse
from datetime import date

from . import manager as mg
from . import market_data as md
from . import news as nw
from . import portfolio as pf
from . import rules_engine as reng
from .common import (CONFIG, DOCS_DATA, HISTORY_PATH, STATE_PATH, WEEKLY_DIR, DataError, esc, fail,
                     load_json, log, save_json, settings, telegram, today)
from .decide import _cost, add_spend, decide, month_spend, portfolio_view
from .regime import read_regime
from .stock_tools import StockTools
from .summarize import summarize


def tracked_tickers(state: dict, cfg: dict) -> list[str]:
    held = set()
    for book in state["books"].values():
        held |= set(book["holdings"])
    held |= {r["ticker"] for r in state.get("pending_reviews", [])}
    if state["status"] != "live":
        held |= set(cfg["prelive_watchlist"])
    held.add(cfg["benchmark"])
    return sorted(held)


def annotate(level: str, msg: str) -> None:
    """GitHub Actions annotation (visible in the run summary)."""
    print(f"::{level} title=AI Portfolio::{msg.replace(chr(10), ' ')[:3500]}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no Anthropic/Telegram calls")
    ap.add_argument("--go-live", action="store_true", help="AI builds the starting portfolio")
    ap.add_argument("--preview", action="store_true", help="with --go-live: show the proposal, save nothing")
    ap.add_argument("--retry", action="store_true", help="Sunday safety run: only runs if Saturday's run failed")
    args = ap.parse_args(argv)
    dry, go_live, preview = args.dry_run, args.go_live, args.preview and args.go_live

    cfg = settings()
    rules = load_json(CONFIG / "rules.json")
    state = load_json(STATE_PATH)
    state.setdefault("pending_reviews", [])
    history_log = load_json(HISTORY_PATH, default=[])
    asof = today()
    month = asof.strftime("%Y-%m")
    warnings: list[str] = []
    if go_live and state["status"] == "live" and not preview:
        fail("พอร์ตเริ่มลงทุนไปแล้ว — go-live ซ้ำไม่ได้", dry)
    if args.retry and state.get("last_weekly_ok") and (asof - date.fromisoformat(state["last_weekly_ok"])).days <= 1:
        log("Saturday run already succeeded — Sunday retry not needed")
        return 0

    tickers = tracked_tickers(state, cfg)
    rt = cfg["regime_tickers"]
    regime_syms = [rt["market"], rt["vix"], rt["us10y"], *rt["sectors"]]
    fx_sym = cfg["fx_ticker"]

    # 1) Data --------------------------------------------------------------
    syms = sorted(set(tickers + regime_syms + [fx_sym]))
    log(f"Fetching history for {len(syms)} symbols")
    hist = md.fetch_history(syms)
    prices, errors = md.latest_prices(tickers + [fx_sym], hist, cfg["price_max_age_days"], asof)
    held = set().union(*(set(b["holdings"]) for b in state["books"].values()))
    prev_dash = load_json(DOCS_DATA / "dashboard.json", default={}) or {}
    stale = md.fill_stale(held, prices, hist, prev_dash.get("prices", {}))  # halted/acquired/renamed tickers
    for t in stale:
        warnings.append(f"{t}: ไม่มีราคาใหม่ (อาจถูกพักซื้อขาย/ถูกซื้อกิจการ/เปลี่ยนชื่อ) — ใช้ราคาล่าสุด {prices[t]['date']}")
    if fx_sym not in prices and prev_dash.get("fx") and prev_dash.get("run_date") and \
            (asof - date.fromisoformat(prev_dash["run_date"])).days <= 8:
        prices[fx_sym] = {"price": prev_dash["fx"], "date": prev_dash["run_date"], "source": "previous run"}
        warnings.append("ดึงค่าเงินบาทไม่ได้ — ใช้ค่าจากรอบก่อน")
    must = [t for t in held] + [cfg["benchmark"], fx_sym]
    try:
        md.require_prices(must, prices, errors)
    except DataError as e:
        fail(str(e), dry)
    fx = prices[fx_sym]["price"]
    if not (20 < fx < 60):
        fail(f"อัตราแลกเปลี่ยนผิดปกติ: {fx}", dry)
    market_date = prices[cfg["benchmark"]]["date"]
    for t, p in prices.items():
        df = hist.get(t)
        if df is not None and len(df) > 5:
            p["chg_1w"] = round(float(df["Close"].iloc[-1] / df["Close"].iloc[-6] - 1), 4)

    # 2) Corporate actions (live) --------------------------------------------
    events = []
    if state["status"] == "live":
        for book in state["books"].values():  # migration: THB cost basis for positions opened before it existed
            for h in book["holdings"].values():
                h.setdefault("cost_thb", h["cost_usd"] * state["fx_inception"])
        events = pf.process_events(state, hist, market_date, cfg["dividend_withholding"], warnings)

    # 3) Regime, news, filings, earnings -------------------------------------
    regime = read_regime(hist, cfg)
    warnings += [f"regime: {n}" for n in regime["notes"]]
    news_tickers = [t for t in tickers if t != cfg["benchmark"]] or tickers
    news, filings, earnings, profiles, summary = gather_news(news_tickers, regime, cfg, asof, month, state,
                                                             warnings, dry)

    # 4) AI decision ---------------------------------------------------------
    mode = "initial" if go_live else ("weekly" if state["status"] == "live" else None)
    decision_out, trades = None, []
    if mode and not dry:
        if month_spend(state, month) >= cfg["monthly_ai_budget_usd"]:
            warnings.append(f"ใช้งบ AI เดือนนี้ครบ ${cfg['monthly_ai_budget_usd']} แล้ว — สัปดาห์นี้ถือเฉยๆ")
            decision_out = {"mode": mode, "status": "skipped_budget"}
        else:
            decision_out, trades = run_decision(mode, state, cfg, rules, hist, prices, fx, regime, summary, news,
                                                filings, earnings, history_log, asof, market_date, month,
                                                warnings, preview)
        if go_live and not preview and state["status"] != "live":
            fail("AI สร้างพอร์ตตั้งต้นไม่ผ่านกฎ: " + "; ".join((decision_out or {}).get("errors", ["ไม่ทราบสาเหตุ"])), dry)

    if preview:
        report_preview(decision_out, prices, profiles, cfg, dry)
        return 0
    held_now = set(state["books"]["portfolio"]["holdings"])
    if trades and held_now - set(news_tickers):
        log("Portfolio changed — refreshing news for current holdings")
        news_tickers = sorted(held_now)
        news, filings, earnings, profiles, summary = gather_news(news_tickers, regime, cfg, asof, month, state,
                                                                 warnings, dry)

    # 5) Performance (after trades) ------------------------------------------
    perf, flags = None, []
    if state["status"] == "live":
        perf = pf.snapshot(state, prices, fx)
        history_log = [h for h in history_log if h["date"] != market_date]
        history_log.append({"date": market_date, "fx": fx,
                            **{k: perf[k]["value_thb"] for k in ("portfolio", "benchmark", "shadow")},
                            "portfolio_usd": perf["portfolio"]["value_usd"]})
        daily_vals = load_json(STATE_PATH.parent / "daily_values.json", default=[])
        series = sorted({**{h["date"]: h["portfolio"] for h in daily_vals},
                         **{h["date"]: h["portfolio"] for h in history_log}}.items())
        perf["max_drawdown"] = pf.max_drawdown([state["start_capital_thb"]] + [v for _, v in series])
        perf["drawdown_from_peak"] = min(perf["drawdown_from_peak"],
                                         perf["portfolio"]["value_thb"] / max([state["start_capital_thb"]] + [v for _, v in series]) - 1)
        flags = mg.human_flags(perf, history_log, rules)
        warnings += flags

    # 6) Monthly self-review ---------------------------------------------------
    monthly = None
    if not dry and mg.monthly_due(state, market_date):
        monthly = mg.monthly_review(state, cfg, history_log, market_date, warnings)
        if monthly:
            add_spend(state, month, monthly.get("cost", 0))

    left = credit_left(state, cfg)
    if left is not None and left < cfg["credit"]["alert_below_usd"]:
        warnings.append(f"เครดิต Anthropic เหลือประมาณ ${left:.2f} — ควรเติมเงินที่ platform.claude.com แล้วอัปเดตยอดใน settings")

    # 7) Save ----------------------------------------------------------------
    state["last_run"] = asof.isoformat()
    if not dry and not go_live:
        state["last_weekly_ok"] = asof.isoformat()
        state["recent_triggers"] = []
    card = load_json(mg.SCORECARD_PATH, default=[])
    book = state["books"]["portfolio"]
    theses = {t: (h.get("thesis") or {}).get("thesis", "") for t, h in book["holdings"].items()}
    about = {t: (h.get("thesis") or {}).get("about", "") for t, h in book["holdings"].items()}
    sectors = {t: h.get("sector") for t, h in book["holdings"].items()}
    weekly = {
        "run_date": asof.isoformat(), "market_date": market_date, "status": state["status"],
        "fx": fx, "prices": prices, "performance": perf, "events": events, "regime": regime,
        "news": news, "filings": filings, "profiles": profiles, "earnings": earnings, "summary": summary,
        "decision": decision_out, "trades": trades, "monthly_review": monthly, "warnings": warnings,
    }
    dashboard = {
        **weekly,
        "inception_date": state["inception_date"], "start_capital_thb": state["start_capital_thb"],
        "books": state["books"], "theses": theses, "about": about, "sectors": sectors,
        "max_sector_weight": rules["iron"].get("max_sector_weight"),
        "weights": pf.weights(book, prices) if state["status"] == "live" else {},
        "totals": state["totals"], "history": history_log,
        "scorecard": mg.hit_rate(card), "api_spend_month_usd": round(month_spend(state, month), 3),
        "human_flags": flags,
        "credit_left_usd": credit_left(state, cfg),
        "lessons_md": mg.lessons(),
        "scorecard_recent": card[-6:],
        "pending_orders": state.get("pending_orders"),
        "daily_values": load_json(STATE_PATH.parent / "daily_values.json", default=[])[-400:],
    }
    save_json(STATE_PATH, state)
    save_json(HISTORY_PATH, history_log)
    save_json(WEEKLY_DIR / f"{asof.isoformat()}.json", weekly)
    save_json(DOCS_DATA / "dashboard.json", dashboard)
    log("Files written")
    telegram(format_message(dashboard, cfg), dry)
    return 0


def credit_left(state: dict, cfg: dict) -> float | None:
    c = cfg.get("credit")
    if not c:
        return None
    total = state.get("api_spend_total", sum((state.get("api_spend") or {}).values()))
    return round(c["balance_usd"] - (total - c["at_total_spend"]), 2)


def gather_news(news_tickers, regime, cfg, asof, month, state, warnings, dry):
    news = nw.company_news(news_tickers, asof, cfg["news_lookback_days"], cfg["max_news_per_ticker"], warnings)
    filings = nw.edgar_filings(news_tickers, asof, cfg["news_lookback_days"], cfg["edgar_forms"], warnings)
    for rows in filings.values():
        for f in rows:
            f["label"] = nw.label_items(f.get("items", ""))
    earnings = nw.earnings_calendar(news_tickers, asof, cfg["earnings_lookahead_days"], warnings)
    profiles = nw.company_profiles(news_tickers, warnings)
    summary = summarize(news, filings, regime, earnings, cfg, warnings, dry, profiles)
    if summary and summary.get("usage"):
        add_spend(state, month, _cost(summary["usage"], cfg["summary_model"], cfg))
    return news, filings, earnings, profiles, summary


def run_decision(mode, state, cfg, rules, hist, prices, fx, regime, summary, news, filings, earnings,
                 history_log, asof, market_date, month, warnings, preview, triggers=None):
    tools = StockTools(asof, cfg["price_max_age_days"], cfg["leveraged_denylist"])
    tools.history.update(hist)
    due = mg.due_reviews(state, prices, hist, cfg["benchmark"], asof) if mode == "weekly" else []
    perf_now = pf.snapshot(state, prices, fx) if mode != "initial" else None
    ctx = {
        "asof": asof.isoformat(), "market_date": market_date, "fx": fx, "rules": rules,
        "lessons": mg.lessons(), "journal": mg.journal()[-cfg["journal_entries_in_prompt"]:],
        "portfolio": portfolio_view(state, prices, fx, asof) if mode != "initial" else None,
        "performance": perf_now and {"returns": perf_now["returns"], "drawdown": perf_now["drawdown_from_peak"]},
        "regime": {"label": regime["label"], "signals": regime["signals"], "sectors_4w": regime.get("sectors_4w"),
                   "cash_guidance": rules["flexible"]["regime_cash_guidance"].get(regime["label"])},
        "reviews_due": due,
        "news_summary": (summary or {}).get("structured"),
        "headlines": {t: [f"[{n.get('tier', 'other')}] {n['headline']} ({n['source']})" for n in v[:4]] for t, v in news.items()} if mode != "initial" else None,
        "sec_filings": {t: [f"{f['form']} {f['date']} {f.get('label', '')}" for f in v] for t, v in filings.items()}
        if mode != "initial" else None,
        "upcoming_earnings": earnings if mode != "initial" else None,
        "triggers": triggers,
        "turnover_used_last_7_days": reng.turnover_used(state, asof) if mode != "initial" else None,
        "emergency_triggers_since_last_review": state.get("recent_triggers") or None,
        "unexecuted_pending_orders": state.get("pending_orders"),
    }
    out = {"mode": mode, "cost_usd": 0.0, "tools_used": 0, "status": "no_answer"}
    d, errs, plan = None, [], {}
    for attempt in (1, 2):  # one chance to fix a proposal that broke a rule
        d, cost, tool_log = decide(mode, ctx, cfg, tools, warnings)
        add_spend(state, month, cost)
        out["cost_usd"] = round(out["cost_usd"] + cost, 4)
        out["tools_used"] += len(tool_log)
        if d is None:
            return out, []
        new = [str(t.get("ticker", "")).upper() for t in d.get("targets") or []
               if str(t.get("ticker", "")).upper() not in prices]
        for t in new:
            tools.stock_data(t)
        if new:
            extra, _ = md.latest_prices(new, tools.history, cfg["price_max_age_days"], asof)
            for t, p in extra.items():
                df = tools.history.get(t)
                if df is not None and len(df) > 5:
                    p["chg_1w"] = round(float(df["Close"].iloc[-1] / df["Close"].iloc[-6] - 1), 4)
            prices.update(extra)
        errs, plan = reng.validate(d, state, prices, rules, tools.eligible, asof,
                                   initial=(mode == "initial"), sector_of=tools.sector_of,
                                   turnover_used=reng.turnover_used(state, asof) if mode != "initial" else 0.0)
        if not errs or month_spend(state, month) >= cfg["monthly_ai_budget_usd"]:
            break
        log(f"attempt {attempt} rejected: {errs}")
        out["first_attempt_errors"] = errs
        ctx["previous_attempt"] = {"your_targets": d.get("targets"), "rejected_because": errs,
                                   "instruction": "Fix ONLY what broke the rules and return the full JSON again."}
    out.update({k: d.get(k) for k in ("market_view", "action", "summary_th", "journal", "targets", "sells")})

    trades: list = []
    if mode != "initial" and not preview and not errs:
        state["pending_orders"] = None  # a new decision always replaces unexecuted orders
    if errs:
        out.update(status="rejected", errors=errs)
        warnings.append("AI เสนอแผนที่ผิดกฎ จึงถือเฉยๆ: " + "; ".join(errs[:3]))
    elif mode == "initial":
        out["status"] = "proposed" if preview else "executed"
        if not preview:
            cash = max(0.0, 1 - sum(plan.values()))
            pf.seed_books(state, {**plan, "CASH": cash}, prices, fx, cfg["fee_rate"], cfg["benchmark"], market_date)
            trades = [{"date": market_date, "ticker": t, "side": "buy", "price": prices[t]["price"],
                       "usd": round(state["start_capital_usd"] * w, 2)} for t, w in plan.items()]
    elif plan and d.get("action") != "hold" and not preview:
        # Decided while the market is closed → fill at the next session's OPEN (run_daily executes it)
        cur = {t: w for t, w in pf.weights(state["books"]["portfolio"], prices).items() if t != "CASH"}
        changes = {t: round(plan.get(t, 0) - cur.get(t, 0), 4) for t in set(plan) | set(cur)
                   if abs(plan.get(t, 0) - cur.get(t, 0)) > 1e-4}
        if changes:
            state["pending_orders"] = {"decided": market_date, "mode": mode, "plan": plan, "changes": changes,
                                       "targets": d.get("targets"), "sells": d.get("sells"),
                                       "summary": d.get("summary_th")}
            out["status"], out["pending"] = "pending", changes
        else:
            out["status"] = "hold"
    else:
        out["status"] = "hold"

    out["sectors"] = {t: tools.sector_of(t) for t in plan}
    if preview:
        out["plan"] = plan
        return out, trades
    for t, h in state["books"]["portfolio"]["holdings"].items():
        h["sector"] = tools.sector_of(t) or h.get("sector")
    if trades:
        mg.record_theses(state, d, trades, prices, market_date, cfg["review_after_weeks"])
    if due:
        mg.apply_reviews(state, d.get("reviews"), due, market_date)
    mg.append_journal({"date": market_date, "mode": mode, "model": cfg["decision_model"], "status": out["status"],
                       "market_view": d.get("market_view"), "summary": d.get("summary_th"),
                       "journal": d.get("journal"), "trades": trades, "sells": d.get("sells"),
                       "rejected": out.get("errors"), "reviews": d.get("reviews"), "cost_usd": out["cost_usd"]})
    return out, trades


def report_preview(dec: dict | None, prices: dict, profiles: dict, cfg: dict, dry: bool) -> None:
    if not dec or dec.get("status") not in ("proposed", "rejected"):
        annotate("error", f"AI ไม่ได้เสนอพอร์ต: {dec}")
        telegram("⚠️ <b>Preview</b>: AI ไม่ได้เสนอพอร์ต ดูรายละเอียดใน GitHub Actions", dry)
        return
    lines = [f"🔎 <b>Preview พอร์ตตั้งต้น</b> (ยังไม่ได้ซื้อจริง) · ค่า AI ${dec['cost_usd']:.2f}"]
    annotate("notice", f"สถานะ {dec['status']} · cost ${dec['cost_usd']} · tools {dec['tools_used']}")
    if dec.get("market_view"):
        lines += ["", esc(dec["market_view"])]
        annotate("notice", "มุมมองตลาด: " + dec["market_view"])
    tg_sorted = sorted(dec.get("targets") or [], key=lambda x: -float(x.get("weight", 0)))
    annotate("notice", "พอร์ต: " + ", ".join(f"{t['ticker']} {float(t.get('weight', 0)):.0%}" for t in tg_sorted))
    for t in tg_sorted:
        w = float(t.get("weight", 0))
        lines.append(f"\n<b>{esc(t['ticker'])}</b> {w:.0%} — {esc(t.get('about', ''))}\n• {esc(t.get('thesis', ''))}\n"
                     f"• ขายเมื่อ: {esc(t.get('exit_condition', ''))}")
        annotate("notice", f"{t['ticker']} {w:.0%} | {t.get('about', '')} | ทำไม: {t.get('thesis', '')} | "
                           f"ขายเมื่อ: {t.get('exit_condition', '')} | คาดว่า: {t.get('expected_outcome', '')}")
    cash = 1 - sum(float(t.get("weight", 0)) for t in dec.get("targets") or [])
    lines.append(f"\nเงินสด {cash:.0%}")
    annotate("notice", f"เงินสด {cash:.0%}")
    if dec.get("errors"):
        lines += ["", "❌ ผิดกฎ: " + esc("; ".join(dec["errors"]))]
        for e in dec["errors"]:
            annotate("warning", "ผิดกฎ: " + e)
    telegram("\n".join(lines), dry)


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
        lines += ["", f"มูลค่า: <b>{p['portfolio']['value_thb']:,.0f} บาท</b> ({pct(r['portfolio_thb'])})",
                  f"vs VOO: {pct(r['vs_benchmark'])} · vs พอร์ตเงา: {pct(r['vs_shadow'])}",
                  f"ลงจากจุดสูงสุด: {pct(p['drawdown_from_peak'])}"]
    else:
        lines += ["", "สถานะ: <b>ยังไม่เริ่มลงทุน</b>"]
    dec = d.get("decision")
    if dec and dec.get("status") == "rejected":
        lines += ["", "⛔ <b>AI:</b> เสนอแผนที่ผิดกฎ ระบบจึงถือเฉยๆ (ดูเหตุผลใน dashboard)"]
    elif dec and dec.get("status") == "skipped_budget":
        lines += ["", "💸 <b>AI:</b> ใช้งบเดือนนี้ครบแล้ว สัปดาห์นี้ถือเฉยๆ"]
    elif dec and dec.get("summary_th"):
        icon = {"executed": "🔄", "hold": "⏸", "pending": "⏳"}.get(dec.get("status"), "🤖")
        lines += ["", f"{icon} <b>AI:</b> {esc(dec['summary_th'])}"]
        for t, ch in (dec.get("pending") or {}).items():
            lines.append(f"  {'เพิ่ม' if ch > 0 else 'ลด'} {t} {ch:+.1%}")
        if dec.get("pending"):
            lines.append("  → จะซื้อขายที่ราคาเปิดของวันทำการถัดไป")
        for t in d.get("trades") or []:
            lines.append(f"  {'ซื้อ' if t['side'] == 'buy' else 'ขาย'} {t['ticker']} ${t['usd']:,.0f}")
    if d["earnings"]:
        lines += ["", "งบที่จะประกาศ: " + ", ".join(f"{e['ticker']} {e['date']}" for e in d["earnings"][:6])]
    if d["summary"] and d["summary"].get("text"):
        lines += ["", "<b>สรุปข่าว</b>", esc(d["summary"]["text"])]
    if d.get("credit_left_usd") is not None and d["credit_left_usd"] < cfg["credit"]["alert_below_usd"]:
        lines += ["", f"💳 เครดิต AI เหลือประมาณ ${d['credit_left_usd']:.2f} — ถึงเวลาเติมเงินแล้ว"]
    for f in d.get("human_flags") or []:
        lines += ["", f"🚨 {esc(f)}"]
    other = [w for w in d["warnings"] if w not in (d.get("human_flags") or [])]
    if other:
        lines += ["", f"⚠️ คำเตือน {len(other)} รายการ (ดูใน dashboard)"]
    lines += ["", cfg["dashboard_url"]]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
