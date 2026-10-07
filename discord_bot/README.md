# Discord: แจ้งเตือนพอร์ต + บอทจัดระเบียบ server

ใน Discord จะเห็นเป็น **2 ตัวแยกกัน** (ป้าย APP คนละชื่อ คนละรูป คนละสิทธิ์)

| | A. บอทพอร์ต (Webhook) | B. บอทจัดระเบียบ |
|---|---|---|
| หน้าที่ | แจ้งข่าว/สรุปพอร์ตใน `#portfolio-alerts` เท่านั้น | สร้าง/จัดยศ หมวด ห้องแชท ห้อง voice ใหม่ทั้ง server |
| ต้องมี bot token | ไม่ต้อง | ต้อง |
| ต้องออนไลน์ค้างไว้ | ไม่ต้อง (GitHub Actions ส่งเอง) | **ไม่ต้อง** รันตอนจัดระเบียบแล้วปิดได้ |
| เรียก Claude API | ไม่ | ไม่ |

ถาม-ตอบทำในแชท Claude ไม่ได้ทำผ่าน Discord การรับยศของสมาชิกใช้ **Discord Onboarding** (ฟีเจอร์ในตัว ไม่ต้องใช้บอท)

## โครงสร้างที่แม่แบบสร้างให้ (`server_template.json`)

**ยศ** (เรียงจากสูงไปต่ำ)

| ยศ | ทำอะไรได้ | ใครได้ |
|---|---|---|
| Admin | ทุกอย่าง (Administrator) | คุณ |
| Mod | ลบข้อความ, timeout/kick, ปิดเสียง/ย้ายคนใน voice, เปลี่ยนชื่อเล่น, ดู audit log (ไม่แก้ยศ/ช่อง/ตั้งค่า, แบนไม่ได้) | คุณแอดเอง |
| Member | พิมพ์ แนบไฟล์/ลิงก์ ใช้อีโมจิ สร้าง thread เข้า voice พูดได้ (ลบข้อความคนอื่น/@everyone ไม่ได้) | ทุกคนรับเองผ่าน Onboarding |
| Investor | ไม่มีสิทธิ์เพิ่ม ใช้เป็น "กุญแจ" ให้เห็นหมวด AI Portfolio | **คุณแอดเอง** |
| Gamer | แท็กสีเฉย ๆ | รับเองผ่าน Onboarding |

**ใครเห็นอะไร**
- ทุกคนเห็น: `👋 เริ่มต้นที่นี่` (welcome, rules อ่านอย่างเดียว)
- Member: 📣 ประกาศ · 💬 ทั่วไป · 🔊 ห้องคุย · 💼 ห้องทำงาน/เรียน · 🎮 ห้องเกม · 💤 AFK
- **Investor เท่านั้น** (+ Admin): 📈 AI Portfolio (`portfolio-alerts` อ่านอย่างเดียว, `portfolio-discussion`) คนที่ไม่มียศ Investor ไม่เห็นหมวดนี้เลย (Mod ก็ไม่เห็น ถ้าอยากให้เห็น ใส่ `"Mod"` ใน `visible_to` ของหมวดนั้น)
- Mod: 🛡️ ทีมงาน · 📦 Archive (ห้องเดิมที่ไม่อยู่ในแม่แบบ)

แก้ได้ที่ `server_template.json` บอทตรวจความถูกต้องก่อนเริ่มและบอกถ้าผิด

## ขั้นตอน (เรียงตามลำดับ)

### 1) สร้างบอท และเชิญเข้า server
1. https://discord.com/developers/applications → **New Application** (ตั้งชื่อเช่น `Server Manager`) → เมนู **Bot** → **Reset Token** แล้วคัดลอกเก็บไว้ (ไม่ต้องเปิด Privileged Intents)
2. **OAuth2 → URL Generator** → Scopes: ✅ `bot` ✅ `applications.commands`
3. Bot Permissions: เลือก ✅ **Administrator** (ตอนจัดระเบียบ) เพราะ Discord ไม่ยอมให้บอทตั้งสิทธิ์ให้ยศใดเกินกว่าที่บอทมีเอง (เช่น ยศ Admin ต้องใช้บอทที่เป็น Administrator) เสร็จแล้วถอดสิทธิ์นี้ออกจากยศของบอทได้
4. เปิด URL ที่ได้ → เลือก server → Authorize
5. Server Settings → Roles → **ลากยศของบอทขึ้นบนสุด** (ต้องสูงกว่ายศทุกยศที่มันจะลบ/แก้)

### 2) รันบอทแล้วจัด server ใหม่
```bash
pip install -r discord_bot/requirements.txt
export DISCORD_BOT_TOKEN='วาง token ที่นี่'
export DISCORD_GUILD_ID='id server'   # ไม่บังคับ แต่ให้คำสั่งขึ้นทันที (Developer Mode → คลิกขวาไอคอน server → Copy Server ID)
python -m discord_bot.bot
```
ใน Discord (เฉพาะแอดมิน):

