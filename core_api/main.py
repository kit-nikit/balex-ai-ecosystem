import os
import io
import json
import xmlrpc.client
import ssl
import base64
import re
import PyPDF2  
from typing import List, Optional
import chromadb
from chromadb.utils import embedding_functions
from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
import google.generativeai as genai
from PIL import Image
import logging
from urllib.parse import urlparse

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. НАСТРОЙКИ ODOO ---
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# Проверка критических переменных окружения
required_odoo_vars = ["ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_PASSWORD"]
missing_odoo = [var for var in required_odoo_vars if not os.getenv(var)]
if missing_odoo:
    logger.warning(f"⚠️ Отсутствуют переменные Odoo: {', '.join(missing_odoo)}")

# ТОЛЬКО для разработки!
if os.getenv("DEVELOPMENT_MODE") == "true":
    ssl._create_default_https_context = ssl._create_unverified_context
    logger.warning("⚠️ SSL проверка отключена (режим разработки)")

# --- 2. НАСТРОЙКИ AI ---
CHROMA_URL = os.getenv("CHROMA_DB_URL", "http://vectordb:8000")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# ВЕРНУЛИ ВЕРСИЮ 2.5 FLASH!
CURRENT_MODEL_NAME = 'gemini-2.5-flash'  

if not GEMINI_KEY:
    logger.error("❌ ОШИБКА: Нет API ключа Gemini!")
else:
    genai.configure(api_key=GEMINI_KEY)

# Глобальная инициализация модели
try:
    ai_model = genai.GenerativeModel(CURRENT_MODEL_NAME)
    logger.info(f"✅ Модель {CURRENT_MODEL_NAME} инициализирована")
except Exception as e:
    logger.error(f"❌ Ошибка инициализации модели: {e}")
    ai_model = None

# Настройка ChromaDB с fallback
try:
    parsed_url = urlparse(CHROMA_URL)
    client = chromadb.HttpClient(
        host=parsed_url.hostname or 'vectordb', 
        port=parsed_url.port or 8000
    )
    logger.info(f"✅ Подключение к ChromaDB: {CHROMA_URL}")
except Exception as e:
    logger.warning(f"⚠️ Не удалось подключиться к {CHROMA_URL}, использую локальную базу")
    client = chromadb.PersistentClient(path="./chroma_db")

emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="paraphrase-multilingual-MiniLM-L12-v2"
)
collection = client.get_or_create_collection(
    name="balex_knowledge", 
    embedding_function=emb_fn
)

app = FastAPI(title="B-test AI Ecosystem API", version="3.3.1")

# --- 3. МОДЕЛИ ---
class QueryRequest(BaseModel):
    question: str

class AIResponse(BaseModel):
    answer: str
    sources: list[str]

class DigitalForm(BaseModel):
    is_valid: bool
    rejection_reason: Optional[str] = None
    doc_type: str
    date: str
    inspector_name: str
    fields: dict
    odoo_id: Optional[int] = None

# --- 4. ФУНКЦИИ ---

def update_knowledge_base():
    """Читает все TXT и PDF из папки data и загружает в ChromaDB"""
    data_dir = "data"
    if not os.path.exists(data_dir):
        logger.warning(f"📁 Папка {data_dir} не найдена")
        return False

    logger.info("⏳ Начинаю обновление базы знаний...")
    
    # Очистка коллекции для избежания дублей (СУПЕР ФИЧА!)
    try:
        existing_ids = collection.get()['ids']
        if existing_ids:
            collection.delete(ids=existing_ids)
            logger.info(f"🗑️ Удалено {len(existing_ids)} старых записей")
    except Exception as e:
        logger.warning(f"⚠️ Очистка коллекции пропущена: {e}")

    docs = []
    metadatas = []
    ids = []

    # Читаем TXT
    txt_path = os.path.join(data_dir, "balex_knowledge.txt")
    if os.path.exists(txt_path):
        try:
            with open(txt_path, "r", encoding="utf-8") as f:
                text = f.read()
                chunks = [text[i:i+1500] for i in range(0, len(text), 1200)] 
                for i, chunk in enumerate(chunks):
                    docs.append(chunk)
                    metadatas.append({"source": "balex_knowledge.txt"})
                    ids.append(f"txt_chunk_{i}")
            logger.info(f"✅ Загружен TXT файл: {len(chunks)} чанков")
        except Exception as e:
            logger.error(f"❌ Ошибка чтения TXT: {e}")

    # Читаем PDF
    for filename in os.listdir(data_dir):
        if filename.endswith(".pdf"):
            pdf_path = os.path.join(data_dir, filename)
            try:
                reader = PyPDF2.PdfReader(pdf_path)
                text = f"--- КАТАЛОГ: {filename} ---\n"
                for page in reader.pages:
                    extracted = page.extract_text()
                    if extracted:
                        text += extracted + "\n"
                
                # Улучшенная разбивка на чанки с перекрытием
                chunk_size = 1500
                overlap = 300
                chunks = []
                for i in range(0, len(text), chunk_size - overlap):
                    chunk = text[i:i + chunk_size]
                    if chunk.strip():
                        chunks.append(chunk)
                
                for i, chunk in enumerate(chunks):
                    docs.append(chunk)
                    metadatas.append({"source": filename})
                    ids.append(f"{filename.replace('.pdf', '')}_chunk_{i}")
                    
                logger.info(f"✅ PDF {filename}: {len(chunks)} чанков")
            except Exception as e:
                logger.error(f"❌ Ошибка чтения {filename}: {e}")

    # Загрузка в ChromaDB
    if docs:
        try:
            collection.upsert(documents=docs, metadatas=metadatas, ids=ids)
            logger.info(f"🚀 База обновлена! Загружено {len(docs)} фрагментов")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки в ChromaDB: {e}")
            return False
    else:
        logger.warning("⚠️ Нет данных для загрузки")
        return False

