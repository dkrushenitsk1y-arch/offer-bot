import json
import logging
import os
import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

import httpx
import pytesseract
from openai import OpenAI
from fastapi import FastAPI, Request, Header, HTTPException, status, BackgroundTasks
from PIL import Image
from pypdf import PdfReader

env_path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://.../telegram/webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # random string

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

TESSERACT_CMD = os.getenv("TESSERACT_CMD")

if not TESSERACT_CMD:
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            TESSERACT_CMD = c
            break

if not TESSERACT_CMD:
    TESSERACT_CMD = shutil.which("tesseract")

try:
    import pytesseract

    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
except Exception:
    pass

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it into .env")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "inbox"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = Path(__file__).resolve().parents[1] / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("freight_bot")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _log_path = LOG_DIR / "bot.log"
    _handler = RotatingFileHandler(
        _log_path,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.propagate = False

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/config")
def debug_config():
    return {
        "webhook_url": WEBHOOK_URL,
        "has_bot_token": bool(BOT_TOKEN),
        "has_webhook_secret": bool(WEBHOOK_SECRET),
        "tesseract_cmd": TESSERACT_CMD,
    }


async def tg_send_message(chat_id: int, text: str):
    logger.info("tg_send_message chat_id=%s text_len=%s", chat_id, len(text or ""))
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        r.raise_for_status()
        return r.json()


async def tg_api_post(method: str, payload: dict | None = None):
    logger.info("tg_api_post method=%s", method)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{TELEGRAM_API}/{method}",
                json=payload or {},
            )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API request error: {type(e).__name__}: {str(e)}",
        )

    body_text = r.text

    if r.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API returned {r.status_code}. Body: {body_text}",
        )

    try:
        return r.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API returned non-JSON body: {body_text}",
        )


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = re.sub(r"_+", "_", name)
    return name[:200] or "file"


def save_update_json(update: dict) -> Path:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = LOG_DIR / f"update_{ts}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(update, f, ensure_ascii=False, indent=2)
    return path


async def tg_get_file_info(file_id: str) -> dict:
    data = await tg_api_post("getFile", {"file_id": file_id})

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API returned unexpected response for getFile: {data}",
        )

    if not data.get("ok", False):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API ok=false for getFile. Response: {data}",
        )

    result = data.get("result") or {}
    if not isinstance(result, dict) or not result.get("file_path"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API getFile missing file_path. Response: {data}",
        )

    return result


async def tg_download_file(file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram file download error: {type(e).__name__}: {str(e)}",
        )

    if r.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram file download returned {r.status_code}. Body: {r.text}",
        )

    return r.content


async def save_telegram_file(file_id: str, preferred_name: str | None) -> Path:
    info = await tg_get_file_info(file_id)
    file_path = info["file_path"]

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_unique_id = info.get("file_unique_id") or file_id

    if preferred_name:
        filename = sanitize_filename(preferred_name)
    else:
        suffix = Path(file_path).suffix
        filename = f"file_{ts}_{file_unique_id}{suffix or '.bin'}"

    dest = DATA_DIR / filename
    content = await tg_download_file(file_path)
    dest.write_bytes(content)
    logger.info("File saved: %s", str(dest))
    return dest


def extract_text_from_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    return "\n".join(parts)


async def extract_text_from_pdf_async(path: Path) -> str:
    import anyio

    return await anyio.to_thread.run_sync(extract_text_from_pdf, path)


def extract_text_from_image(path: Path) -> str:
    with Image.open(path) as img:
        return pytesseract.image_to_string(img)


async def extract_text_from_image_async(path: Path) -> str:
    import anyio

    return await anyio.to_thread.run_sync(extract_text_from_image, path)


def preview_text(text: str, limit: int = 1000) -> str:
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "..."


