import os
import io
import base64
import asyncio
import traceback
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from runwayml import RunwayML

# Настройка логов
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

# Конвертация изображения в Data URI
def image_bytes_to_data_uri(img_bytes: bytes, content_type="image/jpeg"):
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{content_type};base64,{b64}"

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Пользователь начал диалог")
    keyboard = [
        [InlineKeyboardButton("🎬 Только текст", callback_data="mode_text")],
        [InlineKeyboardButton("🖼 Текст + изображение", callback_data="mode_image")],
    ]
    await update.message.reply_text(
        "Выбери режим генерации видео:", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ===== MODE SELECTION =====
async def mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = "text" if q.data == "mode_text" else "image"
    context.user_data[MODE_KEY] = mode
    logger.info(f"Выбран режим: {mode}")

    keyboard = [
        [InlineKeyboardButton("5 секунд", callback_data="duration_5")],
        [InlineKeyboardButton("10 секунд", callback_data="duration_10")],
    ]
    await q.edit_message_text("Выбери длительность видео:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===== DURATION SELECTION =====
async def duration_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    duration = "5s" if q.data == "duration_5" else "10s"
    context.user_data[DURATION_KEY] = duration
    logger.info(f"Выбрана длительность: {duration}")

    ratios = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
    keyboard = [[InlineKeyboardButton(r, callback_data=f"ratio_{r}")] for r in ratios]
    await q.edit_message_text("Выбери соотношение сторон:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===== RATIO SELECTION =====
async def ratio_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ratio = q.data.split("_")[1]
    context.user_data[RATIO_KEY] = ratio
    logger.info(f"Выбрано соотношение сторон: {ratio}")

    mode = context.user_data.get(MODE_KEY)
    if mode == "text":
        await q.edit_message_text("Отправь текстовый запрос (промпт):")
    else:
        await q.edit_message_text("Отправь текстовый запрос, затем фото.")

# ===== HANDLE TEXT =====
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get(MODE_KEY)
    duration = context.user_data.get(DURATION_KEY)
    ratio = context.user_data.get(RATIO_KEY)

    if not (mode and duration and ratio):
        await update.message.reply_text("Сначала выбери параметры через /start")
        return

    context.user_data[PROMPT_KEY] = update.message.text.strip()
    logger.info(f"Получен текстовый промпт: {context.user_data[PROMPT_KEY]}")

    if mode == "text":
        await generate_text_video(update, context)
    else:
        await update.message.reply_text("Принято! Теперь пришли фото.")

# ===== HANDLE PHOTO =====
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
    logger.info("Получено изображение для генерации видео")

    await update.message.reply_text("Генерирую видео...")
    asyncio.create_task(generate_image_video(update, context, data_uri))

# ===== GENERATE IMAGE VIDEO =====
async def generate_image_video(update, context, data_uri):
    prompt = context.user_data[PROMPT_KEY]
    duration = context.user_data[DURATION_KEY]
    ratio = context.user_data[RATIO_KEY]

    client = RunwayML(api_key=RUNWAY_KEY)

    try:
        logger.info("Отправка запроса image-to-video в Runway")
        task = client.image_to_video.create(
            model="gen4_turbo",
            prompt_image=data_uri,
            prompt_text=prompt,
            duration=duration,
            ratio=ratio
        ).wait_for_task_output()
        logger.info(f"Runway image-to-video response: {task}")
        video_url = task.output[0] if task.output else None
        await send_video(update, video_url, context)
    except Exception as e:
        logger.error(f"Ошибка image-to-video: {e}\n{traceback.format_exc()}")
        await send_error(update, f"Ошибка генерации видео (image→video): {e}")

# ===== GENERATE TEXT VIDEO =====
async def generate_text_video(update, context):
    await update.message.reply_text("Генерирую видео...")
    asyncio.create_task(_generate_text_video(update, context))

async def _generate_text_video(update, context):
    prompt = context.user_data[PROMPT_KEY]
    duration = context.user_data[DURATION_KEY]
    ratio = context.user_data[RATIO_KEY]

    client = RunwayML(api_key=RUNWAY_KEY)

    try:
        logger.info("Отправка запроса text-to-video в Runway")
        task = client.text_to_video.create(
            model="gen4_turbo",
            prompt_text=prompt,
            duration=duration,
            ratio=ratio
        ).wait_for_task_output()
        logger.info(f"Runway text-to-video response: {task}")
        video_url = task.output[0] if task.output else None
        await send_video(update, video_url, context)
    except Exception as e:
        logger.error(f"Ошибка text-to-video: {e}\n{traceback.format_exc()}")
        await send_error(update, f"Ошибка генерации видео (text→video): {e}")

# ===== SEND VIDEO =====
async def send_video(update, video_url, context):
    if video_url:
        try:
            await update.message.reply_video(video=video_url, caption="Готово! 🎬")
            logger.info("Видео успешно отправлено пользователю")
        except Exception as e:
            logger.error(f"Ошибка отправки видео: {e}")
            await update.message.reply_text(f"Видео создано, но не удалось отправить напрямую. Ссылка: {video_url}")
    else:
        await update.message.reply_text("Ошибка: не удалось получить ссылку на видео.")
    context.user_data.clear()

# ===== SEND ERROR =====
async def send_error(update, message):
    await update.message.reply_text(
        f"⚠️ Произошла ошибка при работе с Runway.\n"
        f"{message}\n"
        "Проверьте параметры или попробуйте снова."
    )
    update.application.bot_data.clear()

# ===== MAIN =====
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(mode_selection, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(duration_selection, pattern="^duration_"))
    app.add_handler(CallbackQueryHandler(ratio_selection, pattern="^ratio_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    logger.info("Бот запущен и ожидает сообщений...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
