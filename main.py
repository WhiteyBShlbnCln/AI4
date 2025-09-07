import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from runwayml import RunwayML, TaskFailedError

BOT_TOKEN = os.getenv("BOT_TOKEN")
RUNWAY_KEY = os.getenv("RUNWAYML_API_SECRET")

# Проверка наличия токена и ключа
if not BOT_TOKEN or not RUNWAY_KEY:
    raise RuntimeError("🚨 Нужно установить переменные окружения BOT_TOKEN и RUNWAYML_API_SECRET")

client = RunwayML(api_key=RUNWAY_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Напиши:\n"
        "/img описание — для изображения\n"
        "/vid описание — для видео"
    )

async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        return await update.message.reply_text("Напиши текст после /img!")

    message = await update.message.reply_text("⏳ Генерирую изображение...")
    try:
        task = client.text_to_image.create(
            model="gen4_image",
            prompt_text=prompt,
            ratio="1920:1080"
        ).wait_for_task_output()
        image_url = task.output[0]
        await message.delete()
        await update.message.reply_photo(photo=image_url)
    except TaskFailedError as e:
        await message.edit_text(f"❌ Генерация не удалась:\n{e.task_details}")
    except Exception as e:
        await message.edit_text(f"❌ Ошибка: {e}")

async def generate_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        return await update.message.reply_text("Напиши текст после /vid!")

    message = await update.message.reply_text("⏳ Генерирую видео...")
    try:
        task = client.image_to_video.create(
            model="gen4_turbo",
            prompt_text=prompt,
            ratio="1280:720",
            duration=5
        ).wait_for_task_output()
        video_url = task.output[0]
        await message.delete()
        await update.message.reply_video(video=video_url)
    except TaskFailedError as e:
        await message.edit_text(f"❌ Генерация не удалась:\n{e.task_details}")
    except Exception as e:
        await message.edit_text(f"❌ Ошибка: {e}")

app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("img", generate_image))
app.add_handler(CommandHandler("vid", generate_video))

if __name__ == "__main__":
    app.run_polling()
