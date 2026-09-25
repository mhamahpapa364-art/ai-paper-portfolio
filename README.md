# AI Paper Portfolio

พอร์ตจำลอง 100,000 บาท ที่ AI บริหารเองทั้งหมด รันอัตโนมัติทุกวันเสาร์ด้วย GitHub Actions

**Dashboard:** https://mhamahpapa364-art.github.io/ai-paper-portfolio/

## ระบบทำอะไรทุกสัปดาห์
1. ดึงราคาจาก yfinance (ถ้าดึงไม่ได้ ใช้ Finnhub แทน) ถ้าราคาหาย ผิดปกติ หรือเก่าเกิน 5 วัน → **หยุดทันที ไม่บันทึกอะไร** แล้วแจ้งเตือนทาง Telegram
2. บันทึกปันผล (หักภาษี 15%) และการแตกพาร์
3. คำนวณผลตอบแทน 3 พอร์ต: **พอร์ต AI** · **VOO** (ใช้เป็นคู่เทียบ) · **พอร์ตเงา** (ถือหุ้นชุดแรกไว้เฉยๆ ไม่แตะเลย)
4. แยกผลตอบแทนเป็น 2 ส่วน: ส่วนที่มาจากหุ้น (USD) และส่วนที่มาจากค่าเงิน
5. อ่านสภาพตลาด (risk-on / neutral / risk-off) จาก VOO เทียบเส้น 200 วัน, VIX, พันธบัตร 10 ปี และหมวดอุตสาหกรรมที่นำตลาด
6. ข่าวจาก Finnhub + เอกสารทางการจาก SEC EDGAR + ปฏิทินประกาศงบ
7. Claude Haiku สรุปข่าวเป็นภาษาไทย (เนื้อหาข่าวถือเป็นข้อมูลเท่านั้น AI จะไม่ทำตามคำสั่งที่แฝงมาในข่าว)
8. บันทึกผลลง `data/` แล้ว commit (ประวัติ git = log การตัดสินใจอัตโนมัติ) → อัปเดต dashboard → ส่งสรุปทาง Telegram

## โครงสร้างไฟล์
| ไฟล์ | หน้าที่ |
|---|---|
| `config/settings.json` | ค่าตั้งระบบ (ค่าธรรมเนียม 0.25%, ภาษีปันผล, watchlist) |
| `config/rules.json` | กฎของ AI: iron (แก้ไม่ได้) / flexible (AI ปรับได้เดือนละครั้ง) |
| `data/portfolio-state.json` | ข้อมูลหลักของระบบ (single source of truth) |
| `data/history.json` | มูลค่าพอร์ตรายสัปดาห์ |
| `data/weekly/*.json` | บันทึกเต็มของแต่ละรอบ |
| `docs/` | หน้า dashboard (GitHub Pages) |
| `src/` | โค้ดทั้งหมด |

## สถานะ
- **Part 1** (ข้อมูล + dashboard + Telegram): เสร็จแล้ว ตอนนี้อยู่ในโหมด `pre-live` ติดตามรายชื่อหุ้นทดสอบ ยังไม่มีการลงทุน
- **Part 2** (AI ตัดสินใจซื้อขาย + journal + lessons): ยังไม่เริ่ม → go-live หลัง Part 2 เสร็จ

## รันด้วยมือ
แท็บ **Actions** → **Weekly run** → **Run workflow** (ติ๊ก dry run ถ้าแค่ต้องการทดสอบ)

## Secrets ที่ต้องมี
`FINNHUB_API_KEY` · `ANTHROPIC_API_KEY` · `TELEGRAM_BOT_TOKEN` · `TELEGRAM_CHAT_ID`