def send_to_odoo_crm(data: dict, image_base64: str):
    """Создает Лид в Odoo CRM"""
    if not all([ODOO_URL, ODOO_USER, ODOO_DB, ODOO_PASSWORD]):
        logger.warning("⚠️ Odoo не настроен")
        return None

    try:
        logger.info("📡 Подключение к Odoo CRM...")
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        
        if not uid:
            logger.error("❌ Ошибка аутентификации Odoo")
            return None

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
        
        desc = f"AI РАСПОЗНАВАНИЕ:\n"
        desc += f"Документ: {data['doc_type']}\n"
        desc += f"Инспектор: {data['inspector_name']}\n"
        desc += f"Дата: {data['date']}\n"
        desc += "-" * 30 + "\n"
        
        for k, v in data['fields'].items():
            desc += f"{k}: {v}\n"
            
        if data.get('rejection_reason'):
            desc += f"\nПРИМЕЧАНИЕ: {data['rejection_reason']}"

        lead_name = f"SCAN: {data['doc_type']} ({data['date']})"
        
        lead_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
            'crm.lead', 'create', [{
                'name': lead_name,
                'description': desc,
                'type': 'opportunity',
                'priority': '2'
            }]
        )
        
        # Прикрепление файла
        if lead_id:
            file_name = f"{data['date']}_{data['doc_type'].replace(' ', '_')}.jpg"
            attachment_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                'ir.attachment', 'create', [{
                    'name': file_name,
                    'type': 'binary',
                    'datas': image_base64,
                    'res_model': 'crm.lead',
                    'res_id': lead_id,
                    'mimetype': 'image/jpeg'
                }]
            )
            logger.info(f"✅ Создан лид {lead_id}, файл {attachment_id}")
        
        return lead_id
        
    except Exception as e:
        logger.error(f"❌ Ошибка Odoo: {e}")
        return None

def clean_json_response(text: str) -> str:
    """Очистка JSON ответа от markdown разметки"""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

# --- 5. ЭНДПОИНТЫ ---

@app.post("/agent/technologist/ask", response_model=AIResponse)
async def ask_technologist(request: QueryRequest):
    """AI-ассистент технолога"""
    if not ai_model:
        raise HTTPException(status_code=503, detail="AI модель недоступна")
    
    try:
        # ВЕРНУЛИ n_results=50 ДЛЯ ГИБРИДНОГО ПОИСКА!
        results = collection.query(query_texts=[request.question], n_results=50)
        retrieved_docs = results['documents'][0] if results['documents'] else []
        context_text = "\n\n".join(retrieved_docs)
        
        logger.info(f"❓ ЗАПРОС: {request.question}")
        logger.info(f"📚 НАЙДЕНО ДОКУМЕНТОВ: {len(retrieved_docs)}")
        
        sources_list = []
        if results.get('metadatas') and results['metadatas'][0]:
            sources_list = list(set([
                m.get('source', 'Unknown') 
                for m in results['metadatas'][0] 
                if m
            ]))
        
        # ВЕРНУЛИ ПРАВИЛО №5 ДЛЯ ТАБЛИЦ!
        prompt = f"""
Ти — професійний B2B AI-асистент компанії, яка постачає інгредієнти для харчової промисловості та пекарень.

Ти ідеально знаєш асортимент двох наших головних брендів:

👑 Бренд "Optima":
- Сухі суміші для випічки (Каталог: Каталог суміші.pdf)
- Базові наповнювачі (Каталог: Наповнювачі.pdf)  
- Шоколадна продукція та декор (Каталог: ChocoCraft.pdf)

🌟 Бренд "Golden Mile":
- Фруктові наповнювачі (гомогенні та гетерогенні)
- Молочні та макові начинки
- Кондитерські наповнювачі, сиропи та штучний мед

ПРАВИЛА ТВОЄЇ РОБОТИ:
1. Відповідай ВИКЛЮЧНО на основі наданого КОНТЕКСТУ.
2. Якщо інформації немає в контексті, чесно скажи про це.
3. Відповідай мовою запиту клієнта.
4. Для конкретних товарів надавай структуровану інформацію: назва, властивості, дозування, фасування.
5. УВАГА ДО НАЗВ ТА СТРУКТУРИ: Якщо клієнт запитує про конкретний товар (наприклад, поліпшувач "Фреш"), ти ПОВИНЕН знайти в тексті точний збіг. Зверни увагу, що через специфіку верстки каталогів, опис та дозування можуть знаходитися ПЕРЕД самою назвою товару. Аналізуй текст навколо.

КОНТЕКСТ (витяг з PDF-каталогів):
{context_text}

ЗАПИТ КЛІЄНТА:
{request.question}
"""
        
        response = ai_model.generate_content(prompt)
        ai_answer = response.text
        
    except Exception as e:
        logger.error(f"❌ Ошибка в ask_technologist: {e}")
        ai_answer = "Вибачте, сталася технічна помилка при обробці запиту."
        sources_list = []

    return AIResponse(answer=ai_answer, sources=sources_list)

