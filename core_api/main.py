import os
import io
import json
import xmlrpc.client
import ssl
import base64
import re
from typing import List, Optional
import chromadb
from chromadb.utils import embedding_functions
from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
import google.generativeai as genai
from PIL import Image


# --- 1. НАСТРОЙКИ ODOO ---
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

ssl._create_default_https_context = ssl._create_unverified_context

# --- 2. НАСТРОЙКИ AI ---
CHROMA_URL = os.getenv("CHROMA_DB_URL", "http://vectordb:8000")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CURRENT_MODEL_NAME = 'gemini-2.5-flash' 

if not GEMINI_KEY:
    print("❌ ОШИБКА: Нет API ключа Gemini!")
else:
    genai.configure(api_key=GEMINI_KEY)

client = chromadb.HttpClient(host='vectordb', port=8000)
emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="paraphrase-multilingual-MiniLM-L12-v2")
collection = client.get_or_create_collection(name="balex_knowledge", embedding_function=emb_fn)

app = FastAPI(title="Balex AI Ecosystem API", version="3.3.0 (CRM Integration)")

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

# --- 4. ФУНКЦИЯ ОТПРАВКИ В CRM ---
def send_to_odoo_crm(data: dict, image_base64: str):
    """
    Создает Лид (crm.lead) и прикрепляет фото.
    """
    try:
        print(f"📡 Подключение к Odoo CRM...")
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        
        if not uid:
            print("❌ Ошибка входа в Odoo")
            return None

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
        
        # 1. Формируем описание для карточки
        desc = f"🤖 AI РАСПОЗНАВАНИЕ:\n"
        desc += f"Документ: {data['doc_type']}\n"
        desc += f"Инспектор: {data['inspector_name']}\n"
        desc += "-"*20 + "\n"
        for k, v in data['fields'].items():
            desc += f"✅ {k}: {v}\n"
            
        if data.get('rejection_reason'):
            desc += f"\n⚠️ ПРИМЕЧАНИЕ: {data['rejection_reason']}"

        lead_name = f"SCAN: {data['doc_type']} ({data['date']})"

        # 2. Создаем ЛИД (crm.lead)
        # priority: '1' (Low), '2' (Medium), '3' (High)
        lead_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
            'crm.lead', 'create', [{
                'name': lead_name,          
                'description': desc,        
                'type': 'opportunity',      
                'priority': '2',            
                'tag_ids': []               
            }]
        )
        print(f"✅ Создан Лид в CRM: ID {lead_id}")

        # 3. Прикрепляем фото 
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
        print(f"📎 Фото прикреплено: ID {attachment_id}")
        
        return lead_id
        
    except Exception as e:
        print(f"❌ Odoo CRM Error: {e}")
        return None

def clean_json_response(text):
    text = text.strip()
    if text.startswith("```json"): text = text[7:]
    if text.startswith("```"): text = text[3:]
    if text.endswith("```"): text = text[:-3]
    return text.strip()

# --- 5. ЭНДПОИНТЫ ---

@app.post("/agent/technologist/ask", response_model=AIResponse)
async def ask_technologist(request: QueryRequest):
    results = collection.query(query_texts=[request.question], n_results=3)
    retrieved_docs = results['documents'][0] if results['documents'] else []
    context_text = "\n\n".join(retrieved_docs)
    
    try:
        model = genai.GenerativeModel(CURRENT_MODEL_NAME)
        response = model.generate_content(f"CONTEXT: {context_text}\nQUESTION: {request.question}")
        ai_answer = response.text
    except Exception as e:
        ai_answer = str(e)

    return AIResponse(answer=ai_answer, sources=[m['source'] for m in results['metadatas'][0]])

@app.post("/agent/doc/digitize", response_model=DigitalForm)
async def digitize_document(file: UploadFile = File(...)):
    contents = await file.read()
    user_image = Image.open(io.BytesIO(contents))
    
    reference_image = None
    try:
        reference_image = Image.open("data/master_form.jpg")
    except: pass

    model = genai.GenerativeModel(CURRENT_MODEL_NAME)
    
    prompt = """
    Ты — эксперт по оцифровке.
    1. Игнорируй рукописный/печатный стиль.
    2. Если смысл документа (поля) совпадает с эталоном -> VALID.
    3. Если это кот или пейзаж -> INVALID.
    
    Верни JSON:
    {
        "is_valid": true, "rejection_reason": "", 
        "doc_type": "Тип", "date": "YYYY-MM-DD", 
        "inspector_name": "Name",
        "fields": {"Поле": "Значение"}
    }
    """
    
    inputs = [prompt]
    if reference_image: inputs.extend(["ЭТАЛОН:", reference_image])
    inputs.extend(["КАНДИДАТ:", user_image])
    
    try:
        response = model.generate_content(inputs)
        json_text = clean_json_response(response.text)
        data = json.loads(json_text)
        
        odoo_id = None
        if data.get("is_valid"):
            buffered = io.BytesIO()
            user_image.save(buffered, format="JPEG")
            img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            
            
            odoo_id = send_to_odoo_crm(data, img_str)
        
        data['odoo_id'] = odoo_id
        return DigitalForm(**data)
        
    except Exception as e:
        return DigitalForm(
            is_valid=False, rejection_reason=f"Error: {str(e)}",
            doc_type="Error", date="", inspector_name="", fields={}
        )

@app.post("/admin/train_knowledge_base")
async def train_base():
    return {"status": "ok"}