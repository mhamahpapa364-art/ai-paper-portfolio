"""Daily run after the US close (Mon–Fri): fill pending orders at the open, then scan for emergencies.

1. Pending orders (decided while the market was closed) are filled at the OPEN of the first session after
   the decision date. Positions without an opening print (halted) are left untouched.
2. Emergency scan — no AI unless something serious happens:
     • a holding moves ≥ ±10% in one day          • a holding has no new price (halt/acquisition/rename)
     • market shock: VOO ≤ −5% in a day or VIX ≥ 35 • a critical SEC 8-K item (bankruptcy, delisting notice,
                                                       auditor change, restatement, impairment, acquisition)
   A trigger wakes the AI in "event" mode (default HOLD, same rule book). Its trades are filled at the next open.
   On Friday night no AI is called: the weekly review runs a few hours later and receives the triggers.
Telegram is only used when something happened. Exit code != 0 = do not commit.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from . import manager as mg
from . import market_data as md
from . import news as nw
from . import portfolio as pf
from . import rules_engine as reng
from .common import CONFIG, DOCS_DATA, STATE_PATH, esc, fail, load_json, log, save_json, settings, telegram, today
from .decide import month_spend
from .regime import read_regime
from .run_weekly import credit_left, run_decision
from .stock_tools import StockTools

CRITICAL_8K = {"1.03": "ล้มละลาย/พิทักษ์ทรัพย์", "2.01": "ซื้อ/ขายกิจการเสร็จสิ้น", "2.06": "ด้อยค่าสินทรัพย์ก้อนใหญ่",
               "3.01": "ถูกเตือนเรื่องการจดทะเบียน", "4.01": "เปลี่ยนผู้สอบบัญชี", "4.02": "งบเดิมเชื่อถือไม่ได้/แก้งบย้อนหลัง"}


DAILY_PATH = STATE_PATH.parent / "daily_values.json"


def record_daily_value(state: dict, hist: dict, fx: float, day, cfg: dict) -> None:
    """End-of-day value of the three books — gives true (not weekly-sampled) drawdowns."""
    px = {}
    for b in state["books"].values():
        for t in b["holdings"]:
            df = hist.get(t)
            if df is not None and not df.empty:
                px[t] = {"price": float(df[df.index.date <= day]["Close"].iloc[-1])}
    if any(t not in px for b in state["books"].values() for t in b["holdings"]):
        return
    rows = [r for r in load_json(DAILY_PATH, default=[]) if r["date"] != day.isoformat()]
    rows.append({"date": day.isoformat(), "fx": round(fx, 4),
                 **{k: round(pf.book_value(b, px) * fx, 2) for k, b in state["books"].items()}})
    save_json(DAILY_PATH, sorted(rows, key=lambda r: r["date"]))
    state["peak_value_thb"] = max(state.get("peak_value_thb") or 0, rows[-1]["portfolio"])


def fill_pending(state: dict, hist: dict, cfg: dict, fx: float, benchmark: str, warnings: list) -> list[dict]:
    po = state.get("pending_orders")
    if not po:
        return []
    decided = date.fromisoformat(po["decided"])
    bench = hist.get(benchmark)
    if bench is None or "Open" not in bench:
        warnings.append("ไม่มีราคาเปิดของตลาด — เลื่อนการซื้อขายไปวันถัดไป")
        return []
    sessions = [d.date() for d in bench.index if d.date() > decided]
    if not sessions:
        return []  # next session has not happened yet
    day = sessions[0]
    if len(sessions) > 5:
        warnings.append(f"คำสั่งซื้อขายจาก {po['decided']} ค้างนานเกินไป — ยกเลิก")
        state["pending_orders"] = None
        return []

    # corporate actions up to and including the fill day happen BEFORE the open fill
    pf.process_events(state, hist, day, cfg["dividend_withholding"], warnings)
    book = state["books"]["portfolio"]
    tickers = set(po["plan"]) | set(book["holdings"])
    opens, skipped = {}, []
    for t in tickers:
        df = hist.get(t)
        row = df[df.index.date == day] if df is not None else None
        if row is not None and len(row) and "Open" in row and row["Open"].iloc[0] > 0:
            opens[t] = {"price": float(row["Open"].iloc[0])}
        else:
            skipped.append(t)
    plan = dict(po["plan"])
    if skipped:
        # value untouched positions at their last close, and never trade a ticker without an opening price
        for t in skipped:
            df = hist.get(t)
            last = df[df.index.date <= day]["Close"] if df is not None else []
            if t in book["holdings"] and len(last):
                opens[t] = {"price": float(last.iloc[-1])}
        cur = pf.weights(book, opens)
        for t in skipped:
            if t in book["holdings"]:
                plan[t] = cur.get(t, 0)
            else:
                plan.pop(t, None)
        warnings.append("ไม่มีราคาเปิดของ " + ", ".join(sorted(skipped)) + " — ข้ามตัวนี้ในรอบนี้")
    total_before = pf.book_value(book, opens)
    trades = reng.execute(state, plan, opens, cfg["fee_rate"], day.isoformat(), fx)
    state.setdefault("turnover_log", []).append({"date": day.isoformat(),
                                                 "turnover": round(reng.turnover_of(trades, total_before), 4)})
    state["turnover_log"] = state["turnover_log"][-60:]
    for tr in trades:
        tr["fill"] = "open"
    decision = {"targets": po.get("targets") or []}
    if trades:
        mg.record_theses(state, decision, trades, opens, day.isoformat(), cfg["review_after_weeks"])
    mg.append_journal({"date": day.isoformat(), "mode": "execution", "status": "executed",
                       "summary": f"ซื้อขายตามแผนวันที่ {po['decided']} ที่ราคาเปิด", "trades": trades,
                       "skipped": skipped})
    state["pending_orders"] = None
    state["last_execution"] = {"date": day.isoformat(), "decided": po["decided"], "trades": trades,
                               "summary": po.get("summary")}
    return trades


def scan(state: dict, hist: dict, cfg: dict, asof: date, warnings: list) -> list[dict]:
    ec = cfg["emergency"]
    bench = hist.get(cfg["benchmark"])
    if bench is None or len(bench) < 2:
        return []
    last_day = bench.index[-1].date()
    trig = []
    for t in state["books"]["portfolio"]["holdings"]:
        df = hist.get(t)
        if df is None or df.empty or (last_day - df.index[-1].date()).days >= 3:
            trig.append({"type": "no_price", "ticker": t, "date": last_day.isoformat(),
                         "detail": "ไม่มีราคาใหม่ — อาจถูกพักซื้อขาย ถูกซื้อกิจการ หรือเปลี่ยนชื่อ"})
            continue
        if len(df) >= 2 and df.index[-1].date() == last_day:
            ch = float(df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1)
            if abs(ch) >= ec["stock_move"]:
                trig.append({"type": "big_move", "ticker": t, "date": last_day.isoformat(), "change": round(ch, 4),
                             "detail": f"ราคา{'ขึ้น' if ch > 0 else 'ลง'} {ch:+.1%} ในวันเดียว"})
    mch = float(bench["Close"].iloc[-1] / bench["Close"].iloc[-2] - 1)
    if mch <= -ec["market_drop"]:
        trig.append({"type": "market_shock", "ticker": cfg["benchmark"], "date": last_day.isoformat(),
                     "change": round(mch, 4), "detail": f"ตลาดทั้งตลาดลง {mch:.1%} ในวันเดียว"})
    vix = hist.get(cfg["regime_tickers"]["vix"])
    if vix is not None and not vix.empty and float(vix["Close"].iloc[-1]) >= ec["vix_level"]:
        trig.append({"type": "market_fear", "ticker": "^VIX", "date": last_day.isoformat(),
                     "detail": f"VIX {float(vix['Close'].iloc[-1]):.1f} — ตลาดตื่นตระหนก"})
    held = list(state["books"]["portfolio"]["holdings"])
    for t, rows in nw.edgar_filings(held, asof, 3, ["8-K", "6-K"], warnings).items():
        for f in rows:
            crit = [c.strip() for c in (f.get("items") or "").split(",") if c.strip() in CRITICAL_8K]
            if crit:
                trig.append({"type": "sec_filing", "ticker": t, "date": f["date"], "url": f["url"],
                             "detail": f"{f['form']}: " + ", ".join(CRITICAL_8K[c] for c in crit)})
    seen = set(state.get("triggers_seen", []))
    fresh = [x for x in trig if f"{x['type']}|{x['ticker']}|{x['date']}" not in seen]
    state["triggers_seen"] = (list(seen) + [f"{x['type']}|{x['ticker']}|{x['date']}" for x in fresh])[-200:]
    return fresh


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ai", action="store_true", help="scan only, never call the AI")
    args = ap.parse_args(argv)
    dry = args.dry_run
    cfg, rules, state = settings(), load_json(CONFIG / "rules.json"), load_json(STATE_PATH)
    if state.get("status") != "live":
        log("not live")
        return 0
    asof, warnings = today(), []
    month = asof.strftime("%Y-%m")
    po = state.get("pending_orders") or {}
    held = set(state["books"]["portfolio"]["holdings"])
    rt = cfg["regime_tickers"]
    syms = sorted(held | set(po.get("plan", {})) | {cfg["benchmark"], cfg["fx_ticker"], rt["vix"], rt["us10y"]}
                  | set(rt["sectors"]) | set().union(*(set(b["holdings"]) for b in state["books"].values())))
    hist = md.fetch_history(syms, period="1y")
    md.patch_with_quotes(hist, syms)
    if cfg["benchmark"] not in hist:
        fail("รายวัน: ดึงราคาตลาดไม่ได้", dry)
    fxdf = hist.get(cfg["fx_ticker"])
    fx = float(fxdf["Close"].iloc[-1]) if fxdf is not None and not fxdf.empty else None
    if not fx or not (20 < fx < 60):
        prev = load_json(DOCS_DATA / "dashboard.json", default={}) or {}
        fx = prev.get("fx") or state["fx_inception"]
        warnings.append("ดึงค่าเงินบาทไม่ได้ — ใช้ค่าล่าสุดที่มี")

    trades = fill_pending(state, hist, cfg, fx, cfg["benchmark"], warnings)
    last_session = hist[cfg["benchmark"]].index[-1].date()
    events = pf.process_events(state, hist, last_session, cfg["dividend_withholding"], warnings)
    record_daily_value(state, hist, fx, last_session, cfg)
    triggers = scan(state, hist, cfg, asof, warnings)
    state.setdefault("recent_triggers", [])
    state["recent_triggers"] = (state["recent_triggers"] + triggers)[-30:]

    event_out = None
    friday_night = last_session.weekday() == 4
    if triggers and not dry and not args.no_ai and not friday_night:
        if month_spend(state, month) >= cfg["monthly_ai_budget_usd"]:
            warnings.append("มีเหตุฉุกเฉินแต่งบ AI เดือนนี้หมด — ถือเฉยๆ")
        else:
            prices, _ = md.latest_prices(sorted(held | {cfg["benchmark"]}), hist, 7, asof)
            md.fill_stale(held, prices, hist, {})
            for t, p in prices.items():
                df = hist.get(t)
                if df is not None and len(df) > 5:
                    p["chg_1w"] = round(float(df["Close"].iloc[-1] / df["Close"].iloc[-6] - 1), 4)
            regime = read_regime(hist, cfg)
            tk = sorted({x["ticker"] for x in triggers if x["ticker"] in held} or held)
            news = nw.company_news(tk, asof, 3, 6, warnings)
            event_out, _ = run_decision("event", state, cfg, rules, hist, prices, fx, regime, None, news, {}, [],
                                        [], asof, last_session.isoformat(), month, warnings, False, triggers)

    if not trades and not triggers and not warnings and not events:
        log("quiet day — values recorded, nothing else to do")
        save_json(STATE_PATH, state)
        return 0

    # Refresh the dashboard's portfolio section (full refresh happens on Saturday)
    dash = load_json(DOCS_DATA / "dashboard.json", default={}) or {}
    if dash:
        closes, _ = md.latest_prices(sorted(set(state["books"]["portfolio"]["holdings"]) | {cfg["benchmark"]}),
                                     hist, 7, asof)
        md.fill_stale(set(state["books"]["portfolio"]["holdings"]), closes, hist, dash.get("prices", {}))
        for t, p in closes.items():
            dash.setdefault("prices", {})[t] = {**dash.get("prices", {}).get(t, {}), **p}
        book = state["books"]["portfolio"]
        dash["books"] = state["books"]
        dash["weights"] = pf.weights(book, closes)
        dash["theses"] = {t: (h.get("thesis") or {}).get("thesis", "") for t, h in book["holdings"].items()}
        dash["about"] = {t: (h.get("thesis") or {}).get("about", "") for t, h in book["holdings"].items()}
        dash["sectors"] = {t: h.get("sector") for t, h in book["holdings"].items()}
        dash["pending_orders"] = state.get("pending_orders")
        dash["totals"] = state["totals"]
        dash["daily"] = {"date": last_session.isoformat(), "trades": trades, "triggers": triggers,
                         "event_decision": event_out, "warnings": warnings}
        if trades:
            dash["trades"], dash["last_execution"] = trades, state.get("last_execution")
        if event_out:
            dash["decision"] = event_out
        dash["credit_left_usd"] = credit_left(state, cfg)
        save_json(DOCS_DATA / "dashboard.json", dash)
    save_json(STATE_PATH, state)

    lines = [f"📅 <b>AI Paper Portfolio — รายวัน</b> {last_session.isoformat()}"]
    if trades:
        lines += ["", "🔄 <b>ซื้อขายแล้วที่ราคาเปิด</b>"]
        lines += [f"  {'ซื้อ' if t['side'] == 'buy' else 'ขาย'} {t['ticker']} ${t['usd']:,.0f} @ {t['price']:.2f}" for t in trades]
    if triggers:
        lines += ["", "🚨 <b>เหตุที่ต้องจับตา</b>"] + [f"  {esc(x['ticker'])}: {esc(x['detail'])}" for x in triggers]
        if friday_night:
            lines.append("  → ส่งให้ AI พิจารณาในรอบเช้าวันเสาร์")
    if event_out:
        st = event_out.get("status")
        lines += ["", f"🤖 <b>AI (ฉุกเฉิน):</b> {esc(event_out.get('summary_th') or st)}"]
        if st == "pending":
            lines.append("  → จะซื้อขายที่ราคาเปิดของวันทำการถัดไป")
    if warnings:
        lines += ["", "⚠️ " + esc("; ".join(warnings[:3]))]
    lines += ["", cfg["dashboard_url"]]
    telegram("\n".join(lines), dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
