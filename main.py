# main.py - final version (no asyncio.run inside handlers)
import os
import io
import base64
import asyncio
import traceback
import logging
from typing import Optional
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from runwayml import RunwayML
import aiohttp

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RUNWAY_API_KEY = os.getenv("RUNWAY_API_KEY") or os.getenv("RUNWAYML_API_SECRET")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
if not RUNWAY_API_KEY:
    raise RuntimeError("RUNWAY_API_KEY is missing")

# state keys
MODE_KEY = "mode"
DURATION_KEY = "duration"
RATIO_KEY = "ratio"
PROMPT_KEY = "prompt"

def image_bytes_to_data_uri(img_bytes: bytes, content_type: str = "image/jpeg") -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{content_type};base64,{b64}"

def extract_video_url_from_task(task) -> Optional[str]:
    """
    Try to extract a usable video URL from different task.output shapes.
    """
    try:
        out = getattr(task, "output", None)
        if isinstance(out, (list, tuple)) and out:
            first = out[0]
            if isinstance(first, (str,)):
                return first
            # If first is dict with url
            if isinstance(first, dict):
                # common key names
                for k in ("url", "output", "video", "uri", "result"):
                    if k in first:
                        return first[k]
            # nested structure
        # also if task is dict-like
        if isinstance(task, dict):
            out = task.get("output")
            if isinstance(out, (list, tuple)) and out:
                first = out[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, dict):
                    for k in ("url", "output", "video", "uri", "result"):
                        if k in first:
                            return first[k]
    except Exception:
        logger.exception("extract_video_url_from_task failed")
    return None

# ----- Handlers / UI -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User started")
    keyboard = [
        [InlineKeyboardButton("🎬 Только текст", callback_data="mode_text")],
        [InlineKeyboardButton("🖼 Текст + изображение", callback_data="mode_image")],
    ]
    await update.message.reply_text("Выбери режим генерации видео:", reply_markup=InlineKeyboardMarkup(keyboard))

async def mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = "text" if q.data == "mode_text" else "image"
    context.user_data[MODE_KEY] = mode
    logger.info("Mode chosen: %s", mode)
    kb = [
        [InlineKeyboardButton("5 секунд", callback_data="duration_5")],
        [InlineKeyboardButton("10 секунд", callback_data="duration_10")],
    ]
    await q.edit_message_text("Выбери длительность видео:", reply_markup=InlineKeyboardMarkup(kb))

async def duration_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    duration = "5s" if q.data == "duration_5" else "10s"
    context.user_data[DURATION_KEY] = duration
    logger.info("Duration chosen: %s", duration)
    ratios = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
    kb = [[InlineKeyboardButton(r, callback_data=f"ratio_{r}")] for r in ratios]
    await q.edit_message_text("Выбери соотношение сторон:", reply_markup=InlineKeyboardMarkup(kb))

async def ratio_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ratio = q.data.split("_", 1)[1]
    context.user_data[RATIO_KEY] = ratio
    logger.info("Ratio chosen: %s", ratio)
    mode = context.user_data.get(MODE_KEY)
    if mode == "text":
        await q.edit_message_text("Отправь текстовый запрос (промпт):")
    else:
        await q.edit_message_text("Отправь текстовый запрос, затем фото.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get(MODE_KEY)
    duration = context.user_data.get(DURATION_KEY)
    ratio = context.user_data.get(RATIO_KEY)
    if not (mode and duration and ratio):
        await update.message.reply_text("Сначала выбери параметры через /start")
        return
    prompt = (update.message.text or "").strip()
    context.user_data[PROMPT_KEY] = prompt
    logger.info("Prompt received: %s", prompt)
    # run generation in background to avoid blocking handler
    asyncio.create_task(text_generation_flow(update, context))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get(MODE_KEY)
    if mode != "image":
        await update.message.reply_text("Фото не нужно для этого режима. Используй /start.")
        return
    prompt = context.user_data.get(PROMPT_KEY) or (update.message.caption or "").strip()
    if not prompt:
        await update.message.reply_text("Сначала отправь текстовый промпт.")
        return
    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_bytes = await file.download_as_bytearray()
    data_uri = image_bytes_to_data_uri(bytes(img_bytes))
    await update.message.reply_text("Генерирую видео...")
    asyncio.create_task(image_generation_flow(update, context, data_uri, prompt))

# ----- Background flows -----
async def image_generation_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, data_uri: str, prompt: str):
    duration = context.user_data.get(DURATION_KEY)
    ratio = context.user_data.get(RATIO_KEY)
    logger.info("Starting image generation: prompt=%s duration=%s ratio=%s", prompt, duration, ratio)
    try:
        # run blocking SDK in thread
        task = await asyncio.to_thread(run_image_generation_sync, data_uri, prompt, duration, ratio)
        logger.info("Runway image task done (raw): %s", getattr(task, "id", "<no-id>"))
        video_url = extract_video_url_from_task(task)
        await send_video(update, video_url, context)
    except Exception as e:
        logger.exception("Error in image_generation_flow")
        await send_error(update, f"Ошибка при генерации (image→video): {e}")

