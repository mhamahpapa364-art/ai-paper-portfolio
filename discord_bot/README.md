# Discord: แจ้งเตือนพอร์ต + บอทจัดระเบียบ server

มี 2 ส่วนที่แยกกัน **ไม่ต้องทำทั้งสองอย่าง**

| ส่วน | ใช้ทำอะไร | ต้องมี bot token? | ต้องรันค้างไว้? |
|---|---|---|---|
| **A. Webhook** | ระบบพอร์ตส่งข่าวเข้า Discord (แทน Telegram) | ไม่ต้อง | ไม่ต้อง (GitHub Actions ส่งเอง) |
| **B. Organizer bot** | สร้างหมวด/ช่อง/ยศตามแม่แบบ, ตรวจความรก, แผงปุ่มรับยศ | ต้อง | ไม่ต้อง — รันครั้งเดียวตอนตั้งค่าแล้วปิดได้ |

ไม่มีส่วนไหนเรียก Claude API ถามตอบทำในแชท Claude

ใน Discord จะเห็นเป็น **บอท 2 ตัวแยกกัน** (ป้าย APP คนละชื่อ คนละรูป คนละสิทธิ์):
ตัวแจ้งเตือนโพสต์ได้แค่ช่องเดียว ตัวจัดการ server ไม่เกี่ยวกับการแจ้งเตือน
ตั้งชื่อ/รูป webhook ได้ที่ Edit Channel → Integrations → Webhooks (เช่น `AI Portfolio`)
และตั้งชื่อ/รูปของบอทจัดการได้ที่ Developer Portal → General Information (เช่น `Server Manager`)

## A. แจ้งเตือนพอร์ตเข้า Discord (Webhook)
1. ใน Discord คลิกขวาช่องที่จะรับข่าว (เช่น `#portfolio-alerts`) → **Edit Channel → Integrations → Webhooks → New Webhook** → **Copy Webhook URL**
2. GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**
   ชื่อ `DISCORD_WEBHOOK_URL` ค่า = URL ที่คัดลอก
3. แท็บ **Actions → Test Discord → Run workflow** ต้องมีข้อความทดสอบเด้งในช่อง
4. ถ้าเห็นข้อความแล้ว จะลบ secret `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` ทิ้งก็ได้ ระบบส่งไปทุกช่องทางที่ตั้งค่าไว้ ถ้าเหลือ Discord อย่างเดียวก็ส่งแค่ Discord

> URL ของ webhook = รหัสผ่านของช่องนั้น ใครมีก็โพสต์ได้ ห้ามโพสต์ในแชท/commit ถ้าหลุดให้ลบ webhook แล้วสร้างใหม่

## B. สร้างและรัน Organizer bot
### 1) สร้าง application และ token
1. เปิด https://discord.com/developers/applications → **New Application** ตั้งชื่อ
2. เมนู **Bot** → **Reset Token** → คัดลอกเก็บไว้ (เห็นครั้งเดียว) *ไม่ต้องเปิด Privileged Intents* ยกเว้นอยากใช้ข้อความต้อนรับ (ดูล่าง)

### 2) เชิญบอทเข้า server
1. เมนู **OAuth2 → URL Generator**
2. Scopes: ✅ `bot` ✅ `applications.commands`
3. Bot Permissions: ✅ Manage Roles ✅ Manage Channels ✅ View Channels ✅ Send Messages ✅ Embed Links
4. เปิด URL ที่ได้ด้านล่าง → เลือก server → Authorize
5. **สำคัญ:** Server Settings → Roles → ลากยศของบอทให้อยู่ **สูงกว่า** ยศที่มันต้องให้ (Member, Investor, Dev, Gamer)

### 3) รัน
```bash
pip install -r discord_bot/requirements.txt
export DISCORD_BOT_TOKEN='วาง token ที่นี่'
export DISCORD_GUILD_ID='id server'   # ไม่บังคับ: ให้คำสั่งขึ้นทันที (เปิด Developer Mode → คลิกขวาไอคอน server → Copy Server ID)
python -m discord_bot.bot
```

### 4) ใช้คำสั่งใน Discord (เฉพาะแอดมิน)
| คำสั่ง | ทำอะไร |
|---|---|
| `/setup` | **ดูตัวอย่าง**ว่าจะสร้างอะไร (ยังไม่สร้างจริง) |
| `/setup preview:False` | สร้างหมวด/ช่อง/ยศที่ขาดตาม `server_template.json` (ไม่ลบ ไม่ย้ายของเดิม รันซ้ำได้) |
| `/audit` | หมวดว่าง ช่องที่ไม่อยู่ในหมวด ช่องไม่มี topic ช่องเงียบเกิน N วัน |
| `/rolepanel` | โพสต์ปุ่มให้สมาชิกกดรับ/เอายศ (ใช้ได้เฉพาะยศที่ตั้ง `self_assignable` และไม่มีสิทธิ์สูง) |

### ปรับแม่แบบ
แก้ `server_template.json` (ยศ หมวด ช่อง topic read_only) แล้วรันบอทใหม่ มันตรวจความถูกต้องก่อนเริ่มและบอกถ้ามีผิด

### ข้อความต้อนรับ + ให้ยศ Member อัตโนมัติ (ไม่บังคับ)
เปิด **Server Members Intent** ใน Developer Portal → Bot → Privileged Gateway Intents แล้วรันด้วย `ENABLE_WELCOME=1`
ส่วนนี้ต้องให้บอทออนไลน์ตลอด ถ้ารันแค่ตั้งค่าครั้งเดียวไม่ต้องเปิด
