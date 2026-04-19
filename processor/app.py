import base64, json, logging, os, re
import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("processor")

app = FastAPI()
ai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

ZAMMAD_BASE  = os.environ["ZAMMAD_BASE_URL"].rstrip("/")
ZAMMAD_TOKEN = os.environ["ZAMMAD_API_TOKEN"]
ZAMMAD_GROUP = os.getenv("ZAMMAD_GROUP", "managers")
FALLBACK_EMAIL = os.getenv("ZAMMAD_FALLBACK_CUSTOMER_EMAIL", "leadbot@aaa-wb.by")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

EXTRACT_SYSTEM = """Ты — ассистент менеджера по продажам на выставке.
Извлеки из текста данные лида. Верни ТОЛЬКО JSON без markdown:
{"name":"...","company":"...","phone":"...","email":"...","position":"...","comment":"..."}
Телефон в международном формате (+7... или +375...). Если поле не найдено — пустая строка."""

CARD_PROMPT = """Это фото визитки. Извлеки все данные.
Верни ТОЛЬКО JSON без markdown:
{"name":"...","company":"...","phone":"...","email":"...","position":"..."}
Телефон в международном формате. Если поля нет — пустая строка."""


def parse_json(raw: str) -> dict:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {}


def guess_mime(filename: str, fallback: str) -> str:
    low = (filename or "").lower()
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".webp"):
        return "image/webp"
    if low.endswith(".oga") or low.endswith(".ogg"):
        return "audio/ogg"
    if low.endswith(".webm"):
        return "audio/webm"
    if low.endswith(".mp3"):
        return "audio/mpeg"
    if low.endswith(".m4a") or low.endswith(".mp4"):
        return "audio/mp4"
    return fallback


async def zammad_ticket(
    lead: dict,
    attachment_name: str = "",
    attachment_mime: str = "",
    attachment_data: bytes | None = None,
) -> dict:
    lines = []
    for key, label in [("name","Имя"),("company","Компания"),("phone","Телефон"),
                       ("email","Email"),("position","Должность"),
                       ("source","Источник"),("comment","Комментарий")]:
        val = lead.get(key, "")
        if val:
            lines.append(f"{label}: {val}")

    company = lead.get("company") or "?"
    name    = lead.get("name")    or "?"
    customer = lead.get("email") or FALLBACK_EMAIL

    payload = {
        "title": f"Лид: {company} / {name}",
        "group": ZAMMAD_GROUP,
        "customer_id": "guess:" + customer,
        "article": {
            "subject": "Новый лид с выставки",
            "body": "\n".join(lines),
            "type": "note",
            "internal": False,
        },
    }

    if attachment_data:
        payload["article"]["attachments"] = [{
            "filename": attachment_name or "voice.ogg",
            "mime-type": attachment_mime or "audio/ogg",
            "data": base64.b64encode(attachment_data).decode("ascii"),
        }]

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{ZAMMAD_BASE}/api/v1/tickets",
            headers={"Authorization": f"Token token={ZAMMAD_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if not r.is_success and attachment_data:
            # Some Zammad setups can reject attachment metadata; retry without attachment to avoid losing the lead.
            log.warning("Zammad rejected ticket with attachment (%s): %s. Retrying without attachment.", r.status_code, r.text)
            payload["article"].pop("attachments", None)
            r = await c.post(
                f"{ZAMMAD_BASE}/api/v1/tickets",
                headers={"Authorization": f"Token token={ZAMMAD_TOKEN}",
                         "Content-Type": "application/json"},
                json=payload,
            )
        if not r.is_success:
            log.error("Zammad error %s: %s", r.status_code, r.text)
            r.raise_for_status()
        return r.json()


async def extract_card_lead(data: bytes, mime: str) -> dict:
    b64 = base64.b64encode(data).decode()
    resp = await ai.chat.completions.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": CARD_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        max_tokens=400,
    )
    raw = resp.choices[0].message.content or ""
    return parse_json(raw)


