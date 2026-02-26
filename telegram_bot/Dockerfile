FROM python:3.9-slim

WORKDIR /app

RUN pip install python-telegram-bot requests google-generativeai

COPY bot.py .

CMD ["python", "bot.py"]