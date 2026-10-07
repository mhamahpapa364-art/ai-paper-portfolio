"""Shared helpers: paths, JSON I/O, logging, notifications (Discord + Telegram)."""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
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


DISCORD_WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
DISCORD_LIMIT = 1900  # hard limit is 2000 chars per message; keep a margin


def html_to_discord(text: str) -> str:
    """Convert the Telegram-style HTML used by our messages into Discord markdown."""
    t = re.sub(r"<a\s+href=[\"']([^\"']+)[\"']\s*>(.*?)</a>", r"[\2](\1)", text, flags=re.S)
    t = re.sub(r"</?(b|strong)>", "**", t)
    t = re.sub(r"</?(i|em)>", "*", t)
    t = re.sub(r"</?u>", "__", t)
    t = re.sub(r"</?(s|strike|del)>", "~~", t)
    t = re.sub(r"</?pre>", "```", t)
    t = re.sub(r"</?code>", "`", t)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries so no chunk exceeds Discord's message limit."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single over-long line
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


def discord(text: str, dry_run: bool = False) -> bool:
    """Send a message to a Discord webhook. Never raises. Accepts Telegram-style HTML."""
    url = env("DISCORD_WEBHOOK_URL")
    body = html_to_discord(text)
    if dry_run:
        log("[dry-run] Discord message:\n" + body)
        return True
    if not url:
        log("Discord webhook missing — message not sent")
        return False
    if not url.startswith(DISCORD_WEBHOOK_PREFIXES):
        log("DISCORD_WEBHOOK_URL is not a Discord webhook URL — message not sent")
        return False
    try:
        for chunk in split_message(body):
            for attempt in range(2):
                r = requests.post(url, json={"content": chunk, "allowed_mentions": {"parse": []}},
                                  timeout=20)
                if r.status_code == 429 and attempt == 0:  # rate limited: wait once and retry
                    try:
                        wait = float(r.json().get("retry_after", 1))
                    except (ValueError, AttributeError):
                        wait = 1.0
                    time.sleep(min(wait, 10))
                    continue
                break
            if r.status_code not in (200, 204):
                log(f"Discord error {r.status_code}: {r.text[:200]}")
                return False
            time.sleep(0.3)  # stay clear of the per-webhook rate limit
        return True
    except requests.RequestException as e:
        log(f"Discord request failed: {e}")
        return False


def notify(text: str, dry_run: bool = False) -> bool:
    """Send to every configured channel (Discord webhook and/or Telegram). Never raises.

    Returns True if at least one channel accepted the message.
    """
    if dry_run:
        log("[dry-run] Notification:\n" + text)
        return True
    results = []
    if env("DISCORD_WEBHOOK_URL"):
        results.append(discord(text))
    if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
        results.append(telegram(text))
    if not results:
        log("No notification channel configured (set DISCORD_WEBHOOK_URL or Telegram secrets)")
        return False
    return any(results)


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def fail(msg: str, dry_run: bool = False) -> None:
    """Alert and exit non-zero so the workflow does NOT commit anything."""
    log("FATAL: " + msg)
    if notify(f"⚠️ <b>AI Paper Portfolio — รันไม่สำเร็จ</b>\n{esc(msg)}\n\nไม่มีการบันทึกข้อมูลรอบนี้", dry_run):
        ALERT_MARKER.write_text(msg)  # tells the workflow not to send a second alert
    sys.exit(1)


ALERT_MARKER = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "ai_portfolio_alert_sent"
