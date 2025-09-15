import os
import io
import base64
import asyncio
import traceback
import logging
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from runwayml import RunwayML

# Логи
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RUNWAY_API_KEY = os.getenv("RUNWAYML_API_SECRET")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
if not RUNWAY_API_KEY:
    raise RuntimeError("RUNWAYML_API_SECRET is missing")

# Ключи для состояния пользователя
MODE_KEY = "mode"
DURATION_KEY = "duration"
RATIO_KEY = "ratio"
PROMPT_KEY = "prompt"

# Конвертируем изображение в Data URI
def image_bytes_to_data_uri(img_bytes: bytes, content_type="image/jpeg"):
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{content_type};base64,{b64}"

# -------------------- UI --------------------
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
    context.user_data[MODE_KEY] = "text" if q.data == "mode_text" else "image"
    kb = [
        [InlineKeyboardButton("5 секунд", callback_data="duration_5")],
        [InlineKeyboardButton("10 секунд", callback_data="duration_10")],
    ]
    await q.edit_message_text("Выбери длительность видео:", reply_markup=InlineKeyboardMarkup(kb))

async def duration_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data[DURATION_KEY] = 5 if q.data == "duration_5" else 10
    ratios = [
        ("16:9", "1280:720"), ("9:16", "720:1280"), ("1:1", "960:960"),
        ("4:3", "1104:832"), ("3:4", "832:1104"), ("21:9", "1584:672")
    ]
    kb = [[InlineKeyboardButton(name, callback_data=f"ratio_{name}")] for name, _ in ratios]
    await q.edit_message_text("Выбери соотношение сторон:", reply_markup=InlineKeyboardMarkup(kb))

async def ratio_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ratio_map = {
        "16:9": "1280:720", "9:16": "720:1280", "1:1": "960:960",
        "4:3": "1104:832", "3:4": "832:1104", "21:9": "1584:672"
    }
    key = q.data.split("_")[1]
    context.user_data[RATIO_KEY] = ratio_map.get(key, "1280:720")
    mode = context.user_data[MODE_KEY]
    if mode == "text":
        await q.edit_message_text("Отправь текстовый запрос (промпт):")
    else:
        await q.edit_message_text("Отправь текстовый запрос, затем фото.")

# -------------------- HANDLERS --------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get(MODE_KEY)
    duration = context.user_data.get(DURATION_KEY)
    ratio = context.user_data.get(RATIO_KEY)
    if not (mode and duration and ratio):
        await update.message.reply_text("Сначала выбери параметры через /start")
        return
    context.user_data[PROMPT_KEY] = update.message.text.strip()
    if mode == "text":
        await generate_text_video(update, context)
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
    await update.message.reply_text("Генерирую видео...")
    await generate_image_video(update, context, data_uri)

# -------------------- RUNWAY --------------------
async def generate_image_video(update, context, data_uri):
    client = RunwayML(api_key=RUNWAY_API_KEY)
    prompt = context.user_data[PROMPT_KEY]
    duration = context.user_data[DURATION_KEY]
    ratio = context.user_data[RATIO_KEY]

    try:
        task = client.tasks.create(
            model="gen3",
            input={
                "prompt": prompt,
                "image": data_uri,
                "duration": duration,
                "ratio": ratio,
            }
        ).wait()

        logger.info(f"Runway image-to-video response: {task}")
        video_url = task.output[0] if task.output else None
        await send_video(update, video_url, context)
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Runway image-to-video error: {e}\n{error_details}")
        await send_error(update, f"Ошибка при генерации (image→video): {e}")

async def generate_text_video(update, context):
    client = RunwayML(api_key=RUNWAY_API_KEY)
    prompt = context.user_data[PROMPT_KEY]
    duration = context.user_data[DURATION_KEY]
    ratio = context.user_data[RATIO_KEY]

    try:
        task = client.tasks.create(
            model="gen3",
            input={
                "prompt": prompt,
                "duration": duration,
                "ratio": ratio,
            }
        ).wait()

        logger.info(f"Runway text-to-video response: {task}")
        video_url = task.output[0] if task.output else None
        await send_video(update, video_url, context)
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Runway text-to-video error: {e}\n{error_details}")
        await send_error(update, f"Ошибка при генерации (text→video): {e}")

# -------------------- UTILS --------------------
async def send_video(update, video_url, context):
    if video_url:
        await update.message.reply_video(video=video_url, caption="Готово! 🎬")
    else:
        await update.message.reply_text("Ошибка: не удалось получить ссылку на видео.")
    context.user_data.clear()

async def send_error(update, message):
    await update.message.reply_text(
        f"⚠️ Произошла ошибка при работе с Runway.\n"
        f"{message}\n"
        "Проверьте параметры или попробуйте снова."
    )

# -------------------- MAIN --------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(mode_selection, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(duration_selection, pattern="^duration_"))
    app.add_handler(CallbackQueryHandler(ratio_selection, pattern="^ratio_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Бот запущен (run_polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
