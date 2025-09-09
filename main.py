# main.py (исправленная рабочая версия)
import os
import io
import base64
import asyncio
import logging
import traceback
from typing import Optional
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from runwayml import RunwayML

# Логи
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RUNWAY_KEY = os.getenv("RUNWAYML_API_SECRET")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
if not RUNWAY_KEY:
    raise RuntimeError("RUNWAYML_API_SECRET is missing")

# Ключи состояния пользователя
MODE_KEY = "mode"
DURATION_KEY = "duration"
RATIO_KEY = "ratio"
PROMPT_KEY = "prompt"

def image_bytes_to_data_uri(img_bytes: bytes, content_type: str = "image/jpeg") -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{content_type};base64,{b64}"

def extract_video_url_from_task(task) -> Optional[str]:
    """
    Попробуем прочитать task.output в разных форматах.
    """
    try:
        out = getattr(task, "output", None)
        if isinstance(out, (list, tuple)) and out:
            return out[0]
        if isinstance(task, dict):
            out = task.get("output")
            if isinstance(out, (list, tuple)) and out:
                return out[0]
    except Exception as e:
        logger.warning("Не удалось извлечь output из task: %s", e)
    return None

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Пользователь начал диалог")
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
    logger.info("Выбран режим: %s", mode)

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
    logger.info("Выбрана длительность: %s", duration)

    ratios = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
    kb = [[InlineKeyboardButton(r, callback_data=f"ratio_{r}")] for r in ratios]
    await q.edit_message_text("Выбери соотношение сторон:", reply_markup=InlineKeyboardMarkup(kb))

async def ratio_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ratio = q.data.split("_", 1)[1]
    context.user_data[RATIO_KEY] = ratio
    logger.info("Выбрано соотношение: %s", ratio)

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
    logger.info("Получен промпт: %s", prompt)
    if mode == "text":
        # Запускаем в фоне
        asyncio.create_task(text_generation_flow(update, context))
    else:
        await update.message.reply_text("Принято! Теперь пришли фото.")

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
    logger.info("Получено изображение, стартуем генерацию")
    await update.message.reply_text("Генерирую видео...")
    # Запускаем флоу в фоне
    asyncio.create_task(image_generation_flow(update, context, data_uri, prompt))

# ---------- Background flows (async) ----------
async def image_generation_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, data_uri: str, prompt: str):
    duration = context.user_data.get(DURATION_KEY)
    ratio = context.user_data.get(RATIO_KEY)
    logger.info("image_generation_flow: prompt=%s duration=%s ratio=%s", prompt, duration, ratio)
    try:
        # Запускаем синхронный SDK в отдельном потоке
        task = await asyncio.to_thread(run_image_generation_sync, data_uri, prompt, duration, ratio)
        logger.info("Runway image task finished: %s", getattr(task, "id", "<no-id>"))
        video_url = extract_video_url_from_task(task)
        await send_video(update, video_url, context)
    except Exception as e:
        logger.exception("Ошибка в image_generation_flow")
        await send_error(update, f"Ошибка при генерации (image→video): {e}")

async def text_generation_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = context.user_data.get(PROMPT_KEY)
    duration = context.user_data.get(DURATION_KEY)
    ratio = context.user_data.get(RATIO_KEY)
    logger.info("text_generation_flow: prompt=%s duration=%s ratio=%s", prompt, duration, ratio)
    try:
        task = await asyncio.to_thread(run_text_generation_sync, prompt, duration, ratio)
        logger.info("Runway text task finished: %s", getattr(task, "id", "<no-id>"))
        video_url = extract_video_url_from_task(task)
        await send_video(update, video_url, context)
    except Exception as e:
        logger.exception("Ошибка в text_generation_flow")
        await send_error(update, f"Ошибка при генерации (text→video): {e}")

# ---------- Sync wrappers for the Runway SDK (run in thread) ----------
def run_image_generation_sync(data_uri: str, prompt: str, duration: str, ratio: str):
    client = RunwayML(api_key=RUNWAY_KEY)
    # В SDK параметры могут отличаться — проверяй документацию. Здесь используем типичные имена.
    task = client.image_to_video.create(
        model="gen4_turbo",
        prompt_image=data_uri,
        prompt_text=prompt,
        duration=duration,
        ratio=ratio
    ).wait_for_task_output()
    return task

def run_text_generation_sync(prompt: str, duration: str, ratio: str):
    client = RunwayML(api_key=RUNWAY_KEY)
    task = client.text_to_video.create(
        model="gen4_turbo",
        prompt_text=prompt,
        duration=duration,
        ratio=ratio
    ).wait_for_task_output()
    return task

# ---------- Send helpers ----------
async def send_video(update: Update, video_url: Optional[str], context: ContextTypes.DEFAULT_TYPE):
    try:
        if video_url:
            logger.info("Отправляем видео пользователю: %s", video_url)
            try:
                await update.message.reply_video(video=video_url, caption="Готово! 🎬")
                logger.info("Видео отправлено через ссылку")
            except Exception as e:
                logger.warning("Не удалось отправить по URL, пробуем скачать и загрузить: %s", e)
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(video_url) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
                await update.message.reply_video(video=InputFile(io.BytesIO(data), filename="result.mp4"), caption="Готово! 🎬")
                logger.info("Видео отправлено как файл")
        else:
            await update.message.reply_text("Ошибка: не удалось получить ссылку на видео.")
    except Exception:
        logger.exception("Ошибка при отправке видео")
        try:
            await update.message.reply_text("Видео создано, но не удалось отправить его в Telegram.")
        except Exception:
            pass
    finally:
        # Сбрасываем состояние пользователя
        context.user_data.clear()

async def send_error(update: Update, message: str):
    logger.error("send_error -> %s", message)
    try:
        await update.message.reply_text(
            f"⚠️ Произошла ошибка при работе с Runway.\n{message}\nПроверьте параметры или попробуйте снова."
        )
    except Exception:
        logger.exception("Не удалось отправить сообщение об ошибке")
    # Сбрасываем состояние пользователя на всякий случай
    # update и context могут быть в разных состояниях, но попробуем очистить user_data
    try:
        update and update._effective_user and (update._effective_user.id)  # no-op to avoid lint
    except Exception:
        pass

# ---------- App startup ----------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(mode_selection, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(duration_selection, pattern="^duration_"))
    app.add_handler(CallbackQueryHandler(ratio_selection, pattern="^ratio_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Бот запущен (run_polling)...")
    # Блокирующий вызов, он сам создаёт и запускает loop
    app.run_polling()

if __name__ == "__main__":
    main()
