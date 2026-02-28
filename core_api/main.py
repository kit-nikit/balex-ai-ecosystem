import os
import io
import json
import xmlrpc.client
import ssl
import base64
import PyPDF2  
from typing import List, Optional
from datetime import datetime
import chromadb
from chromadb.utils import embedding_functions
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
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

if os.getenv("DEVELOPMENT_MODE") == "true":
    ssl._create_default_https_context = ssl._create_unverified_context

# --- 2. НАСТРОЙКИ AI ---
CHROMA_URL = os.getenv("CHROMA_DB_URL", "http://vectordb:8000")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CURRENT_MODEL_NAME = 'gemini-2.5-flash'  

if not GEMINI_KEY:
    logger.error("❌ ОШИБКА: Нет API ключа Gemini!")
else:
    genai.configure(api_key=GEMINI_KEY)

try:
    ai_model = genai.GenerativeModel(CURRENT_MODEL_NAME)
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

app = FastAPI(title="B-test AI Ecosystem API", version="3.3.3")

# --- MIDDLEWARE & STARTUP ---
@app.middleware("http")
async def count_requests(request: Request, call_next):
    if hasattr(app.state, "request_count"):
        app.state.request_count += 1
    else:
        app.state.request_count = 1
    response = await call_next(request)
    return response

@app.on_event("startup")
async def startup_event():
    app.state.start_time = datetime.now()
    app.state.request_count = 0
    logger.info("🚀 BALEX AI Ecosystem started")
    try:
        update_knowledge_base()
        logger.info("✅ Knowledge base updated")
    except Exception as e:
        logger.error(f"❌ Knowledge base update failed: {e}")

# --- 3. МОДЕЛИ PYDANTIC ---
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

class RecipeRequest(BaseModel):
    product: str
    volume: int
    production_type: Optional[str] = "промислове"

# --- 4. ФУНКЦИИ И ГЕНЕРАТОРЫ ПРОМПТОВ ---
def update_knowledge_base():
    data_dir = "data"
    if not os.path.exists(data_dir):
        return False
    logger.info("⏳ Начинаю обновление базы знаний...")
    
    try:
        existing_ids = collection.get()['ids']
        if existing_ids:
            collection.delete(ids=existing_ids)
    except Exception:
        pass

    docs, metadatas, ids = [], [], []

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
            logger.info(f"✅ Загружен TXT: {len(chunks)} чанков")
        except Exception as e:
            logger.error(f"❌ Ошибка TXT: {e}")

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
                
                chunk_size, overlap = 1500, 300
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
                logger.error(f"❌ Ошибка PDF {filename}: {e}")

    if docs:
        collection.upsert(documents=docs, metadatas=metadatas, ids=ids)
        logger.info(f"🚀 База обновлена! Загружено {len(docs)} фрагментов")
        return True
    return False

def send_to_odoo_crm(data: dict, image_base64: str):
    if not all([ODOO_URL, ODOO_USER, ODOO_DB, ODOO_PASSWORD]):
        return None
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid: return None
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
        
        desc = f"AI РАСПОЗНАВАНИЕ:\nДокумент: {data['doc_type']}\nИнспектор: {data['inspector_name']}\nДата: {data['date']}\n" + "-" * 30 + "\n"
        for k, v in data['fields'].items(): desc += f"{k}: {v}\n"
        if data.get('rejection_reason'): desc += f"\nПРИМЕЧАНИЕ: {data['rejection_reason']}"

        lead_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'crm.lead', 'create', [{
            'name': f"SCAN: {data['doc_type']} ({data['date']})",
            'description': desc,
            'type': 'opportunity', 'priority': '2'
        }])
        
        if lead_id:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'ir.attachment', 'create', [{
                'name': f"{data['date']}_{data['doc_type'].replace(' ', '_')}.jpg",
                'type': 'binary', 'datas': image_base64,
                'res_model': 'crm.lead', 'res_id': lead_id, 'mimetype': 'image/jpeg'
            }])
        return lead_id
    except Exception as e:
        logger.error(f"❌ Ошибка Odoo: {e}")
        return None