@app.post("/agent/doc/digitize", response_model=DigitalForm)
async def digitize_document(file: UploadFile = File(...)):
    """Оцифровка документов"""
    if not ai_model:
        raise HTTPException(status_code=503, detail="AI модель недоступна")
    
    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(
            status_code=400, 
            detail="Файл должен быть изображением"
        )
    
    try:
        contents = await file.read()
        user_image = Image.open(io.BytesIO(contents))
        
        reference_image = None
        try:
            if os.path.exists("data/master_form.jpg"):
                reference_image = Image.open("data/master_form.jpg")
        except Exception as e:
            logger.warning(f"⚠️ Эталон не загружен: {e}")

        prompt = """
Ты — эксперт по оцифровке документов.

ЗАДАЧА: Проанализируй документ и определи:
1. Является ли это валидным документом (не картинка кота/пейзажа)
2. Извлеки все текстовые поля и их значения

Верни строго JSON в формате:
{
    "is_valid": true/false,
    "rejection_reason": "причина отклонения или пустая строка", 
    "doc_type": "тип документа",
    "date": "YYYY-MM-DD",
    "inspector_name": "имя инспектора",
    "fields": {"поле1": "значение1", "поле2": "значение2"}
}
"""
        
        inputs = [prompt]
        if reference_image:
            inputs.extend(["ЭТАЛОН:", reference_image])
        inputs.extend(["АНАЛИЗИРУЕМЫЙ ДОКУМЕНТ:", user_image])
        
        response = ai_model.generate_content(inputs)
        json_text = clean_json_response(response.text)
        data = json.loads(json_text)
        
        odoo_id = None
        if data.get("is_valid"):
            buffered = io.BytesIO()
            user_image.save(buffered, format="JPEG", quality=85)
            img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            odoo_id = send_to_odoo_crm(data, img_str)
        
        data['odoo_id'] = odoo_id
        return DigitalForm(**data)
        
    except json.JSONDecodeError as e:
        logger.error(f"❌ Ошибка парсинга JSON: {e}")
        return DigitalForm(
            is_valid=False,
            rejection_reason="Ошибка обработки ответа AI",
            doc_type="Error", date="", inspector_name="", fields={}
        )
    except Exception as e:
        logger.error(f"❌ Ошибка оцифровки: {e}")
        return DigitalForm(
            is_valid=False,
            rejection_reason=f"Техническая ошибка: {str(e)}",
            doc_type="Error", date="", inspector_name="", fields={}
        )

@app.post("/admin/train_knowledge_base")
async def train_base():
    """Ручное обновление базы знаний"""
    success = update_knowledge_base()
    if success:
        return {"status": "success", "message": "База знаний успешно обновлена"}
    else:
        raise HTTPException(
            status_code=500, 
            detail="Ошибка обновления базы знаний"
        )

@app.get("/health")
async def health_check():
    """Проверка состояния сервиса"""
    status = {
        "status": "healthy",
        "ai_model": ai_model is not None,
        "chromadb": True,
        "odoo_configured": all([ODOO_URL, ODOO_USER, ODOO_DB, ODOO_PASSWORD])
    }
    
    try:
        collection.peek()
        collection_count = collection.count()
        status["knowledge_base_records"] = collection_count
    except Exception as e:
        status["chromadb"] = False
        status["error"] = str(e)
    
    return status

# РАСКОММЕНТИРОВАЛИ! Инициализация базы знаний при старте
update_knowledge_base()