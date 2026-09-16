"""Telegram bot webhook handler for Vercel serverless deployment.

Bu fayl bitta o'zida quyidagilarni o'z ichiga oladi:
    * O'zbek lotin<->kirill konvertatsiya mantig'i (converter.py bilan bir xil).
    * Telegram webhook so'rovlarini qabul qiluvchi HTTP handler.
    * PDF, DOCX va TXT fayllardan matn ajratib olish va natijani fayl
      sifatida qaytarish.

Arxitektura tanlovi: bu bot HOLATSIZ (stateless) ishlaydi — foydalanuvchi
tanlagan "rejim" saqlanmaydi, chunki serverless funksiyalar har safar yangi
"process" sifatida ishga tushadi va xotira saqlanmaydi. Shu sababli, har bir
xabar uchun avtomatik aniqlash (auto-detect) ishlatiladi: matn lotinchami
yoki kirillchami, buni o'zi aniqlab, mos yo'nalishda konvertatsiya qiladi.
Bu cheklov ataylab tanlangan — muqobili (tashqi ma'lumotlar bazasi orqali
holatni saqlash) keraksiz murakkablik qo'shган bo'lardi.
"""

from __future__ import annotations

import io
import json
import os
import re
from http.server import BaseHTTPRequestHandler

import requests
from pypdf import PdfReader
from docx import Document

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

MAX_CHUNK_LENGTH = 3800

#: Telegram bot API orqali yuklab olish mumkin bo'lgan maksimal fayl hajmi
#: (Telegram'ning o'zi 20 MB dan katta fayllarni bot orqali yuklashga
#: ruxsat bermaydi; biz xavfsizlik uchun bir oz kichikroq chegara qo'yamiz).
MAX_FILE_SIZE_BYTES = 18 * 1024 * 1024

#: Qo'llab-quvvatlanadigan fayl kengaytmalari.
SUPPORTED_EXTENSIONS = (".txt", ".pdf", ".docx")


# ---------------------------------------------------------------------------
# Konvertatsiya mantig'i (converter.py bilan bir xil)
# ---------------------------------------------------------------------------

APOSTROPHE_VARIANTS = ("'", "ʻ", "‘", "’", "ʼ", "`", "‛", "ʹ", "ˊ")
CANONICAL_APOSTROPHE = "ʻ"

_APOSTROPHE_CLASS = "".join(re.escape(ch) for ch in APOSTROPHE_VARIANTS)
_APOSTROPHE_NORMALIZE_RE = re.compile(rf"([oOgG])[{_APOSTROPHE_CLASS}]")


def _normalize_apostrophes(text: str) -> str:
    return _APOSTROPHE_NORMALIZE_RE.sub(lambda m: m.group(1) + CANONICAL_APOSTROPHE, text)


LAT_TO_CYR_DIGRAPHS = {
    "sh": "ш", "ch": "ч", "yo": "ё", "yu": "ю", "ya": "я", "ye": "е", "ts": "ц",
    f"o{CANONICAL_APOSTROPHE}": "ў",
    f"g{CANONICAL_APOSTROPHE}": "ғ",
}

LAT_TO_CYR_SINGLE = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "ҳ", "i": "и", "j": "ж", "k": "к", "l": "л", "m": "м",
    "n": "н", "o": "о", "p": "п", "q": "қ", "r": "р", "s": "с",
    "t": "т", "u": "у", "v": "в", "x": "х", "y": "й", "z": "з",
}

LATIN_VOWELS = frozenset("aeiou")
LATIN_WORD_RE = re.compile(rf"[A-Za-z{re.escape(CANONICAL_APOSTROPHE)}]+")


def _convert_word_lat_to_cyr(word: str) -> str:
    result = []
    i = 0
    length = len(word)
    while i < length:
        two = word[i:i + 2]
        if two in LAT_TO_CYR_DIGRAPHS:
            result.append(LAT_TO_CYR_DIGRAPHS[two])
            i += 2
            continue
        ch = word[i]
        if ch == "e":
            is_start_or_after_vowel = i == 0 or word[i - 1] in LATIN_VOWELS
            result.append("э" if is_start_or_after_vowel else "е")
        else:
            result.append(LAT_TO_CYR_SINGLE.get(ch, ch))
        i += 1
    return "".join(result)


