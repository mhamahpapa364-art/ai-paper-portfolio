"""News (Finnhub), earnings calendar (Finnhub) and official filings (SEC EDGAR).

News failures are non-fatal: they are recorded as warnings and the run continues.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import requests

from .common import env, log

FINNHUB = "https://finnhub.io/api/v1"


def _finnhub(path: str, params: dict):
    key = env("FINNHUB_API_KEY")
    if not key:
        raise RuntimeError("FINNHUB_API_KEY not set")
    r = requests.get(f"{FINNHUB}{path}", params={**params, "token": key}, timeout=25)
    if r.status_code == 429:
        time.sleep(5)
        r = requests.get(f"{FINNHUB}{path}", params={**params, "token": key}, timeout=25)
    r.raise_for_status()
    return r.json()


def company_news(tickers: list[str], asof: date, lookback: int, per_ticker: int, warnings: list) -> dict:
    out = {}
    frm = (asof - timedelta(days=lookback)).isoformat()
    for t in tickers:
        try:
            items = _finnhub("/company-news", {"symbol": t, "from": frm, "to": asof.isoformat()})
        except Exception as e:
            warnings.append(f"ข่าว {t}: {e}")
            continue
        seen, rows = set(), []
        for it in sorted(items or [], key=lambda x: x.get("datetime", 0), reverse=True):
            head = (it.get("headline") or "").strip()
            if not head or head.lower() in seen:
                continue
            seen.add(head.lower())
            rows.append({
                "headline": head[:220],
                "summary": (it.get("summary") or "").strip()[:400],
                "source": it.get("source", ""),
                "url": it.get("url", ""),
                "date": datetime.fromtimestamp(it.get("datetime", 0), tz=timezone.utc).date().isoformat(),
            })
            if len(rows) >= per_ticker:
                break
        out[t] = rows
        time.sleep(0.3)
    return out


def earnings_calendar(tickers: list[str], asof: date, lookahead: int, warnings: list) -> list[dict]:
    # Query per symbol: the unfiltered calendar on the free tier is truncated
    rows = []
    for t in tickers:
        try:
            j = _finnhub("/calendar/earnings", {"symbol": t, "from": asof.isoformat(),
                                                "to": (asof + timedelta(days=lookahead)).isoformat()})
        except Exception as e:
            warnings.append(f"ปฏิทินงบ {t}: {e}")
            continue
        rows += [{"ticker": t, "date": e.get("date"), "hour": e.get("hour", "")}
                 for e in (j or {}).get("earningsCalendar", []) if e.get("symbol") == t]
        time.sleep(0.3)
    return sorted(rows, key=lambda r: r["date"] or "")


def company_profiles(tickers: list[str], warnings: list) -> dict:
    """Name + industry per ticker (ETFs return nothing on the free tier)."""
    out = {}
    for t in tickers:
        try:
            p = _finnhub("/stock/profile2", {"symbol": t})
        except Exception as e:
            warnings.append(f"ข้อมูลบริษัท {t}: {e}")
            continue
        if p and p.get("name"):
            out[t] = {"name": p["name"], "industry": p.get("finnhubIndustry", ""),
                      "country": p.get("country", ""), "logo": p.get("logo", "")}
        time.sleep(0.3)
    return out


# ---------- SEC EDGAR ----------
_CIK_CACHE: dict[str, str] | None = None


def _sec_headers() -> dict:
    # SEC requires a User-Agent that includes a contact email, e.g. "AI Paper Portfolio you@example.com"
    ua = env("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError("ยังไม่ได้ตั้ง secret SEC_USER_AGENT (SEC ต้องการอีเมลติดต่อ)")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def _cik_map() -> dict[str, str]:
    global _CIK_CACHE
    if _CIK_CACHE is None:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=_sec_headers(), timeout=30)
        r.raise_for_status()
        _CIK_CACHE = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in r.json().values()}
    return _CIK_CACHE


def edgar_filings(tickers: list[str], asof: date, lookback: int, forms: list[str], warnings: list) -> dict:
    out = {}
    try:
        ciks = _cik_map()
    except Exception as e:
        warnings.append(f"EDGAR: {e}")
        return out
    since = asof - timedelta(days=lookback)
    for t in tickers:
        cik = ciks.get(t.upper())
        if not cik:
            continue  # ETFs etc.
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=_sec_headers(), timeout=30)
            r.raise_for_status()
            rec = r.json()["filings"]["recent"]
        except Exception as e:
            warnings.append(f"EDGAR {t}: {e}")
            continue
        rows = []
        for form, fdate, acc, doc, items in zip(rec["form"], rec["filingDate"], rec["accessionNumber"],
                                                rec["primaryDocument"], rec.get("items", [""] * len(rec["form"]))):
            if form not in forms or date.fromisoformat(fdate) < since:
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
            rows.append({"form": form, "date": fdate, "items": items, "url": url})
        if rows:
            out[t] = rows[:8]
        time.sleep(0.15)  # SEC fair-access: <=10 req/s
    return out


# 8-K item codes → Thai labels (the ones that matter most)
ITEM_LABELS = {
    "1.01": "สัญญาสำคัญ", "1.02": "ยกเลิกสัญญาสำคัญ", "2.01": "ซื้อ/ขายกิจการ", "2.02": "ผลประกอบการ",
    "2.05": "ปรับโครงสร้าง/ปลดคน", "2.06": "ด้อยค่าสินทรัพย์", "3.01": "ถูกเตือนเรื่องจดทะเบียน",
    "4.02": "แก้งบย้อนหลัง", "5.02": "เปลี่ยนผู้บริหาร/กรรมการ", "7.01": "Reg FD", "8.01": "เหตุการณ์อื่น",
}


def label_items(items: str) -> str:
    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    return ", ".join(ITEM_LABELS.get(c, c) for c in codes if c != "9.01")
