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
        "👋 Вітаю! Я AI-асистент брендів **Optima** та **Golden Mile**.\n\n"
        "✍️ **Напишіть текстом** або 🎤 **запишіть голосове** — я допоможу підібрати інгредієнти, суміші та начинки з наших каталогів.\n"
        "📸 **Надішліть фото бланку** — я оцифрую його та створю лід в CRM."
    )

# --- ОБРАБОТКА ТЕКСТА (БАЗА ЗНАНИЙ) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("🔍 Шукаю інформацію в каталогах...")
    user_question = update.message.text.strip()
    
    try:
        payload = {"question": user_question}
        rag_response = requests.post(f"{API_URL}/agent/technologist/ask", json=payload)
        
        if rag_response.status_code == 200:
            rag_data = rag_response.json()
            answer_text = rag_data.get('answer', 'Помилка отримання відповіді')
            
            await status_msg.edit_text(f"🤖 **AI Менеджер:**\n\n{answer_text}")
        else:
            await status_msg.edit_text(f"❌ Помилка API сервера: {rag_response.status_code}")

    except Exception as e:
        await status_msg.edit_text(f"❌ Помилка з'єднання: {e}")

# --- ОБРАБОТКА ФОТО (ДОКУМЕНТЫ) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("🧐 Аналізую документ...")
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        
        files = {'file': ('doc.jpg', photo_bytes, 'image/jpeg')}
        response = requests.post(f"{API_URL}/agent/doc/digitize", files=files)
        
        if response.status_code != 200:
            await status_msg.edit_text(f"❌ Помилка сервера: {response.text}")
            return

        data = response.json()
        
        if data.get("is_valid"):
            reply = f"✅ **УСПІХ!**\n\n" \
                    f"📄 Тип: {data['doc_type']}\n" \
                    f"🔢 Дані: {data['fields']}\n" \
                    f"📎 **Створено Лід в CRM ID:** {data['odoo_id']}"
            await status_msg.edit_text(reply)
        else:
            reply = f"⛔ **ВІДМОВА**\n\n" \
                    f"Причина: {data.get('rejection_reason')}\n" \
                    f"(Дані не відправлені в Odoo)"
            await status_msg.edit_text(reply)

    except Exception as e:
        await status_msg.edit_text(f"❌ Помилка бота: {e}")

# --- ОБРАБОТКА ГОЛОСА (ТЕХНОЛОГ) ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("👂 Слухаю...")
    file_path = "temp_voice.ogg" 
    
    try:
        voice_file = await update.message.voice.get_file()
        voice_bytes = await voice_file.download_as_bytearray()
        
        with open(file_path, "wb") as f:
            f.write(voice_bytes)
            
        uploaded_file = genai.upload_file(path=file_path, mime_type="audio/ogg")
        
        model = genai.GenerativeModel('gemini-2.5-flash')
        transcribe_resp = model.generate_content(
            [uploaded_file, "Напиши только текст того, что сказано в аудио. На украинском или русском языке."]
        )
        
        user_question = transcribe_resp.text.strip()
        await status_msg.edit_text(f"🗣 **Ваш запит:** {user_question}\n🔍 Шукаю в каталогах...")
        
        payload = {"question": user_question}
        rag_response = requests.post(f"{API_URL}/agent/technologist/ask", json=payload)
        
        if rag_response.status_code == 200:
            rag_data = rag_response.json()
            answer_text = rag_data.get('answer', 'Помилка отримання відповіді')
            
            await status_msg.edit_text(f"🤖 **AI Менеджер:**\n\n{answer_text}")
        else:
            await status_msg.edit_text(f"❌ Помилка API сервера: {rag_response.status_code}")

    except Exception as e:
        await status_msg.edit_text(f"❌ Помилка: {e}")
        
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler('start', start))
    # Добавили обработчик ТЕКСТА (игнорируем команды)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    print("🤖 Бот запущен!")
    application.run_polling()