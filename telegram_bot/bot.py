import os
import asyncio
import aiohttp
import io
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import google.generativeai as genai

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL", "http://core_api:8000")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")


if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    voice_model = genai.GenerativeModel('gemini-2.5-flash')
else:
    voice_model = None
    print("⚠️ GEMINI_API_KEY не знайдено. Голосові повідомлення не працюватимуть.")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class CalculatorStates(StatesGroup):
    waiting_for_product = State()
    waiting_for_volume = State()

def get_main_keyboard():
    keyboard = [
        [KeyboardButton(text="💬 Запитання технологу")],
        [KeyboardButton(text="🧮 Калькулятор рецептури")],
        [KeyboardButton(text="📦 Каталог продукції")],
        [KeyboardButton(text="📞 Зв'язок з менеджером")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🤖 Вітаю! Я AI-асистент компанії\n\n"
        "Можу допомогти з:\n"
        "• Підбором продукції з каталогу\n"
        "• Розрахунком рецептур та потреби\n"
        "• Оцифровкою документів (просто надішліть мені фото бланку)\n"
        "🎤 <b>НОВИНКА:</b> Ви можете задавати мені питання голосовими повідомленнями!\n\n"
        "Оберіть дію або задайте питання:",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

# --- КАЛЬКУЛЯТОР ---
@dp.message(F.text == "🧮 Калькулятор рецептури")
async def start_calculator(message: types.Message, state: FSMContext):
    await message.answer("📊 Розрахуємо вашу потребу!\n\nЩо плануєте виробляти?\n(наприклад: еклери, круасани, тістечка)")
    await state.set_state(CalculatorStates.waiting_for_product)

@dp.message(CalculatorStates.waiting_for_product)
async def process_product(message: types.Message, state: FSMContext):
    await state.update_data(product=message.text)
    await message.answer(f"Чудово! {message.text}\n\nЯкий планований об'єм виробництва?\n(штук на день)")
    await state.set_state(CalculatorStates.waiting_for_volume)

@dp.message(CalculatorStates.waiting_for_volume)
async def process_volume(message: types.Message, state: FSMContext):
    try:
        volume = int(message.text)
        if volume <= 0:
            await message.answer("Вибачте, але потрібна кількість. Напишіть цифру (наприклад: 100) або натисніть /start.")
            return

        data = await state.get_data()
        progress_msg = await message.answer("⏳ Аналізую каталоги та розраховую. Це може зайняти до хвилини...")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_URL}/agent/recipe/calculate",
                json={"product": data['product'], "volume": volume},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                await progress_msg.delete()
                
                if response.status == 200:
                    result = await response.json()
                    await message.answer(
                        f"📊 Розрахунок для {data['product']}:\n\n{result['recommendation']}\n\n📚 Джерела: {', '.join(result['sources'][:3])}",
                        reply_markup=get_main_keyboard()
                    )
                else:
                    print(f"🔥 ПОМИЛКА СЕРВЕРА (Калькулятор): HTTP {response.status}")
                    await message.answer("❌ Виникла помилка при розрахунку.", reply_markup=get_main_keyboard())
        await state.clear()
        
    except ValueError:
        await message.answer("Вибачте, потрібна конкретна кількість. Вкажіть цифру (наприклад: 500) або натисніть /start.")
    except Exception as e:
        print(f"🔥 КРИТИЧНА ПОМИЛКА (Калькулятор): {str(e)}")
        try: await progress_msg.delete()
        except: pass
        await message.answer("❌ Виникла технічна помилка. Спробуйте /start", reply_markup=get_main_keyboard())
        await state.clear()

# --- ОБРОБКА ФОТО (CRM) ---
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    progress_msg = await message.answer("📸 Отримав документ. Розпізнаю текст та створюю Лід у CRM...")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        
        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field('file', downloaded_file.read(), filename='document.jpg', content_type='image/jpeg')
            
            async with session.post(f"{API_URL}/agent/doc/digitize", data=data, timeout=60) as response:
                await progress_msg.delete()
                if response.status == 200:
                    result = await response.json()
                    if result.get("is_valid"):
                        text = f"✅ <b>Документ успішно розпізнано!</b>\n\n📄 <b>Тип:</b> {result.get('doc_type')}\n👨‍💼 <b>Інспектор:</b> {result.get('inspector_name')}\n"
                        text += f"\n📎 <b>Створено Лід в Odoo CRM! (ID: {result['odoo_id']})</b>" if result.get("odoo_id") else "\n⚠️ <i>Лід не створено.</i>"
                        await message.answer(text, parse_mode="HTML")
                    else:
                        await message.answer(f"❌ <b>Документ відхилено.</b>\nПричина: {result.get('rejection_reason')}", parse_mode="HTML")
                else:
                    print(f"🔥 ПОМИЛКА СЕРВЕРА (Оцифровка): HTTP {response.status}")
                    await message.answer("❌ Помилка сервера під час обробки фотографії.")
    except Exception as e:
        print(f"🔥 КРИТИЧНА ПОМИЛКА (Оцифровка): {str(e)}")
        try: await progress_msg.delete()
        except: pass
        await message.answer(f"❌ Технічна помилка: Перевірте логи сервера.")

# --- ОБРОБКА ГОЛОСОВИХ ПОВІДОМЛЕНЬ  ---
@dp.message(F.voice)
async def handle_voice(message: types.Message):
    if not voice_model:
        await message.answer("❌ Голосові повідомлення тимчасово недоступні (не налаштовано AI).")
        return

    progress_msg = await message.answer("🎤 Слухаю ваше голосове повідомлення...")

    try:
        
        file_info = await bot.get_file(message.voice.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        audio_bytes = downloaded_file.read()

        
        prompt = "Розпізнай це голосове повідомлення і напиши ТІЛЬКИ текст, який там звучить, тією ж мовою. Без жодних додаткових коментарів."
        response = voice_model.generate_content([
            prompt,
            {"mime_type": "audio/ogg", "data": audio_bytes}
        ])
        
        transcribed_text = response.text.strip()
        if not transcribed_text:
            raise ValueError("Порожнє розпізнавання")

        await progress_msg.edit_text(f"🎤 <b>Розпізнано:</b> <i>{transcribed_text}</i>\n\n⏳ Шукаю відповідь у каталогах...", parse_mode="HTML")

        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_URL}/agent/technologist/ask",
                json={"question": transcribed_text},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as api_response:
                await progress_msg.delete()
                
                if api_response.status == 200:
                    data = await api_response.json()
                    text = f"🎤 Запит: {transcribed_text}\n\n🤖 Відповідь технолога:\n\n{data.get('answer')}"
                    if data.get('sources'): 
                        text += f"\n\n📚 Джерела: {', '.join(data.get('sources')[:3])}"
                    await message.answer(text, reply_markup=get_main_keyboard())
                else:
                    print(f"🔥 ПОМИЛКА СЕРВЕРА (Голосове/API): HTTP {api_response.status}")
                    await message.answer("⚠️ Сервер повернув помилку при пошуку відповіді.", reply_markup=get_main_keyboard())

    except Exception as e:
        print(f"🔥 КРИТИЧНА ПОМИЛКА (Голосове): {str(e)}")
        try: await progress_msg.delete()
        except: pass
        await message.answer("❌ Не вдалося розпізнати голосове повідомлення. Перевірте, чи чітко вас чути.", reply_markup=get_main_keyboard())

# --- МЕНЮ ТА ТЕКСТОВІ ЗАПИТАННЯ ---
@dp.message(F.text == "📦 Каталог продукції")
async def show_catalog(message: types.Message):
    catalog_text = """
📚 <b>Наш каталог продукції</b>

👑 <b>Бренд "Optima":</b>
• Сухі суміші для випічки
• Поліпшувачі хліба
• Базові наповнювачі
• Шоколадна продукція (ChocoCraft)

🌟 <b>Бренд "Golden Mile":</b>
• Фруктові наповнювачі
• Молочні начинки (карамель, згущене молоко)
• Макові начинки
• Кондитерські наповнювачі та сиропи
• Мед штучний

<i>💡 Щоб дізнатися деталі або дозування, натисніть "💬 Запитання технологу" та напишіть назву!</i>
"""
    await message.answer(catalog_text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(F.text == "📞 Зв'язок з менеджером")
async def contact_manager(message: types.Message):
    contact_text = """
👤 <b>Зв'язок з менеджером</b>
Для консультацій щодо цін, оптових закупівель або співпраці:

📞 <b>Телефон:</b> +38 (044) 123-45-67
📧 <b>Email:</b> sales@balex.com
🌐 <b>Сайт:</b> www.balex.com
"""
    await message.answer(contact_text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(F.text == "💬 Запитання технологу")
async def ask_mode(message: types.Message):
    await message.answer("🤖 Задайте будь-яке питання по продукції. Я знаю всі каталоги напам'ять!")

@dp.message(F.text)
async def handle_question(message: types.Message):
    progress_msg = await message.answer("⏳ Шукаю відповідь у каталогах...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_URL}/agent/technologist/ask",
                json={"question": message.text},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                await progress_msg.delete()
                
                if response.status == 200:
                    data = await response.json()
                    text = f"🤖 Відповідь технолога:\n\n{data.get('answer')}"
                    if data.get('sources'): text += f"\n\n📚 Джерела: {', '.join(data.get('sources')[:3])}"
                    await message.answer(text, reply_markup=get_main_keyboard())
                else:
                    print(f"🔥 ПОМИЛКА СЕРВЕРА (Запитання): HTTP {response.status}")
                    await message.answer("⚠️ Сервер повернув помилку.", reply_markup=get_main_keyboard())
    except Exception as e:
        print(f"🔥 КРИТИЧНА ПОМИЛКА (Запитання): {str(e)}")
        try: await progress_msg.delete()
        except: pass
        await message.answer("❌ Помилка з'єднання з сервером.", reply_markup=get_main_keyboard())

async def main():
    print("🤖 Starting Telegram Bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())