| คำสั่ง | ทำอะไร |
|---|---|
| `/reorganize` | **ดูตัวอย่าง** ทุกอย่างที่จะเกิดขึ้น (ยังไม่ทำจริง) |
| `/reorganize preview:False confirm:RESET` | ทำจริง: สร้างยศ/หมวด/ห้องตามแม่แบบ, ย้ายห้องเดิมที่ตรงชื่อเข้าหมวดใหม่, **ย้ายห้องเดิมที่เหลือไป 📦 Archive**, ลบหมวดเดิมที่ว่าง, **ลบยศเดิมทั้งหมด** (สมาชิกเสียยศเหล่านั้นทันที ย้อนคืนไม่ได้) |
| `/setup` | แบบเบา: สร้างเฉพาะของที่ขาด ไม่แตะของเดิม |
| `/audit` | ตรวจความรก: หมวดว่าง ช่องไม่อยู่ในหมวด ช่องไม่มี topic ช่องเงียบ |

ข้อควรรู้ก่อนกดรันจริง
- **ไม่ลบห้องแชท/voice** ห้องเดิมถูกย้ายไป Archive ให้คุณลบเองทีหลัง ส่วนยศเดิมถูกลบจริง
- ยศของบอท/integration และยศ @everyone ไม่ถูกแตะ
- ห้อง rules/updates ที่ Discord ผูกกับโหมด Community จะไม่ถูกย้ายไป Archive
- คนที่เป็นแอดมินผ่านยศเดิม (ที่ไม่ใช่เจ้าของ server) จะเสียสิทธิ์ ส่วนคนที่สั่งคำสั่งจะได้ยศ Admin ใหม่ให้อัตโนมัติ
- สมาชิกทุกคนจะเห็นแค่ห้องในหมวดเริ่มต้นจนกว่าจะได้ Member
- คำสั่งอาจทำงานหลายสิบวินาที ผลลัพธ์จะแสดงทีละรายการ ถ้ามี ❌ ส่วนใหญ่แปลว่ายศบอทต่ำเกินไป ลากขึ้นแล้วรันซ้ำได้ (รันซ้ำปลอดภัย)

### 3) ตั้ง Discord Onboarding (ให้สมาชิกรับ Member เอง ไม่ต้องใช้บอท)
1. Server Settings → **Enable Community** (ต้องเลือกช่อง rules และช่องอัปเดตของ Discord ใช้ `#rules` และ `#announcements`)
2. Server Settings → **Onboarding** (ชื่อเมนูอาจต่างไปเล็กน้อยตามเวอร์ชัน Discord)
3. คำถามบังคับ: "รับทราบกฎของ server" ตัวเลือก "รับทราบ" → ให้ยศ **Member**
4. คำถามเสริม: "สนใจอะไร?" ตัวเลือก "เกม" → ยศ **Gamer** (ไม่ต้องใส่ Investor คุณแอดเอง)
5. สมาชิกเดิมเข้าไปตอบได้ที่ Channels & Roles (ด้านบนรายชื่อช่อง)

> Onboarding มีเงื่อนไขเรื่องช่อง default ที่ต้องเปิดให้ทุกคนเห็น ถ้า Discord ไม่ยอมให้เปิดเพราะมีช่องสาธารณะน้อยเกินไป ให้ตั้ง `visible_to` ของหมวด `💬 ทั่วไป` เป็น `[]` ในแม่แบบแล้วรัน `/reorganize` ซ้ำ

### 4) สร้าง webhook ของบอทพอร์ต (ทำหลังจัด server เสร็จ)
1. ที่ `#portfolio-alerts` → **Edit Channel → Integrations → Webhooks → New Webhook** ตั้งชื่อ `AI Portfolio` ใส่รูป → **Copy Webhook URL**
2. GitHub repo → **Settings → Secrets and variables → Actions** → secret ชื่อ `DISCORD_WEBHOOK_URL`
3. แท็บ **Actions → Test Discord → Run workflow** ต้องมีข้อความเด้งใน `#portfolio-alerts`
4. เห็นข้อความแล้ว ลบ secret `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` ได้ ระบบส่งไปทุกช่องทางที่ตั้งไว้ ถ้าเหลือ Discord อย่างเดียวก็ส่งแค่ Discord
5. เพิ่มยศ **Investor** ให้ตัวคุณเอง (Admin เห็นหมวดนี้อยู่แล้ว แต่ให้คนอื่นที่อยากดูต้องแอดยศนี้)

> URL ของ webhook = รหัสผ่านของช่องนั้น ใครมีก็โพสต์ได้ ห้ามโพสต์ในแชท/commit ถ้าหลุดให้ลบ webhook แล้วสร้างใหม่
> Token ของบอท = กุญแจทั้ง server ถ้าหลุดให้ **Reset Token** ทันที
