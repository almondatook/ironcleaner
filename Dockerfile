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
# --timeout 1800, а не 300: /clean гоняет стратегии цепочкой, и каждый шаг имеет
# собственный бюджет до 300 с в fitz_worker — иначе мастер убьёт единственного
# воркера посреди длинной стирки. От зависаний защищают пооперационные таймауты.
CMD gunicorn -w 1 -b 0.0.0.0:${PORT:-8000} --timeout 1800 --access-logfile - app:app
