"""Success criteria for the AI portfolio manager, and progress against them.

Defined in config/settings.json -> "goals". These are *targets and soft limits the manager is told about*;
they never change the iron rules (rules.json) or the human-review triggers (manager.human_flags).
"""
from __future__ import annotations

from datetime import date

DEFAULTS = {
    "horizon_months": 12,
    "beat_benchmark": True,
    "beat_shadow": True,
    "max_drawdown": 0.15,
    "min_hit_rate": 0.5,
    "min_graded_reviews": 4,
}


def _status(ok: bool | None) -> str:
    return "unknown" if ok is None else ("on_track" if ok else "behind")


def evaluate(cfg: dict, returns: dict | None, drawdown: float | None, hit_rate: dict | None,
             inception: str | None, asof: date) -> dict | None:
    """Progress report: one row per goal with status on_track | behind | unknown. None before go-live."""
    if not returns or not inception:
        return None
    g = {**DEFAULTS, **(cfg.get("goals") or {})}
    elapsed = (asof - date.fromisoformat(inception)).days
    horizon_days = round(g["horizon_months"] * 30.4)
    rows = []
    if g["beat_benchmark"]:
        v = returns.get("vs_benchmark")
        rows.append({"id": "beat_benchmark", "label": "ชนะ VOO (หลังค่าธรรมเนียม)", "value": v,
                     "status": _status(None if v is None else v > 0)})
    if g["beat_shadow"]:
        v = returns.get("vs_shadow")
        # exactly 0 means "never traded" - no evidence either way, so don't claim success
        rows.append({"id": "beat_shadow", "label": "ชนะพอร์ตเงา (การซื้อขายสร้างมูลค่า)", "value": v,
                     "status": _status(None if v is None or v == 0 else v > 0)})
    if drawdown is not None:
        rows.append({"id": "max_drawdown", "label": f"ลงจากจุดสูงสุดไม่เกิน {g['max_drawdown']:.0%}", "value": drawdown,
                     "status": _status(drawdown > -g["max_drawdown"])})
    hr = (hit_rate or {})
    graded = (hr.get("correct") or 0) + (hr.get("wrong") or 0)
    ok = None if graded < g["min_graded_reviews"] or hr.get("hit_rate") is None else hr["hit_rate"] >= g["min_hit_rate"]
    rows.append({"id": "hit_rate", "label": f"thesis ถูก ≥ {g['min_hit_rate']:.0%} ของที่ตัดสินแล้ว",
                 "value": hr.get("hit_rate"), "status": _status(ok)})
    return {"day": elapsed, "horizon_days": horizon_days, "progress": min(1.0, elapsed / horizon_days),
            "goals": rows, "behind": [r["id"] for r in rows if r["status"] == "behind"]}


def prompt_text(report: dict | None, cfg: dict) -> str | None:
    """The block given to the decision model."""
    if not report:
        return None
    g = {**DEFAULTS, **(cfg.get("goals") or {})}
    return ("Success means, after fees and measured in THB over %d months: beat VOO AND beat the never-trading shadow, "
            "with drawdown from peak no worse than -%d%%, and at least %d%% of graded theses correct. "
            "These are your own scorecard, not extra rules: holding is still the default, and being 'behind the shadow' "
            "is a reason to ask whether trading earns its fee — never a reason to trade for its own sake. "
            "A drawdown near the limit is a reason to review risk and cash, in your journal.\nPROGRESS: "
            % (g["horizon_months"], round(g["max_drawdown"] * 100), round(g["min_hit_rate"] * 100))
            + ", ".join(f"{r['id']}={r['status']}" for r in report["goals"]) + f" (day {report['day']})")


def telegram_lines(report: dict | None) -> list[str]:
    if not report:
        return []
    icon = {"on_track": "✅", "behind": "⚠️", "unknown": "⏳"}
    return ["", f"🎯 <b>เป้าหมาย</b> (วันที่ {report['day']}/{report['horizon_days']})"] + \
           [f"{icon[r['status']]} {r['label']}" for r in report["goals"]]