def clean_json_response(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"): text = text[7:]
    elif text.startswith("```"): text = text[3:]
    if text.endswith("```"): text = text[:-3]
    return text.strip()

def build_technologist_prompt(question: str, context_text: str, sources: list = None) -> str:
    catalog_info = ""
    if sources:
        unique_sources = list(set(sources))
        catalog_info = f"\n📚 **Доступні каталоги:** {', '.join(unique_sources)}\n"
    
    return f"""
**ТИ — ГОЛОВНИЙ ТЕХНОЛОГ КОМПАНІЇ ** з 15+ років досвіду в харчовій промисловості.

** ТВОЯ РОЛЬ:**
Консультуєш B2B клієнтів з підбору інгредієнтів та розробки рецептур на основі асортименту.

**📦 НАША ПРОДУКЦІЯ:**
**Бренд "Optima":** Сухі суміші для випічки, Поліпшувачі хліба, Базові наповнювачі, Шоколадна продукція (ChocoCraft).
**Бренд "Golden Mile":** Фруктові наповнювачі, Молочні начинки, Макові начинки, Кондитерські наповнювачі, Сиропи, Мед штучний.

** ЛОГІКА ТВОЄЇ РОБОТИ (Chain of Thought):**
1. **Аналізуй запит:** Що шукає клієнт?
2. **Перевіряй контекст:** Чи є точні назви, дозування?
3. **Особливості PDF:** Опис та дозування можуть знаходитися ПЕРЕД або ПІСЛЯ назви товару.
4. **Перевіряй вхідні дані для розрахунків:** Якщо просять розрахунок, але НЕ вказали об'єм → ЗУПИНИСЬ і запитай. Якщо ВКАЗАЛИ → роби розрахунок.

**⚖️ КРИТИЧНІ ПРАВИЛА:**
** ЗАБОРОНА НА ВИГАДКИ:** Використовуй ВИКЛЮЧНО інформацію з контексту. НЕ вигадуй дозування.
**🧮 ЗАБОРОНА НА УМОВНІ РОЗРАХУНКИ:** Якщо немає об'єму виробництва (у шт чи кг), ТИ МАЄШ відповісти:
*"Для точного розрахунку рецептури та собівартості, будь ласка, уточніть планований об'єм виробництва (наприклад: 500 еклерів/день або 50 кг тіста/день). Тоді я зможу підібрати оптимальне рішення!"*
** МОВНИЙ БАР'ЄР ТА ЧИСТОТА:** - Якщо в каталозі назва або опис вказані англійською (або іншою мовою), ОБОВ'ЯЗКОВО переклади їх на українську.
- НЕ пиши фрази типу "зі сторінки 10" або "з англійської частини". Видавай лише чисту комерційну пропозицію.

**СТРУКТУРА ВІДПОВІДІ:**
Для **конкретних товарів**: Назва, Властивості, Дозування, Фасування.
Для **комплексних рішень** (якщо є об'єм):
**БАЗОВА СУМІШ (Optima):** [Назва] | Дозування: [г на кг тіста]
**НАЧИНКА (Golden Mile):** [Назва] | Термостабільність | Дозування: [г на виріб]
**РОЗРАХУНОК ПОТРЕБИ:** Денна: [X] кг суміші + [Y] кг начинки. Місячна (22 дні): [X*22] кг + [Y*22] кг. Рекомендована фасовка.

** МОВА ТА СТИЛЬ:** Професійний, дружній, мовою запиту клієнта.
{catalog_info}
**📄 КОНТЕКСТ З КАТАЛОГІВ:**
{context_text}
**❓ ЗАПИТ КЛІЄНТА:**
{question}
"""

def build_recipe_calculator_prompt(product: str, volume: int, context: str) -> str:
    return f"""
**ТИ — ГОЛОВНИЙ ТЕХНОЛОГ GOLDEN MILE/BALEX.** Розраховуєш рецептуру для B2B клієнта.

**ВИХІДНІ ДАНІ:**
- Продукт: {product}
- Об'єм виробництва: {volume} шт/день

**ЗАВДАННЯ:**
1. Підбери БАЗОВУ СУМІШ Optima (точна назва з каталогу).
2. Підбери НАЧИНКУ Golden Mile (врахуй термостабільність!).
3. Розрахуй денну та місячну (22 робочі дні) потребу в кілограмах.
4. Порекомендуй оптимальну фасовку для закупів дозування з контексту.

**⚖️ ВАЖЛИВО:** 1. Використовуй ТІЛЬКИ дозування з контексту. Якщо немає — пиши "Потрібна додаткова консультація".
2.  **МОВНИЙ БАР'ЄР:** Якщо в каталозі назва або опис вказані англійською, ОБОВ'ЯЗКОВО переклади їх на українську (наприклад, "Poppy seed filling" -> "Макова начинка"). Уся відповідь має бути українською мовою.
3.  **ЖОДНОГО МЕТА-ТЕКСТУ:** НЕ пиши номери сторінок (наприклад, "зі сторінки 82") або фрази "з каталогу". Клієнту потрібен готовий бізнес-звіт.

**КОНТЕКСТ З БАЗИ ЗНАНЬ:** {context}

**ФОРМАТ ВІДПОВІДІ:**
**1. Рекомендовані інгредієнти:**
- **Суміш (Optima):** [Назва українською] | Дозування: [Х] г на 1 кг
- **Начинка (Golden Mile):** [Назва українською] | Дозування: [Х] г на 1 шт
**2. Розрахунок потреби (на {volume} шт/день):**
- **На день:** [Х] кг суміші, [Y] кг начинки
- **На місяць (22 дні):** [Х] кг суміші, [Y] кг начинки
**3. Рекомендація щодо закупівлі:** [Фасовка з каталогу]
"""

# --- ЭНДПОИНТЫ ---
@app.post("/agent/technologist/ask", response_model=AIResponse)
async def ask_technologist(request: QueryRequest):
    if not ai_model: raise HTTPException(status_code=503, detail="AI модель недоступна")
    try:
        results = collection.query(query_texts=[request.question], n_results=50)
        retrieved_docs = results['documents'][0] if results['documents'] else []
        context_text = "\n\n".join(retrieved_docs)
        
        sources_list = []
        if results.get('metadatas') and results['metadatas'][0]:
            sources_list = [m.get('source', 'Unknown') for m in results['metadatas'][0] if m]
        
        prompt = build_technologist_prompt(request.question, context_text, sources_list)
        response = ai_model.generate_content(prompt)
        return AIResponse(answer=response.text, sources=list(set(sources_list)))
    except Exception as e:
        logger.error(f"❌ Ошибка ask_technologist: {e}")
        return AIResponse(answer="Вибачте, сталася технічна помилка.", sources=[])

@app.post("/agent/recipe/calculate")
async def calculate_recipe(request: RecipeRequest):
    if not ai_model: raise HTTPException(status_code=503, detail="AI недоступен")
    try:
        search_query = f"{request.product} начинка суміш дозування рецептура"
        results = collection.query(query_texts=[search_query], n_results=50)
        context = "\n\n".join(results['documents'][0] if results['documents'] else [])
        
        prompt = build_recipe_calculator_prompt(request.product, request.volume, context)
        response = ai_model.generate_content(prompt)
        
        sources_list = []
        if results.get('metadatas') and results['metadatas'][0]:
            sources_list = [m.get('source', 'Unknown') for m in results['metadatas'][0] if m]
            
        return {
            "success": True, "product": request.product, "volume": request.volume,
            "recommendation": response.text, "sources": list(set(sources_list))
        }
    except Exception as e:
        logger.error(f"Recipe calculation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/agent/doc/digitize", response_model=DigitalForm)
async def digitize_document(file: UploadFile = File(...)):
    if not ai_model: raise HTTPException(status_code=503, detail="AI недоступна")
    try:
        contents = await file.read()
        user_image = Image.open(io.BytesIO(contents))
        
        prompt = """Ты эксперт по оцифровке. Определи: 1. Валидный ли документ. 2. Извлеки поля. Верни JSON: {"is_valid": true, "rejection_reason": "", "doc_type": "тип", "date": "YYYY-MM-DD", "inspector_name": "имя", "fields": {"поле": "значение"}}"""
        response = ai_model.generate_content([prompt, user_image])
        data = json.loads(clean_json_response(response.text))
        
        odoo_id = None
        if data.get("is_valid"):
            buffered = io.BytesIO()
            user_image.save(buffered, format="JPEG", quality=85)
            img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            odoo_id = send_to_odoo_crm(data, img_str)
        
        data['odoo_id'] = odoo_id
        return DigitalForm(**data)
    except Exception as e:
        logger.error(f"❌ Ошибка оцифровки: {e}")
        return DigitalForm(is_valid=False, rejection_reason=str(e), doc_type="Error", date="", inspector_name="", fields={})

@app.post("/admin/train_knowledge_base")
async def train_base():
    success = update_knowledge_base()
    if success: return {"status": "success", "message": "База знаний успешно обновлена"}
    raise HTTPException(status_code=500, detail="Ошибка обновления")

@app.get("/health")
async def health_check():
    health = {
        "status": "healthy", "timestamp": datetime.now().isoformat(),
        "uptime_seconds": (datetime.now() - app.state.start_time).total_seconds(), "services": {}
    }
    health["services"]["gemini"] = {"status": "operational", "model": CURRENT_MODEL_NAME} if ai_model else {"status": "unavailable"}
    try:
        health["services"]["chromadb"] = {"status": "operational", "records": collection.count()}
    except Exception as e:
        health["services"]["chromadb"] = {"status": "error", "error": str(e)}
    health["services"]["odoo"] = {"status": "configured" if all([ODOO_URL, ODOO_USER, ODOO_DB, ODOO_PASSWORD]) else "not_configured"}
    return health

@app.get("/metrics")
async def get_metrics():
    return {
        "total_requests": getattr(app.state, "request_count", 0),
        "knowledge_base_size": collection.count(),
        "uptime_seconds": (datetime.now() - app.state.start_time).total_seconds(),
        "model": CURRENT_MODEL_NAME
    }