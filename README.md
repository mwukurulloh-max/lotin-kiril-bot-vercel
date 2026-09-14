# Ikki Alifbo — Telegram bot (Vercel versiyasi)

Bu papka Telegram botni **Vercel**da (bepul, kredit karta shart emas) doimiy
ishlaydigan qilib joylashtirish uchun mo'ljallangan.

## Nima uchun bu boshqacha?

Oddiy Python botlar (`python-telegram-bot` bilan) doimiy "tinglab" turadigan
jarayon (polling) sifatida ishlaydi — bunga doimiy ishlaydigan server (VPS)
kerak. Vercel esa **serverless** — funksiya faqat so'rov kelganda ishga
tushadi. Shuning uchun bu versiya "webhook" usulida ishlaydi: Telegram har
safar yangi xabar kelganda Vercel'dagi manzilga so'rov yuboradi, funksiya
uyg'onib, javob qaytaradi va yana "uxlaydi". Bu — bepul tarifga mos, ishonchli
usul.

**Cheklov:** oldingi (polling) versiyadagi menyu tugmalari (Lotin→Kirill /
Kirill→Lotin / Avtomatik) olib tashlangan, chunki serverless funksiyalar
xotirani saqlamaydi. O'rniga har bir xabar **avtomatik aniqlash** orqali
ishlanadi — bu 99% holatda to'g'ri natija beradi.

## Joylashtirish qadamlari

1. Bu papkadagi fayllarni (`api/webhook.py`, `vercel.json`) GitHub'da yangi
   repository'ga yuklang (masalan `lotin-kiril-bot-vercel`).
2. [vercel.com](https://vercel.com) ga GitHub akkauntingiz bilan kiring.
3. **"Add New" → "Project"** → GitHub repository'ingizni tanlang → **"Import"**.
4. **"Environment Variables"** bo'limida:
   - Key: `BOT_TOKEN`
   - Value: @BotFather'dan olgan tokeningiz
5. **"Deploy"** tugmasini bosing.
6. Deploy tugagach, sizga havola beriladi, masalan:
   ```
   https://lotin-kiril-bot-vercel.vercel.app
   ```
7. Botga webhook o'rnatish uchun, brauzerda **aynan shu manzilni** oching
   (o'z tokeningiz va Vercel havolangiz bilan almashtirib):
   ```
   https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<VERCEL_URL>/api/webhook
   ```
   Muvaffaqiyatli bo'lsa, brauzerda `{"ok":true,"result":true,...}` chiqadi.

Shu bilan tamom — botingiz endi kompyuteringiz o'chiq bo'lsa ham, internet
bilan bog'liq mahalliy cheklovlardan mustaqil ravishda ishlayveradi.

## Tekshirish

Telegram'da botga `/start` yuboring — javob kelsa, hammasi tayyor.