def parse_offer_fields(text: str) -> dict:
    t = (text or "").replace("\u00a0", " ")
    t_compact = re.sub(r"\s+", " ", t).strip()

    lines = [l.strip() for l in t.splitlines() if l.strip()]
    lower_lines = [l.lower() for l in lines]
    fields: dict[str, str | None] = {
        "loading": None,
        "unloading": None,
        "route": None,
        "timing": None,
        "date": None,
        "weight": None,
        "truck": None,
        "price": None,
        "notes": None,
    }

    # OCR examples (unit-style):
    # - "CZ-783 66 Hlubocky >>> FR-41500 MER"
    # - "CZ-783 66 Hlubocky >> FR-41500 MER"
    # - "CZ-783 66 Hlubocky -> FR-41500 MER"
    # - "FR-41500" + "MER" split -> should become "FR-41500 MER"
    route_match = re.search(r"(.+?)\s*(>>>|->|>>)\s*(.+)", t_compact)
    if route_match:
        left = route_match.group(1).strip()
        sep = route_match.group(2)
        right = route_match.group(3).strip()

        # If unloading is like "FR-41500" and OCR put the city right after as a separate token,
        # ensure it is appended.
        m_right = re.match(r"^(?P<code>[A-Z]{2}-\d{3,6})(?:\s+(?P<city>[A-Z]{2,10}))?(?P<rest>\b.*)?$", right)
        if m_right and not m_right.group("city"):
            after = t_compact[route_match.end() :].strip()
            m_after = re.match(r"^(?P<city>[A-Z]{2,10})\b", after)
            if m_after:
                right = f"{m_right.group('code')} {m_after.group('city')}" + (m_right.group("rest") or "")

        fields["route"] = f"{left} {sep} {right}".strip()
    else:
        for l in lines:
            m = re.search(r"(.+?)\s*(>>>|->|>>)\s*(.+)", l)
            if m:
                fields["route"] = f"{m.group(1).strip()} {m.group(2)} {m.group(3).strip()}"
                break

    for idx, ll in enumerate(lower_lines):
        if any(k in ll for k in ["loading", "pickup", "collection", "załadunek", "zaladunek"]):
            fields["loading"] = lines[idx]
            break

    for idx, ll in enumerate(lower_lines):
        if any(k in ll for k in ["unloading", "delivery", "rozładunek", "rozladunek"]):
            fields["unloading"] = lines[idx]
            break

    timing_hint_patterns = [
        r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:tomorrow|today)\b",
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    ]
    for idx, ll in enumerate(lower_lines):
        if any(re.search(p, ll, flags=re.IGNORECASE) for p in timing_hint_patterns):
            if any(k in ll for k in ["loading", "pickup", "collection", "delivery", "unloading", "załadunek", "zaladunek", "rozładunek", "rozladunek"]):
                fields["timing"] = lines[idx]
                break

    date_patterns = [
        r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:tomorrow|today)\b",
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    ]
    joined = t_compact
    for pat in date_patterns:
        m = re.search(pat, joined, flags=re.IGNORECASE)
        if m:
            fields["date"] = m.group(0)
            break

    m = re.search(r"\b(\d{1,3}(?:[\s.,]\d{3})*(?:[\s.,]\d+)?)\s*kg\b", joined, flags=re.IGNORECASE)
    if m:
        fields["weight"] = re.sub(r"\s+", " ", m.group(0)).strip()
    else:
        m = re.search(r"\b(\d{1,2}(?:[.,]\d+)?)\s*t\b", joined, flags=re.IGNORECASE)
        if m:
            fields["weight"] = m.group(0)

    truck_keywords = [
        "tautliner",
        "curtain",
        "standard",
        "mega",
        "reefer",
        "frigo",
        "van",
        "bus",
        "ftl",
        "ltl",
    ]
    for kw in truck_keywords:
        if re.search(rf"\b{re.escape(kw)}\b", joined, flags=re.IGNORECASE):
            fields["truck"] = kw.upper() if kw in {"ftl", "ltl"} else kw
            break

    # OCR examples (unit-style):
    # - "2 400 €" -> "2400€"
    # - "2400EUR" -> "2400 EUR"
    # - "2,400€"  -> "2400€"
    # - "Price 1350" -> "1350" (fallback)
    price_pattern = re.compile(
        r"(?i)\b(\d{2,6}(?:[.,]\d{1,2})?)\s*(€|eur|euro|pln|zl|zł)\b"
    )
    price_matches = list(price_pattern.finditer(t_compact))
    if price_matches:
        m_last = price_matches[-1]
        amount_raw = m_last.group(1)
        currency_raw = m_last.group(2)
        amount_clean = amount_raw.replace(",", ".")
        currency_clean = currency_raw.upper() if currency_raw.lower() in {"eur", "euro", "pln", "zl", "zł"} else currency_raw
        if currency_raw.lower() in {"eur", "euro"}:
            fields["price"] = f"{amount_clean} EUR"
        elif currency_raw.lower() in {"pln"}:
            fields["price"] = f"{amount_clean} PLN"
        elif currency_raw.lower() in {"zl", "zł"}:
            fields["price"] = f"{amount_clean} ZŁ"
        else:
            fields["price"] = f"{amount_clean} {currency_clean}".strip()
    else:
        m = re.search(r"\bprice\s*(\d+(?:[\s.,]\d{3})*(?:[.,]\d+)?)\b", t_compact, flags=re.IGNORECASE)
        if m:
            fields["price"] = re.sub(r"[\s,]", "", m.group(1))

    note_keywords = [
        "no change",
        "adr",
        "temperature",
        "temp",
        "reefer",
        "frigo",
    ]
    note_lines: list[str] = []
    for l in lines:
        ll = l.lower()
        if any(k in ll for k in note_keywords):
            note_lines.append(l)
    if note_lines:
        fields["notes"] = " | ".join(note_lines)

    return fields


