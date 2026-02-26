import xmlrpc.client
import json
from datetime import datetime

# --- 1. НАСТРОЙКИ ODOO ---
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

TARGET_MODEL = "res.partner"


ai_json_output = """
{
    "doc_type": "Журнал температур",
    "date": "2024-05-20",
    "inspector_name": "Петров А.В.",
    "fields": {
        "Линия 1 (Печь)": "210 C",
        "Линия 1 (Охладитель)": "15 C",
        "Влажность цеха": "55%"
    },
    "raw_text": "Отчет смены. Температура в норме."
}
"""

def push_to_odoo(json_data):
    print("=== Начинаем отправку в Odoo ===")
    data = json.loads(json_data)

    
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            print("ОШИБКА: Не удалось войти в Odoo. Проверь логин/пароль.")
            return
        print(f"Успешный вход! UID пользователя: {uid}")
    except Exception as e:
        print(f"Ошибка подключения: {e}")
        return

    
    
    report_body = f"""
    === АВТОМАТИЧЕСКИЙ ОТЧЕТ ОТ AI AGENT ===
    Тип документа: {data['doc_type']}
    Дата документа: {data['date']}
    Ответственный: {data['inspector_name']}
    
    --- РАСПОЗНАННЫЕ ДАННЫЕ ---
    """
    
    for key, value in data['fields'].items():
        report_body += f"{key}: {value}\n"
        
    report_body += f"\n--- Сырой текст ---\n{data['raw_text']}"

    
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    
    
    record_id = models.execute_kw(
        ODOO_DB, 
        uid, 
        ODOO_PASSWORD,
        TARGET_MODEL,  
        'create',      
        [{             
            'name': f"AI OTCHET: {data['doc_type']} от {data['date']}", 
            'comment': report_body,  
            'type': 'other'
        }]
    )
    
    print(f"УСПЕХ! Запись создана в Odoo. ID новой записи: {record_id}")
    print("=== Завершено ===")

if __name__ == "__main__":
    
    push_to_odoo(ai_json_output)
