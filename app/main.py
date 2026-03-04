import os
from dotenv import load_dotenv

import httpx
from fastapi import FastAPI, Request

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it into .env")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


async def tg_send_message(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text
        })
        r.raise_for_status()
        return r.json()


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    # Обрабатываем только обычные сообщения (позже добавим фото/документы)
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")

    if not chat_id:
        # Это может быть callback_query или что-то ещё — просто игнорируем
        return {"ok": True}

    text = msg.get("text")
    if text:
        await tg_send_message(chat_id, f"✅ получено: {text}")
    else:
        await tg_send_message(chat_id, "✅ получено (не текст). Пришли текст или фото — скоро научусь обрабатывать 🙂")

    return {"ok": True}