async def extract_voice_lead(data: bytes, fname: str, mime: str) -> tuple[dict, str]:
    tr = await ai.audio.transcriptions.create(
        model="whisper-1",
        file=(fname, data, mime),
        language="ru",
    )
    text = tr.text
    log.info("transcript: %s", text)

    resp = await ai.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": text},
        ],
        max_tokens=400,
    )
    raw = resp.choices[0].message.content or ""
    lead = parse_json(raw)
    if not lead.get("comment"):
        lead["comment"] = text
    return lead, text


async def create_ticket_from_card(data: bytes, mime: str, source: str, comment: str = "", notify: bool = True) -> tuple[dict, int | None]:
    log.info("card: %s bytes, type=%s", len(data), mime)
    lead = await extract_card_lead(data, mime)
    lead.setdefault("source", source)
    if comment:
        lead["comment"] = comment
    log.info("card lead: %s", lead)

    ticket = await zammad_ticket(lead)
    ticket_id = ticket.get("id")
    if notify:
        await send_telegram_notice(ticket_id, lead, source)
    return lead, ticket_id


async def create_ticket_from_voice(data: bytes, fname: str, mime: str, source: str, notify: bool = True) -> tuple[dict, int | None, str]:
    log.info("voice: %s bytes, type=%s", len(data), mime)
    lead, text = await extract_voice_lead(data, fname, mime)
    lead.setdefault("source", source)
    log.info("voice lead: %s", lead)

    ticket = await zammad_ticket(
        lead,
        attachment_name=fname,
        attachment_mime=mime,
        attachment_data=data,
    )
    ticket_id = ticket.get("id")
    if notify:
        await send_telegram_notice(ticket_id, lead, source)
    return lead, ticket_id, text


async def telegram_api(method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", json=payload or {})
        if not r.is_success:
            log.error("Telegram %s error %s: %s", method, r.status_code, r.text)
            return {}
        return r.json()


async def telegram_send_message(chat_id: str | int, text: str) -> None:
    await telegram_api("sendMessage", {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    })


async def telegram_download_file(file_id: str, fallback_name: str, fallback_mime: str) -> tuple[bytes, str, str]:
    meta = await telegram_api("getFile", {"file_id": file_id})
    result = meta.get("result") or {}
    path = result.get("file_path")
    if not path:
        raise RuntimeError("Telegram getFile returned empty file_path")

    filename = path.split("/")[-1] or fallback_name
    mime = guess_mime(filename, fallback_mime)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}"

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        if not r.is_success:
            raise RuntimeError(f"Telegram file download failed: HTTP {r.status_code}")
        return r.content, filename, mime