async def text_generation_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = context.user_data.get(PROMPT_KEY)
    duration = context.user_data.get(DURATION_KEY)
    ratio = context.user_data.get(RATIO_KEY)
    logger.info("Starting text generation: prompt=%s duration=%s ratio=%s", prompt, duration, ratio)
    try:
        task = await asyncio.to_thread(run_text_generation_sync, prompt, duration, ratio)
        logger.info("Runway text task done (raw): %s", getattr(task, "id", "<no-id>"))
        video_url = extract_video_url_from_task(task)
        await send_video(update, video_url, context)
    except Exception as e:
        logger.exception("Error in text_generation_flow")
        await send_error(update, f"Ошибка при генерации (text→video): {e}")

# ----- Sync wrappers for Runway SDK (executed in thread) -----
def run_image_generation_sync(data_uri: str, prompt: str, duration: str, ratio: str):
    client = RunwayML(api_key=RUNWAY_API_KEY)
    # normalize duration (allow both '5s' or 5)
    if isinstance(duration, int) or (isinstance(duration, str) and duration.isdigit()):
        duration_val = f"{duration}s"
    else:
        duration_val = duration
    resp = client.tasks.create(
        model="gen3",
        input={
            "prompt": prompt,
            "image": data_uri,
            "duration": duration_val,
            "ratio": ratio,
        }
    ).wait()
    return resp

def run_text_generation_sync(prompt: str, duration: str, ratio: str):
    client = RunwayML(api_key=RUNWAY_API_KEY)
    if isinstance(duration, int) or (isinstance(duration, str) and duration.isdigit()):
        duration_val = f"{duration}s"
    else:
        duration_val = duration
    resp = client.tasks.create(
        model="gen3",
        input={
            "prompt": prompt,
            "duration": duration_val,
            "ratio": ratio,
        }
    ).wait()
    return resp

# ----- Send helpers -----
async def send_video(update: Update, video_url: Optional[str], context: ContextTypes.DEFAULT_TYPE):
    try:
        if video_url:
            logger.info("Sending video to user: %s", video_url)
            try:
                await update.message.reply_video(video=video_url, caption="Готово! 🎬")
                logger.info("Sent video by URL")
                return
            except Exception as e:
                logger.warning("Failed to send by URL, will download and send as file: %s", e)
                # download and send as file
                async with aiohttp.ClientSession() as session:
                    async with session.get(video_url) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
                await update.message.reply_video(video=InputFile(io.BytesIO(data), filename="result.mp4"), caption="Готово! 🎬")
                logger.info("Sent video as uploaded file")
                return
        # No video_url
        await update.message.reply_text("Ошибка: не удалось получить ссылку на видео.")
    except Exception:
        logger.exception("Error while sending video")
        try:
            await update.message.reply_text("Произошла ошибка при отправке видео.")
        except Exception:
            pass
    finally:
        context.user_data.clear()

async def send_error(update: Update, message: str):
    logger.error("send_error -> %s", message)
    try:
        await update.message.reply_text(
            f"⚠️ Произошла ошибка при работе с Runway.\n{message}\nПроверьте параметры или попробуйте снова."
        )
    except Exception:
        logger.exception("Failed to send error message")
    context.user_data.clear()

# ----- App entrypoint -----
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(mode_selection, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(duration_selection, pattern="^duration_"))
    app.add_handler(CallbackQueryHandler(ratio_selection, pattern="^ratio_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Bot starting (run_polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