CYR_TO_LAT_SINGLE = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ҳ": "h", "қ": "q", "ч": "ch",
    "ш": "sh", "щ": "sh", "ы": "i",
    "ғ": f"g{CANONICAL_APOSTROPHE}",
    "ў": f"o{CANONICAL_APOSTROPHE}",
    "ъ": CANONICAL_APOSTROPHE,
    "ь": "",
    "э": "e",
}

CYRILLIC_VOWELS = frozenset("аеёиоуюяэ")
_YE_TRIGGER_AFTER = CYRILLIC_VOWELS | frozenset("ъь")
CYRILLIC_WORD_RE = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳ]+")


def _convert_word_cyr_to_lat(word: str) -> str:
    result = []
    for i, ch in enumerate(word):
        if ch == "е":
            is_start_or_after_trigger = i == 0 or word[i - 1] in _YE_TRIGGER_AFTER
            result.append("ye" if is_start_or_after_trigger else "e")
        elif ch == "ё":
            result.append("yo")
        elif ch == "ю":
            result.append("yu")
        elif ch == "я":
            result.append("ya")
        elif ch == "ц":
            result.append("ts")
        else:
            result.append(CYR_TO_LAT_SINGLE.get(ch, ch))
    return "".join(result)


def _detect_case_pattern(word: str) -> str:
    cased_chars = [c for c in word if c.isupper() or c.islower()]
    if not cased_chars:
        return "lower"
    if all(c.isupper() for c in cased_chars):
        return "upper"
    if cased_chars[0].isupper() and all(c.islower() for c in cased_chars[1:]):
        return "title"
    return "lower"


def _apply_case(word: str, pattern: str) -> str:
    if pattern == "upper":
        return word.upper()
    if pattern == "title":
        for i, c in enumerate(word):
            if c.isalpha():
                return word[:i] + word[i].upper() + word[i + 1:]
        return word
    return word


def convert_latin_to_cyrillic(text: str) -> str:
    if not text:
        return text
    normalized = _normalize_apostrophes(text)

    def _replace(match: re.Match) -> str:
        word = match.group(0)
        pattern = _detect_case_pattern(word)
        converted = _convert_word_lat_to_cyr(word.lower())
        return _apply_case(converted, pattern)

    return LATIN_WORD_RE.sub(_replace, normalized)


def convert_cyrillic_to_latin(text: str) -> str:
    if not text:
        return text

    def _replace(match: re.Match) -> str:
        word = match.group(0)
        pattern = _detect_case_pattern(word)
        converted = _convert_word_cyr_to_lat(word.lower())
        return _apply_case(converted, pattern)

    return CYRILLIC_WORD_RE.sub(_replace, text)


def detect_script(text: str) -> str:
    cyrillic_count = sum(1 for c in text if c.lower() in "абвгдежзийклмнопрстуфхцчшщъыьэюяёўқғҳ")
    latin_count = sum(1 for c in text if c.isalpha() and c.lower() in "abcdefghijklmnopqrstuvwxyz")
    if cyrillic_count == 0 and latin_count == 0:
        return "unknown"
    return "cyrillic" if cyrillic_count >= latin_count else "latin"


def auto_convert(text: str) -> str:
    script = detect_script(text)
    if script == "latin":
        return convert_latin_to_cyrillic(text)
    if script == "cyrillic":
        return convert_cyrillic_to_latin(text)
    return text


# ---------------------------------------------------------------------------
# Telegram bilan aloqa
# ---------------------------------------------------------------------------

WELCOME_TEXT = (
    "Assalomu alaykum! 👋\n\n"
    "Men o'zbek tilidagi matnlarni *lotin* va *kirill* yozuvlari o'rtasida "
    "avtomatik aylantiruvchi botman.\n\n"
    "Menga istalgan matnni yuboring — lotinchami yoki kirillchami, o'zim "
    "aniqlab, qarama-qarshi yozuvga o'tkazib beraman.\n\n"
    "📎 Fayl ham yuborishingiz mumkin: *.txt*, *.pdf*, *.docx* — men ichidagi "
    "matnni o'qib, konvertatsiya qilib, tayyor faylni qaytaraman.\n\n"
    "🌐 Veb-saytimiz ham bor: https://mwukurulloh-max.github.io/ikki-alifbo/\n\n"
    "Yordam uchun /help yuboring."
)