async def send_telegram_notice(ticket_id: int | None, lead: dict, source: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return

    chat_id = TELEGRAM_CHAT_ID
    if not chat_id:
        chat_id = await discover_telegram_chat_id()
    if not chat_id:
        log.warning("Telegram chat_id is unknown. Send any message to the bot first.")
        return

    text = (
        "Новый лид\n"
        f"Источник: {source}\n"
        f"Тикет: #{ticket_id or '-'}\n"
        f"Имя: {lead.get('name') or '-'}\n"
        f"Компания: {lead.get('company') or '-'}\n"
        f"Телефон: {lead.get('phone') or '-'}\n"
        f"Email: {lead.get('email') or '-'}"
    )

    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        if not r.is_success:
            log.error("Telegram error %s: %s", r.status_code, r.text)


async def discover_telegram_chat_id() -> str:
    global TELEGRAM_CHAT_ID

    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates")
        if not r.is_success:
            log.error("Telegram getUpdates error %s: %s", r.status_code, r.text)
            return ""

        payload = r.json()
        items = payload.get("result") or []
        for item in reversed(items):
            msg = item.get("message") or item.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid is not None:
                TELEGRAM_CHAT_ID = str(cid)
                return TELEGRAM_CHAT_ID
    return ""


@app.get("/healthz")
def health():
    return {"ok": True}


@app.post("/webhook/card")
async def card_intake(
    file: UploadFile = File(...),
    comment: str = Form(""),
):
    data = await file.read()
    mime = file.content_type or "image/jpeg"
    lead, ticket_id = await create_ticket_from_card(
        data=data,
        mime=mime,
        source="Выставка / визитка",
        comment=comment,
        notify=True,
    )
    return {"ok": True, "ticket_id": ticket_id, "lead": lead}


@app.post("/webhook/voice")
async def voice_intake(file: UploadFile = File(...)):
    data = await file.read()
    fname = file.filename or "voice.webm"
    mime = file.content_type or "audio/webm"
    lead, ticket_id, text = await create_ticket_from_voice(
        data=data,
        fname=fname,
        mime=mime,
        source="Выставка / голос",
        notify=True,
    )
    return {"ok": True, "ticket_id": ticket_id, "lead": lead, "transcript": text}


@app.post("/webhook/telegram")
async def telegram_intake(request: Request):
    if not TELEGRAM_BOT_TOKEN:
        return JSONResponse({"ok": False, "error": "TELEGRAM_BOT_TOKEN is empty"}, status_code=400)

    if TELEGRAM_WEBHOOK_SECRET:
        got = request.headers.get("x-telegram-bot-api-secret-token", "")
        if got != TELEGRAM_WEBHOOK_SECRET:
            return JSONResponse({"ok": False, "error": "invalid telegram secret"}, status_code=403)

    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return {"ok": True}

    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return {"ok": True}

    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        await telegram_send_message(
            chat_id,
            "Привет. Отправь фото визитки или голосовое сообщение. Я распознаю данные и создам тикет в Zammad.",
        )
        return {"ok": True}

    try:
        if msg.get("photo"):
            photos = msg.get("photo") or []
            file_id = photos[-1].get("file_id")
            if not file_id:
                await telegram_send_message(chat_id, "Не удалось прочитать фото. Попробуй еще раз.")
                return {"ok": True}

            data, fname, mime = await telegram_download_file(file_id, "card.jpg", "image/jpeg")
            lead, ticket_id = await create_ticket_from_card(
                data=data,
                mime=mime,
                source="Telegram / визитка",
                comment=f"Telegram chat: {chat_id}",
                notify=False,
            )
            await telegram_send_message(
                chat_id,
                f"Готово. Создан тикет #{ticket_id}.\n"
                f"{lead.get('name') or '-'} | {lead.get('company') or '-'} | {lead.get('phone') or '-'}",
            )
            return {"ok": True}

        voice = msg.get("voice") or msg.get("audio")
        if voice:
            file_id = voice.get("file_id")
            if not file_id:
                await telegram_send_message(chat_id, "Не удалось прочитать аудио. Попробуй еще раз.")
                return {"ok": True}

            data, fname, mime = await telegram_download_file(file_id, "voice.ogg", "audio/ogg")
            lead, ticket_id, _ = await create_ticket_from_voice(
                data=data,
                fname=fname,
                mime=mime,
                source="Telegram / голос",
                notify=False,
            )
            await telegram_send_message(
                chat_id,
                f"Готово. Создан тикет #{ticket_id}.\n"
                f"{lead.get('name') or '-'} | {lead.get('company') or '-'} | {lead.get('phone') or '-'}",
            )
            return {"ok": True}

        await telegram_send_message(
            chat_id,
            "Поддерживаются только фото визитки и голосовые сообщения.",
        )
        return {"ok": True}

    except Exception as e:
        log.exception("telegram intake failed")
        await telegram_send_message(chat_id, f"Ошибка обработки: {e}")
        return {"ok": True}


@app.get("/webhook/telegram-test")
async def telegram_test():
    if not TELEGRAM_BOT_TOKEN:
        return JSONResponse({"ok": False, "error": "TELEGRAM_BOT_TOKEN is empty"}, status_code=400)
    chat_id = TELEGRAM_CHAT_ID or await discover_telegram_chat_id()
    if not chat_id:
        return JSONResponse({"ok": False, "error": "TELEGRAM_CHAT_ID is empty; send any message to bot and retry"}, status_code=400)
    await send_telegram_notice(None, {}, "тест")
    return {"ok": True, "chat_id": chat_id}
