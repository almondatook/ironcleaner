# Железная Прачка — контейнер для Railway (и любого другого Docker-хостинга).
# fonts-dejavu-core обязателен: cleaner.py вставляет кириллицу шрифтом
# /usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf (методы placeholder/illustration).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# -w 1 обязателен: MuPDF не потокобезопасен, jobstore рассчитан на одного писателя.
# Shell-форма CMD нужна, чтобы раскрылся $PORT (Railway передаёт его через окружение).
CMD gunicorn -w 1 -b 0.0.0.0:${PORT:-8000} --timeout 300 --access-logfile - app:app