def format_parsed(fields: dict) -> str:
    def v(key: str) -> str:
        val = fields.get(key)
        return str(val) if val else "-"

    return "\n".join(
        [
            f"- Loading: {v('loading')}",
            f"- Unloading: {v('unloading')}",
            f"- Route: {v('route')}",
            f"- Date: {v('date')}",
            f"- Truck: {v('truck')}",
            f"- Weight: {v('weight')}",
            f"- Price: {v('price')}",
            f"- Notes: {v('notes')}",
        ]
    )


def format_clean_offer(details: dict) -> str:
    def clean_place(line: str | None) -> str:
        s = (line or "").strip()
        if not s:
            return ""
        s = re.sub(
            r"^\s*(loading|unloading|delivery|pickup|collection|za[łl]adunek|roz[łl]adunek)\s*[:\-–—]*\s*",
            "",
            s,
            flags=re.IGNORECASE,
        ).strip()
        if ":" in s:
            tail = s.split(":", 1)[1].strip()
            return tail or s
        return s

    def normalize_route(route: str | None) -> str:
        r = (route or "").strip()
        if not r:
            return ""
        m = re.search(r"(.+?)\s*(>>>|->|>>)\s*(.+)", r)
        if m:
            return f"{m.group(1).strip()} >>> {m.group(3).strip()}"
        return r

    def normalize_price(price: str | None) -> str:
        s = (price or "").strip()
        if not s:
            return "-"
        if re.search(r"€|\beur\b|eur", s, flags=re.IGNORECASE) or re.search(r"\b[A-Z]{3}\b", s):
            return s
        s_num = re.sub(r"\s+", "", s)
        if re.fullmatch(r"\d+(?:[.,]\d+)?", s_num):
            if "," in s_num and "." not in s_num:
                s_num = s_num.replace(",", ".")
            return f"{s_num}€"
        return s

    def normalize_loading_line(date_val: str | None) -> str:
        s = (date_val or "").strip()
        if not s or s == "-":
            return "-"
        if re.match(r"^\s*loading\b", s, flags=re.IGNORECASE):
            return re.sub(
                r"^\s*loading\s*[:\-–—]*\s*",
                "Loading ",
                s,
                flags=re.IGNORECASE,
            ).strip()
        return f"Loading {s}"

    route = normalize_route(details.get("route"))
    if not route:
        a = clean_place(details.get("loading"))
        b = clean_place(details.get("unloading"))
        if a and b:
            route = f"{a} >>> {b}"
        else:
            route = "-"

    loading_line = normalize_loading_line(details.get("date") or details.get("timing"))

    truck = str(details.get("truck") or "").strip()
    weight = str(details.get("weight") or "").strip()
    if truck and weight:
        truck_weight_line = f"{truck} {weight}"
    elif truck:
        truck_weight_line = truck
    elif weight:
        truck_weight_line = weight
    else:
        truck_weight_line = "-"

    notes_line = str(details.get("notes") or "").strip()
    if not notes_line or notes_line == "-":
        notes_line = "No change"

    price_line = normalize_price(str(details.get("price")) if details.get("price") else None)

    return "\n".join([route, loading_line, truck_weight_line, notes_line, price_line])


