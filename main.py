# main.py
import os
import io
import base64
import asyncio
import logging
import traceback
from typing import Optional, Any, Dict

from dotenv import load_dotenv
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ---------- CONFIG ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Accept both names just in case; prefer RUNWAY_API_KEY
RUNWAY_API_KEY = os.getenv("RUNWAY_API_KEY") or os.getenv("RUNWAYML_API_SECRET")

# Optional: set API version header (recommended by Runway docs)
RUNWAY_API_VERSION = os.getenv("RUNWAY_API_VERSION", "2024-11-06")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
if not RUNWAY_API_KEY:
    raise RuntimeError("RUNWAY_API_KEY (or RUNWAYML_API_SECRET) is missing")

RUNWAY_BASE = "https://api.runwayml.com/v1"

# ---------- STATE KEYS ----------
MODE_KEY = "mode"
DURATION_KEY = "duration"
RATIO_KEY = "ratio"
PROMPT_KEY = "prompt"

# ---------- HELPERS ----------
def image_bytes_to_data_uri(img_bytes: bytes, content_type: str = "image/jpeg") -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{content_type};base64,{b64}"

def extract_task_id(resp_json: Dict[str, Any]) -> Optional[str]:
    # Response shapes vary; try common keys
    for key in ("id", "taskId", "task_id", "task"):
        val = resp_json.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, dict) and val.get("id"):
            return val.get("id")
    # sometimes returned as {'data': {'id': '...'}}
    if isinstance(resp_json.get("data"), dict):
        for k in ("id", "taskId", "task_id"):
            if resp_json["data"].get(k):
                return resp_json["data"].get(k)
    return None

def extract_output_url(task_json: Dict[str, Any]) -> Optional[str]:
    out = task_json.get("output")
    if not out:
        return None
    # output may be list of strings or list of objects
    if isinstance(out, (list, tuple)) and out:
        first = out[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            # try common keys
            for k in ("url", "uri", "output", "video"):
                if k in first:
                    return first[k]
    # fallback
    return None

def runay_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {RUNWAY_API_KEY}",
        "X-Runway-Version": RUNWAY_API_VERSION,
        "Content-Type": "application/json",
    }

# ---------- RUNWAY (async REST) ----------
async def start_generation(endpoint: str, payload: Dict[str, Any], client: httpx.AsyncClient) -> Dict[str, Any]:
    url = f"{RUNWAY_BASE}/{endpoint}"
    logger.info("POST %s payload keys=%s", url, list(payload.keys()))
    resp = await client.post(url, headers=runay_headers(), json=payload, timeout=600.0)
    # don't raise yet — we want to include body in logs if error
    if resp.status_code >= 400:
        text = await resp.text()
        logger.error("Runway start_generation error status=%s body=%s", resp.status_code, text)
        resp.raise_for_status()
    data = await resp.json()
    logger.debug("Start generation response: %s", data)
    return data

async def poll_task(task_id: str, client: httpx.AsyncClient, max_wait: int = 300, interval: float = 3.0) -> Dict[str, Any]:
    """
    Poll GET /v1/tasks/{id} until SUCCEEDED/FAILED or timeout.
    Returns last JSON.
    """
    url = f"{RUNWAY_BASE}/tasks/{task_id}"
    waited = 0.0
    while waited < max_wait:
        resp = await client.get(url, headers=runay_headers(), timeout=120.0)
        if resp.status_code >= 400:
            text = await resp.text()
            logger.error("Runway poll_task error status=%s body=%s", resp.status_code, text)
            resp.raise_for_status()
        j = await resp.json()
        status = j.get("status") or j.get("state")  # some variants
        logger.info("Task %s status=%s", task_id, status)
        if isinstance(status, str) and status.upper() in ("SUCCEEDED", "SUCCESS", "COMPLETED"):
            return j
        if isinstance(status, str) and status.upper() in ("FAILED", "ERROR"):
            return j
        await asyncio.sleep(interval)
        waited += interval
    raise TimeoutError(f"Task {task_id} did not finish within {max_wait} seconds")

# ---------- BOT: UI / HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎬 Только текст", callback_data="mode_text")],
        [InlineKeyboardButton("🖼 Текст + изображение", callback_data="mode_image")],
    ]
    await update.message.reply_text("Выбери режим генерации видео:", reply_markup=InlineKeyboardMarkup(keyboard))
    logger.info("User started conversation (chat_id=%s)", update.effective_chat.id)

async def mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = "text" if q.data == "mode_text" else "image"
    context.user_data[MODE_KEY] = mode
    kb = [
        [InlineKeyboardButton("5 секунд", callback_data="duration_5")],
        [InlineKeyboardButton("10 секунд", callback_data="duration_10")],
    ]
    await q.edit_message_text("Выбери длительность видео:", reply_markup=InlineKeyboardMarkup(kb))
    logger.info("Mode %s chosen (chat=%s)", mode, update.effective_chat.id)

async def duration_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    duration = 5 if q.data == "duration_5" else 10
    context.user_data[DURATION_KEY] = duration
    ratios = ["1280:720", "720:1280", "960:960", "1104:832", "832:1104", "1584:672"]
    kb = [[InlineKeyboardButton(r, callback_data=f"ratio_{r}")] for r in ratios]
    await q.edit_message_text("Выбери соотношение сторон (разрешение):", reply_markup=InlineKeyboardMarkup(kb))
    logger.info("Duration set to %s (chat=%s)", duration, update.effective_chat.id)