HELP_TEXT = (
    "*Bot qanday ishlaydi:*\n\n"
    "1️⃣ Menga matn yuboring — avtomatik ravishda lotin↔kirill aylantirib beraman.\n\n"
    "2️⃣ Yoki fayl yuboring (*.txt*, *.pdf*, *.docx*) — men ichidagi matnni "
    "o'qib, konvertatsiya qilib, *natija.txt* fayli sifatida qaytaraman.\n\n"
    "Raqamlar, tinish belgilari, emoji va boshqa til so'zlari o'zgarishsiz "
    "qoladi.\n\n"
    "*Fayllar bo'yicha eslatmalar:*\n"
    "• Eski *.doc* formati (Word 2003) qo'llab-quvvatlanmaydi — avval "
    "*.docx*ga saqlang.\n"
    "• Skanerlangan (rasm sifatidagi) PDF fayllardan matn chiqarib bo'lmaydi.\n"
    "• Fayl hajmi 18 MB dan oshmasligi kerak.\n\n"
    "🌐 Veb-saytimiz: https://mwukurulloh-max.github.io/ikki-alifbo/"
)

EMPTY_TEXT_WARNING = "Matn bo'sh ko'rinadi. Iltimos, konvertatsiya qilish uchun biror matn yuboring."
UNSUPPORTED_FORMAT_WARNING = (
    "Bu fayl formatini o'qiy olmayman. Faqat *.txt*, *.pdf* va *.docx* "
    "fayllarni qo'llab-quvvatlayman."
)
OLD_DOC_WARNING = (
    "Eski *.doc* formati (Word 2003 va undan oldingi) qo'llab-quvvatlanmaydi. "
    "Iltimos, faylni Word orqali *.docx* formatida qayta saqlang, yoki "
    "matnni to'g'ridan-to'g'ri menga yuboring."
)
FILE_TOO_LARGE_WARNING = "Fayl juda katta (18 MB dan oshmasligi kerak). Kichikroq fayl bilan urinib ko'ring."
NO_TEXT_IN_FILE_WARNING = (
    "Fayldan matn topa olmadim. Fayl bo'sh yoki faqat rasmlardan iborat "
    "(skanerlangan) bo'lishi mumkin."
)
FILE_PROCESSING_ERROR = (
    "Kechirasiz, faylni qayta ishlashda xatolik yuz berdi. Iltimos, faylni "
    "tekshirib, qaytadan urinib ko'ring."
)


def send_message(chat_id: int, text: str) -> None:
    """Telegram Bot API orqali xabar yuboradi, uzun matnni bo'laklarga bo'lib."""
    if not text:
        text = EMPTY_TEXT_WARNING

    chunks = [text[i:i + MAX_CHUNK_LENGTH] for i in range(0, len(text), MAX_CHUNK_LENGTH)] or [text]

    for chunk in chunks:
        try:
            resp = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                timeout=10,
            )
            if not resp.ok:
                raise ValueError("Markdown bilan yuborib bo'lmadi")
        except Exception:
            # Markdown parse xatosi bo'lishi mumkin (masalan maxsus belgilar) —
            # oddiy matn sifatida qayta yuborib ko'ramiz.
            try:
                requests.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk},
                    timeout=10,
                )
            except Exception:
                pass


def send_document(chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
    """Telegram Bot API orqali fayl (masalan natija.txt) yuboradi."""
    try:
        requests.post(
            f"{TELEGRAM_API}/sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1000]},
            files={"document": (filename, content, "text/plain")},
            timeout=20,
        )
    except Exception:
        send_message(chat_id, "Natija faylini yuborib bo'lmadi. Iltimos, qaytadan urinib ko'ring.")


def download_telegram_file(file_id: str) -> bytes | None:
    """Telegram'ga yuklangan faylni ikki bosqichda (getFile + yuklab olish) oladi."""
    file_info = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=15)
    file_info.raise_for_status()
    file_path = file_info.json()["result"]["file_path"]
    file_resp = requests.get(f"{TELEGRAM_FILE_API}/{file_path}", timeout=30)
    file_resp.raise_for_status()
    return file_resp.content


