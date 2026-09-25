"""Diagnose Telegram setup. Prints GitHub annotations (never the token itself).

Usage: python -m src.test_telegram
"""
from __future__ import annotations

import requests

from .common import env


def note(level: str, msg: str) -> None:
    print(f"::{level} title=Telegram::{msg}", flush=True)


def main() -> int:
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token:
        note("error", "ไม่พบ secret TELEGRAM_BOT_TOKEN")
        return 1
    if not chat:
        note("error", "ไม่พบ secret TELEGRAM_CHAT_ID")
        return 1
    if ":" not in token:
        note("error", "TELEGRAM_BOT_TOKEN รูปแบบผิด (ต้องมีเครื่องหมาย : เช่น 123456:ABC...)")
        return 1
    note("notice", f"chat id: ความยาว {len(chat)} ตัว, เป็นตัวเลข={chat.lstrip('-').isdigit()}")

    me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
    if not me.get("ok"):
        note("error", f"token ใช้ไม่ได้: {me.get('description')}")
        return 1
    note("notice", f"token ถูกต้อง — bot คือ @{me['result'].get('username')}")

    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": "✅ ทดสอบจาก AI Paper Portfolio — ถ้าเห็นข้อความนี้แปลว่าตั้งค่าถูกแล้ว"},
                      timeout=20).json()
    if not r.get("ok"):
        note("error", f"ส่งข้อความไม่ได้: {r.get('description')}")
        # Show which chats have messaged the bot, to find the correct id
        upd = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
        ids = {str(u.get('message', {}).get('chat', {}).get('id')) for u in upd.get("result", []) if u.get("message")}
        if ids:
            note("notice", "chat id ที่เคยทักหา bot: " + ", ".join(sorted(ids)))
        else:
            note("notice", "ยังไม่มีใครทักหา bot — เปิดแชทกับ bot แล้วกด Start ก่อน")
        return 1
    note("notice", "ส่งข้อความสำเร็จ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
