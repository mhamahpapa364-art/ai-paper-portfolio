"""Shared helpers: paths, JSON I/O, logging, Telegram."""
from __future__ import annotations

import html
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
DOCS_DATA = ROOT / "docs" / "data"
STATE_PATH = DATA / "portfolio-state.json"
HISTORY_PATH = DATA / "history.json"
WEEKLY_DIR = DATA / "weekly"


class DataError(RuntimeError):
    """Critical data problem — the run must stop and nothing may be committed."""


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    tmp.replace(path)


def settings() -> dict:
    return load_json(CONFIG / "settings.json")


def rules() -> dict:
    return load_json(CONFIG / "rules.json")


def today() -> date:
    return datetime.now(timezone.utc).date()


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def telegram(text: str, dry_run: bool = False) -> bool:
    """Send a Telegram message (HTML). Never raises — notification failure must not break a run."""
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if dry_run:
        log("[dry-run] Telegram message:\n" + text)
        return True
    if not token or not chat:
        log("Telegram secrets missing — message not sent")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"Telegram request failed: {e}")
        return False


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def fail(msg: str, dry_run: bool = False) -> None:
    """Alert and exit non-zero so the workflow does NOT commit anything."""
    log("FATAL: " + msg)
    if telegram(f"⚠️ <b>AI Paper Portfolio — รันไม่สำเร็จ</b>\n{esc(msg)}\n\nไม่มีการบันทึกข้อมูลรอบนี้", dry_run):
        ALERT_MARKER.write_text(msg)  # tells the workflow not to send a second alert
    sys.exit(1)


ALERT_MARKER = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "ai_portfolio_alert_sent"