def format_offer(fields: dict) -> str:
    def v(key: str) -> str:
        val = fields.get(key)
        return str(val) if val else "-"

    return (
        "Hello,\n\n"
        "Thank you for your request. Please find our offer below:\n\n"
        f"Loading: {v('loading')}\n"
        f"Unloading: {v('unloading')}\n"
        f"Loading date: {v('date')}\n"
        f"Truck: {v('truck')}\n"
        f"Weight: {v('weight')}\n"
        f"Price: {v('price')}\n\n"
        "If ok, I can book the truck.\n\n"
        "Best regards,\n"
        "Dima"
    )


OPENAI_SYSTEM_PROMPT = """
You are a logistics assistant. Extract freight offer details from messy text (OCR/PDF/email).
Return ONLY JSON that matches the schema. If missing, use null. Keep values short.
Rules:
- route: "AAA >>> BBB" when possible.
- loading/unloading: locations, ideally "CC-POSTCODE City" if present.
- date: keep as-is (tomorrow, 04.03, Monday, etc.)
- truck: short (FTL/LTL/tautliner/mega/reefer/van, etc.)
- weight: keep as-is (12t, 24000 kg, etc.)
- price: keep as-is with currency (2400€, 1350 EUR, etc.)
- notes: brief constraints (no change, ADR, no reefer, etc.)
"""

OPENAI_JSON_SCHEMA = {
    "name": "freight_offer",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "loading": {"type": ["string", "null"]},
            "unloading": {"type": ["string", "null"]},
            "date": {"type": ["string", "null"]},
            "truck": {"type": ["string", "null"]},
            "weight": {"type": ["string", "null"]},
            "price": {"type": ["string", "null"]},
            "notes": {"type": ["string", "null"]},
            "route": {"type": ["string", "null"]},
        },
        "required": ["loading", "unloading", "date", "truck", "weight", "price", "notes", "route"],
    },
}


def needs_ai(fields: dict) -> bool:
    def _has(value) -> bool:
        if value is None:
            return False
        s = str(value).strip()
        if not s:
            return False
        if s == "-":
            return False
        return True

    # зовём AI только если regex не вытянул ключевые поля
    if not _has(fields.get("route")):
        return True
    if not _has(fields.get("price")):
        return True
    if not (_has(fields.get("loading")) and _has(fields.get("unloading"))):
        return True
    return False


async def openai_parse_offer(text: str) -> dict:
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY is missing")

    import anyio

    def _call():
        resp = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": [{"type": "text", "text": OPENAI_SYSTEM_PROMPT}]},
                {"role": "user", "content": [{"type": "text", "text": text}]},
            ],
            response_format={"type": "json_schema", "json_schema": OPENAI_JSON_SCHEMA},
            timeout=20,  # DEBUG TEMP
        )
        raw = getattr(resp, "output_text", None)
        if not raw:
            raise RuntimeError("OpenAI returned empty output_text")
        return raw

    raw_json = await anyio.to_thread.run_sync(_call)
    return json.loads(raw_json)


async def handle_text(chat_id: int, text: str, source: str):
    fields = parse_offer_fields(text)

    logger.info(
        "handle_text source=%s route=%s price=%s loading=%s unloading=%s",
        source,
        fields.get("route"),
        fields.get("price"),
        bool((fields.get("loading") or "").strip()),
        bool((fields.get("unloading") or "").strip()),
    )

    logger.info("OpenAI TEMP DISABLED")

    logger.info("Sending final offer to chat_id=%s", chat_id)
    await tg_send_message(chat_id, "✅ Offer:\n" + format_clean_offer(fields))


