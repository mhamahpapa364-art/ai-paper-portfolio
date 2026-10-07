"""Diagnose Discord webhook setup. Prints GitHub annotations (never the webhook URL itself).

Usage: python -m src.test_discord
"""
from __future__ import annotations

import sys

from .common import DISCORD_WEBHOOK_PREFIXES, discord, env


def note(level: str, msg: str) -> None:
    print(f"::{level} title=Discord::{msg}", flush=True)


def main() -> int:
    url = env("DISCORD_WEBHOOK_URL")
    if not url:
        note("error", "ไม่พบ secret DISCORD_WEBHOOK_URL")
        return 1
    if not url.startswith(DISCORD_WEBHOOK_PREFIXES):
        note("error", "DISCORD_WEBHOOK_URL รูปแบบผิด (ต้องขึ้นต้นด้วย https://discord.com/api/webhooks/)")
        return 1
    if not discord("✅ <b>AI Paper Portfolio</b>: ทดสอบการแจ้งเตือน Discord สำเร็จ"):
        note("error", "ส่งไม่สำเร็จ — ตรวจว่า webhook ยังไม่ถูกลบ และ URL คัดลอกมาครบ")
        return 1
    note("notice", "ส่งข้อความทดสอบสำเร็จ — ดูในช่อง Discord ที่ผูก webhook ไว้")
    return 0


if __name__ == "__main__":
    sys.exit(main())