def extract_text_from_txt(data: bytes) -> str:
    """TXT fayldan matnni turli kodlashlarni sinab o'qiydi."""
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def extract_text_from_pdf(data: bytes) -> str:
    """PDF fayldagi barcha sahifalardan matnni ajratib oladi."""
    reader = PdfReader(io.BytesIO(data))
    pages_text = []
    for page in reader.pages:
        pages_text.append(page.extract_text() or "")
    return "\n\n".join(pages_text).strip()


def extract_text_from_docx(data: bytes) -> str:
    """DOCX fayldagi paragraflardan matnni ajratib oladi."""
    document = Document(io.BytesIO(data))
    paragraphs = [p.text for p in document.paragraphs]
    return "\n".join(paragraphs).strip()


def handle_document(chat_id: int, document: dict) -> None:
    """Yuborilgan faylni yuklab oladi, matnini konvertatsiya qilib, natijani qaytaradi."""
    file_name = document.get("file_name") or "fayl"
    file_size = document.get("file_size") or 0
    file_id = document.get("file_id")

    if file_size and file_size > MAX_FILE_SIZE_BYTES:
        send_message(chat_id, FILE_TOO_LARGE_WARNING)
        return

    lower_name = file_name.lower()
    ext = "." + lower_name.rsplit(".", 1)[-1] if "." in lower_name else ""

    if ext == ".doc":
        send_message(chat_id, OLD_DOC_WARNING)
        return

    if ext not in SUPPORTED_EXTENSIONS:
        send_message(chat_id, UNSUPPORTED_FORMAT_WARNING)
        return

    try:
        data = download_telegram_file(file_id)
    except Exception:
        send_message(chat_id, FILE_PROCESSING_ERROR)
        return

    try:
        if ext == ".txt":
            text = extract_text_from_txt(data)
        elif ext == ".pdf":
            text = extract_text_from_pdf(data)
        elif ext == ".docx":
            text = extract_text_from_docx(data)
        else:
            text = ""
    except Exception:
        send_message(chat_id, FILE_PROCESSING_ERROR)
        return

    if not text or not text.strip():
        send_message(chat_id, NO_TEXT_IN_FILE_WARNING)
        return

    result = auto_convert(text)
    preview = result[:180] + ("…" if len(result) > 180 else "")
    send_document(chat_id, "natija.txt", result.encode("utf-8"), caption=f"✅ Tayyor!\n\n{preview}")


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime shu klassni HTTP so'rovlarga ishlov berish uchun chaqiradi."""

    def do_GET(self) -> None:
        """Oddiy tekshiruv uchun: brauzerda ochilsa, botning ishlab turganini ko'rsatadi."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot webhook ishlab turibdi ✅".encode("utf-8"))

    def do_POST(self) -> None:
        """Telegram'dan kelgan yangilanishlarni (update) qabul qiladi va javob beradi."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            update = json.loads(body.decode("utf-8"))

            message = update.get("message") or update.get("edited_message")
            if message:
                chat_id = message["chat"]["id"]
                document = message.get("document")

                if document:
                    handle_document(chat_id, document)
                else:
                    text = (message.get("text") or "").strip()
                    if text == "/start":
                        send_message(chat_id, WELCOME_TEXT)
                    elif text == "/help":
                        send_message(chat_id, HELP_TEXT)
                    elif text:
                        result = auto_convert(text)
                        send_message(chat_id, result)
                    else:
                        send_message(chat_id, EMPTY_TEXT_WARNING)
        except Exception:
            # Bot hech qachon qulab tushmasligi kerak — xato bo'lsa ham
            # Telegram'ga 200 OK qaytaramiz, aks holda Telegram qayta-qayta
            # urinib, keraksiz yuklama hosil qiladi.
            pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')
