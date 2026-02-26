FROM python:3.11-slim

WORKDIR /app


RUN apt-get update && apt-get install -y build-essential curl && rm -rf /var/lib/apt/lists/*


RUN pip install --no-cache-dir fastapi uvicorn google-generativeai chromadb pillow python-multipart pydantic requests sentence-transformers

# Запускаем сервер
CMD ["uvicorn", "core_api.main:app", "--host", "0.0.0.0", "--port", "8000"]