async def ratio_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ratio = q.data.split("_", 1)[1]
    context.user_data[RATIO_KEY] = ratio
    mode = context.user_data.get(MODE_KEY)
    if mode == "text":
        await q.edit_message_text("Отправь текстовый запрос (промпт):")
    else:
        await q.edit_message_text("Отправь текстовый запрос, затем фото.")
    logger.info("Ratio set to %s (chat=%s)", ratio, update.effective_chat.id)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = context.user_data.get(MODE_KEY)
    duration = context.user_data.get(DURATION_KEY)
    ratio = context.user_data.get(RATIO_KEY)
    if not (mode and duration and ratio):
        await context.bot.send_message(chat_id=chat_id, text="Сначала выбери параметры через /start")
        return

    prompt = (update.message.text or "").strip()
    context.user_data[PROMPT_KEY] = prompt
    await context.bot.send_message(chat_id=chat_id, text="Генерирую видео... (запрос принят)")

    try:
        async with httpx.AsyncClient() as client:
            # start generation (text->video)
            payload = {
                "model": "gen4_turbo",   # можно менять на gen3a_turbo / gen3 / gen4 etc.
                "promptText": prompt,
                "duration": duration,
                "ratio": ratio,
                "watermark": False
            }
            start_resp = await start_generation("text_to_video", payload, client)
            task_id = extract_task_id(start_resp)
            if not task_id:
                logger.error("No task id in start response: %s", start_resp)
                raise RuntimeError("No task id returned from Runway (see server logs).")
            task_json = await poll_task(task_id, client, max_wait=300, interval=3.0)
            status = (task_json.get("status") or "").upper()
            if status not in ("SUCCEEDED", "SUCCESS", "COMPLETED"):
                # include details
                logger.error("Task failed or not succeeded: %s", task_json)
                await send_error_chat(context, chat_id, f"Runway task status={status}. Details: {task_json}")
                return
            video_url = extract_output_url(task_json)
            if not video_url:
                await send_error_chat(context, chat_id, f"Task finished but no output URL. Task: {task_json}")
                return

            # Try to send by URL; if it fails, download and upload
            try:
                await context.bot.send_video(chat_id=chat_id, video=video_url, caption="Готово! 🎬")
            except Exception as e:
                logger.warning("Sending by URL failed, will download and upload: %s", e)
                # download
                r = await client.get(video_url, timeout=120.0)
                r.raise_for_status()
                data = r.content
                await context.bot.send_video(chat_id=chat_id, video=InputFile(io.BytesIO(data), filename="result.mp4"), caption="Готово! 🎬")

            context.user_data.clear()

    except Exception as e:
        logger.exception("Error in text generation flow")
        # send sanitized but useful error to user
        detail = "".join(traceback.format_exception_only(type(e), e)).strip()
        await send_error_chat(context, chat_id, f"Ошибка при генерации (text→video): {detail}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = context.user_data.get(MODE_KEY)
    if mode != "image":
        await context.bot.send_message(chat_id=chat_id, text="Фото не нужно для этого режима. Используй /start.")
        return
    prompt = context.user_data.get(PROMPT_KEY) or (update.message.caption or "").strip()
    if not prompt:
        await context.bot.send_message(chat_id=chat_id, text="Сначала отправь текстовый промпт.")
        return
    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_bytes = await file.download_as_bytearray()
    data_uri = image_bytes_to_data_uri(bytes(img_bytes))
    await context.bot.send_message(chat_id=chat_id, text="Генерирую видео (из изображения)...")

    try:
        async with httpx.AsyncClient() as client:
            payload = {
                "model": "gen4_turbo",
                "promptText": prompt,
                "promptImage": data_uri,   # REST expects promptImage (camelCase)
                "duration": context.user_data.get(DURATION_KEY),
                "ratio": context.user_data.get(RATIO_KEY),
                "watermark": False
            }
            start_resp = await start_generation("image_to_video", payload, client)
            task_id = extract_task_id(start_resp)
            if not task_id:
                logger.error("No task id in start response: %s", start_resp)
                raise RuntimeError("No task id returned from Runway (see server logs).")
            task_json = await poll_task(task_id, client, max_wait=300, interval=3.0)
            status = (task_json.get("status") or "").upper()
            if status not in ("SUCCEEDED", "SUCCESS", "COMPLETED"):
                logger.error("Task failed or not succeeded: %s", task_json)
                await send_error_chat(context, chat_id, f"Runway task status={status}. Details: {task_json}")
                return
            video_url = extract_output_url(task_json)
            if not video_url:
                await send_error_chat(context, chat_id, f"Task finished but no output URL. Task: {task_json}")
                return

            # send by URL or upload fallback
            try:
                await context.bot.send_video(chat_id=chat_id, video=video_url, caption="Готово! 🎬")
            except Exception:
                r = await client.get(video_url, timeout=120.0)
                r.raise_for_status()
                await context.bot.send_video(chat_id=chat_id, video=InputFile(io.BytesIO(r.content), filename="result.mp4"), caption="Готово! 🎬")

            context.user_data.clear()
    except Exception as e:
        logger.exception("Error in image generation flow")
        detail = "".join(traceback.format_exception_only(type(e), e)).strip()
        await send_error_chat(context, chat_id, f"Ошибка при генерации (image→video): {detail}")

async def send_error_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str):
    # Send a user-friendly error, log full details in server logs
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Произошла ошибка при работе с Runway.\n{message}\n(Проверь логи сервера для деталей)")
    except Exception:
        logger.exception("Failed to send error message to chat %s", chat_id)

# ---------- ENTRYPOINT ----------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(mode_selection, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(duration_selection, pattern="^duration_"))
    app.add_handler(CallbackQueryHandler(ratio_selection, pattern="^ratio_"))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot starting (run_polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