async def send_text_extraction_preview(chat_id: int, path: Path):
    await tg_send_message(chat_id, "📄 Saved. Reading text...")

    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            logger.info("PDF extraction started: %s", str(path))
            text = await extract_text_from_pdf_async(path)
            logger.info("PDF extraction finished: %s chars=%s", str(path), len(text or ""))
        elif suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            logger.info("OCR started: %s", str(path))
            text = await extract_text_from_image_async(path)
            logger.info("OCR finished: %s chars=%s", str(path), len(text or ""))
        else:
            await tg_send_message(chat_id, f"⚠️ Format not supported yet: {suffix or 'unknown'}")
            return
    except Exception as e:
        logger.exception("Text extraction failed: %s", str(path))
        await tg_send_message(chat_id, f"⚠️ Error extracting text: {type(e).__name__}: {str(e)}")
        return

    preview = preview_text(text)
    if not preview:
        await tg_send_message(
            chat_id,
            "⚠️ I could not extract text (maybe scanned/low quality).",
        )
        return

    await tg_send_message(chat_id, "🧾 Extracted text preview:\n\n" + preview)
    await handle_text(chat_id, text, source="pdf" if suffix == ".pdf" else "ocr")


async def process_telegram_update(update: dict):
    logger.info("process_telegram_update start")
    try:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")

        if not chat_id:
            logger.info("process_telegram_update: no chat_id")
            return

        text = msg.get("text")

        # Небольшая логика /start
        if text == "/start":
            await tg_send_message(
                chat_id,
                "Привет! Я Freight Offer Bot 🚛\n"
                "Пока что умею отвечать на текст. Скоро добавлю фото/документы и генерацию офферов 🙂",
            )
            return

        if text:
            if text.startswith("/"):
                await tg_send_message(chat_id, f"✅ получено: {text}")
            else:
                await handle_text(chat_id, text, source="message")
            return

        photos = msg.get("photo")
        if isinstance(photos, list) and photos:
            photo = photos[-1] or {}
            file_id = photo.get("file_id")
            file_unique_id = photo.get("file_unique_id")
            if file_id:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                preferred_name = f"photo_{ts}_{file_unique_id or file_id}.jpg"
                saved_path = await save_telegram_file(file_id, preferred_name)
                logger.info("File saved: %s", str(saved_path))
                size_kb = int(saved_path.stat().st_size / 1024)
                await tg_send_message(
                    chat_id,
                    f"✅ Saved file: {saved_path} ({size_kb} KB)",
                )

                await tg_send_message(chat_id, f"📄 Saved. Reading text: {saved_path.name}")
                suffix = saved_path.suffix.lower()

                try:
                    if suffix == ".pdf":
                        logger.info("PDF extraction started: %s", str(saved_path))
                        extracted = await extract_text_from_pdf_async(saved_path)
                        logger.info("PDF extraction finished: %s chars=%s", str(saved_path), len(extracted or ""))
                        source = "pdf"
                    elif suffix in [".jpg", ".jpeg", ".png", ".webp"]:
                        logger.info("OCR started: %s", str(saved_path))
                        extracted = await extract_text_from_image_async(saved_path)
                        logger.info("OCR finished: %s chars=%s", str(saved_path), len(extracted or ""))
                        source = "ocr"
                    else:
                        await tg_send_message(chat_id, f"⚠️ Unsupported file type: {suffix}")
                        return

                    preview = preview_text(extracted, limit=1000)
                    if preview.strip():
                        await tg_send_message(chat_id, "🧾 Extracted text preview:\n\n" + preview)
                    else:
                        await tg_send_message(
                            chat_id,
                            "⚠️ No text extracted (maybe scanned / low quality).",
                        )

                    if (extracted or "").strip():
                        await handle_text(chat_id, extracted, source=source)

                except Exception as e:
                    err = f"{type(e).__name__}: {str(e)}"
                    await tg_send_message(chat_id, "⚠️ Extraction error:\n" + err)
                    logger.exception("EXTRACTION ERROR: %s", err)

            return

        document = msg.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            file_id = document["file_id"]
            file_name = document.get("file_name")
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            if file_name:
                preferred_name = f"{ts}_{file_name}"
            else:
                preferred_name = None

            saved_path = await save_telegram_file(file_id, preferred_name)
            logger.info("File saved: %s", str(saved_path))
            size_kb = int(saved_path.stat().st_size / 1024)
            await tg_send_message(chat_id, f"✅ Saved file: {saved_path} ({size_kb} KB)")

            await tg_send_message(chat_id, f"📄 Saved. Reading text: {saved_path.name}")
            suffix = saved_path.suffix.lower()

            try:
                if suffix == ".pdf":
                    logger.info("PDF extraction started: %s", str(saved_path))
                    extracted = await extract_text_from_pdf_async(saved_path)
                    logger.info("PDF extraction finished: %s chars=%s", str(saved_path), len(extracted or ""))
                    source = "pdf"
                elif suffix in [".jpg", ".jpeg", ".png", ".webp"]:
                    logger.info("OCR started: %s", str(saved_path))
                    extracted = await extract_text_from_image_async(saved_path)
                    logger.info("OCR finished: %s chars=%s", str(saved_path), len(extracted or ""))
                    source = "ocr"
                else:
                    await tg_send_message(chat_id, f"⚠️ Unsupported file type: {suffix}")
                    return

                preview = preview_text(extracted, limit=1000)
                if preview.strip():
                    await tg_send_message(chat_id, "🧾 Extracted text preview:\n\n" + preview)
                else:
                    await tg_send_message(
                        chat_id,
                        "⚠️ No text extracted (maybe scanned / low quality).",
                    )

                if (extracted or "").strip():
                    await handle_text(chat_id, extracted, source=source)

            except Exception as e:
                err = f"{type(e).__name__}: {str(e)}"
                await tg_send_message(chat_id, "⚠️ Extraction error:\n" + err)
                logger.exception("EXTRACTION ERROR: %s", err)

            return

        await tg_send_message(chat_id, "Received non-text message")

    except Exception:
        logger.exception("process_telegram_update failed")


