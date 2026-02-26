import os
import logging
import requests
import io
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("CORE_API_URL", "http://api:8000") 
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# Настройка Gemini для транскрипции голоса
genai.configure(api_key=GEMINI_KEY)
model_flash = genai.GenerativeModel('gemini-2.5-flash')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я AI-ассистент завода Balex.\n\n"
        "📸 **Пришли мне фото** — я оцифрую документ в CRM.\n"
        "🎤 **Запиши голосовое** — я отвечу по базе знаний (Технолог)."
    )

# --- ОБРАБОТКА ФОТО (ДОКУМЕНТЫ) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("🧐 Смотрю документ...")
    
    try:
        # 1. Скачиваем фото из Telegram
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        
        
        files = {'file': ('doc.jpg', photo_bytes, 'image/jpeg')}
        response = requests.post(f"{API_URL}/agent/doc/digitize", files=files)
        
        if response.status_code != 200:
            await status_msg.edit_text(f"❌ Ошибка сервера: {response.text}")
            return

        data = response.json()
        
        # 3. Ответ пользователю
        if data.get("is_valid"):
            reply = f"✅ **УСПЕХ!**\n\n" \
                    f"📄 Тип: {data['doc_type']}\n" \
                    f"🔢 Данные: {data['fields']}\n" \
                    f"📎 **Создан Лид в CRM ID:** {data['odoo_id']}"
            await status_msg.edit_text(reply)
        else:
            reply = f"⛔ **ОТКАЗ**\n\n" \
                    f"Причина: {data.get('rejection_reason')}\n" \
                    f"(Я не отправил это в Odoo)"
            await status_msg.edit_text(reply)

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка бота: {e}")

# --- ОБРАБОТКА ГОЛОСА (ТЕХНОЛОГ) ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("👂 Слушаю...")
    
    file_path = "temp_voice.ogg" 
    
    try:
        # 1. Скачиваем голосовое сообщение
        voice_file = await update.message.voice.get_file()
        voice_bytes = await voice_file.download_as_bytearray()
        
        
        with open(file_path, "wb") as f:
            f.write(voice_bytes)
            
        
        uploaded_file = genai.upload_file(path=file_path, mime_type="audio/ogg")
        
        
        model = genai.GenerativeModel('gemini-2.5-flash')
        transcribe_resp = model.generate_content(
            [uploaded_file, "Напиши только текст того, что сказано в аудио. На русском языке."]
        )
        
        user_question = transcribe_resp.text.strip()
        await status_msg.edit_text(f"🗣 **Вы спросили:** {user_question}\n🔍 Ищу ответ...")
        
        
        
        payload = {"question": user_question}
        rag_response = requests.post(f"{API_URL}/agent/technologist/ask", json=payload)
        
        if rag_response.status_code == 200:
            rag_data = rag_response.json()
            answer_text = rag_data.get('answer', 'Ошибка получения ответа')
            
            # Отправляем ответ пользователю
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"👨‍🔧 **Технолог:**\n{answer_text}"
            )
        else:
            await status_msg.edit_text(f"❌ Ошибка API Технолога: {rag_response.status_code}")

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")
        
    finally:
        
        if os.path.exists(file_path):
            os.remove(file_path)

if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    print("🤖 Бот запущен!")
    application.run_polling()