async def tg_set_webhook():
    """
    Устанавливает webhook в Telegram.
    Важно: WEBHOOK_URL должен быть публичным HTTPS.
    """
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL is missing in .env")

    payload = {"url": WEBHOOK_URL}

    # Защитный секрет (Telegram будет присылать его в заголовке)
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{TELEGRAM_API}/setWebhook",
                json=payload,
            )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API request error: {type(e).__name__}: {str(e)}",
        )

    body_text = r.text

    if r.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API returned {r.status_code}. Body: {body_text}",
        )

    try:
        data = r.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API returned non-JSON body: {body_text}",
        )

    if not data.get("ok", False):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API ok=false. Response: {data}",
        )

    return data


@app.post("/telegram/set-webhook")
async def set_webhook():
    """
    Ручной endpoint: открыл в браузере/Swagger → set webhook.
    """
    result = await tg_set_webhook()
    return {"ok": True, "result": result, "webhook_url": WEBHOOK_URL}


@app.post("/telegram/get-me")
async def telegram_get_me():
    return await tg_api_post("getMe", {})


@app.post("/telegram/webhook-info")
async def telegram_webhook_info():
    return await tg_api_post("getWebhookInfo", {})


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Проверяем секретный заголовок (если задан WEBHOOK_SECRET)
    if WEBHOOK_SECRET:
        if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret token")
    try:
        update = await request.json()
    except Exception:
        logger.exception("Failed to parse Telegram update JSON")
        return {"ok": True}

    if not isinstance(update, dict):
        logger.info("Telegram update is not a dict: %s", type(update).__name__)
        return {"ok": True}

    try:
        path = save_update_json(update)
        logger.info("Saved update JSON: %s", str(path))
    except Exception:
        logger.exception("Failed to save update JSON")

    try:
        background_tasks.add_task(process_telegram_update, update)
        logger.info("Scheduled process_telegram_update")
    except Exception:
        logger.exception("Failed to schedule background task")

    return {"ok": True}