# 🧺 Железная Прачка

Веб-приложение для подготовки книжных PDF к переводу: снимает водяные знаки,
вычищает колонтитулы и техномусор вёрстки, размечает иллюстрации. Загружаешь PDF
в браузере — апп сам определяет, что за зверь, и предлагает способ чистки.
Терминал не нужен.

Стек: **Flask + PyMuPDF (fitz)**. Фронт — один статический HTML без сборки.

## Что умеет (9 методов)

| Метод | Что это | Как лечим |
|---|---|---|
| **image** | Растровый watermark (image XObject, в т.ч. со SMask), общий объект на многих страницах | Подмена image-stream на 1×1 прозрачный пиксель — чистит все страницы разом |
| **form_xobject** | InDesign Form XObject с маркером `/Private /Watermark` | Обнуление stream всех помеченных объектов |
| **artifact** | Текстовый watermark в content stream, обёрнутый в `/Artifact … BDC … EMC` | Вырезание блока из потока (текст страницы не страдает) |
| **annotation** | Watermark/Stamp как PDF-аннотация | `delete_annot()` |
| **text** | Текстовый watermark по введённому тексту | Умное вырезание: Artifact-блоки + повёрнутые BT…ET, откат на redact для горизонтальных |
| **vector** | Полупрозрачный векторный watermark (подложка, печать) | Вырезание блоков рисования `q…Q` с прозрачным ExtGState (`ca/CA < 0.5`); непрозрачный контент не трогаем |
| **placeholder** | НЕ watermark: текстовые метки-заглушки картинок `Intro001_001_<ISBN>.jpg` | Замена на «<иллюстрация на листе N>» (номер листа PDF) + CSV-отчёт |
| **illustration** | НЕ watermark: реальные растровые иллюстрации с подписями | Под картинкой метка «<иллюстрация на листе N>», под подписью «<подпись под иллюстрацией>» (жёлтый highlight); картинки/подписи остаются. CSV-отчёт |
| **headers** | НЕ watermark: повторяющиеся колонтитулы и техмусор (номера страниц, автор, название главы, имя `.indd`-файла, даты экспорта) | Удаление строк в полях страниц, повторяющихся в той же позиции (цифры нормализуются → номера ловятся одним паттерном). Основной текст и сноски не трогаются |

Можно отметить **несколько методов сразу** — применятся цепочкой за один проход
(сначала снятие знаков, потом колонтитулы, в конце — метки иллюстраций).

## Надёжность

- **Изоляция от крашей** (`fitz_worker.py`): каждый вызов PyMuPDF идёт в отдельном
  процессе. Битый PDF может уронить MuPDF в SIGSEGV — но умрёт только дочерний
  процесс, а веб-воркер выживет и отдаст понятную ошибку. Предохранитель — таймаут.
- **Durable-задачи** (`jobstore.py`): метаданные задач лежат на диске, переживают
  рестарт воркера. `job_id` валидируется (защита от path traversal).

## Что НЕ умеет (честно)

- **Непрозрачный векторный watermark** (нарисованный кривыми без прозрачности) —
  определяет, не снимает (полупрозрачный — снимает метод `vector`).
- **Растеризованные страницы** (вся страница = одна картинка с впечённым знаком) —
  нужен OCR (Tesseract/OCRmyPDF), это другой движок.
- **Неповторяющиеся колонтитулы** — метод `headers` снимает только то, что
  повторяется в полях на ≥2 страницах.

## Запуск локально

```bash
pip install -r requirements.txt
python app.py
```
Открой `http://localhost:5000`.

> **Важно про шрифт.** Метод `placeholder`/`illustration` вставляет кириллицу
> шрифтом `/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf`. На Linux он
> обычно есть (пакет `fonts-dejavu`). Если нет — `sudo apt install fonts-dejavu`,
> либо поправь путь `_CYR_FONT` в начале `cleaner.py` на любой TTF с кириллицей.

---

## Деплой на Railway

В репозитории уже есть всё нужное: `Dockerfile` (Python 3.12 + `fonts-dejavu-core`,
gunicorn слушает `$PORT`) и `railway.toml` (healthcheck на `/healthz`, рестарты
при падении). Railway подхватывает их автоматически.

1. [railway.com](https://railway.com) → **New Project → Deploy from GitHub repo** →
   выбери этот репозиторий. Railway увидит Dockerfile и соберёт образ сам.
2. В сервисе открой **Settings → Networking → Generate Domain** — получишь
   публичный URL вида `*.up.railway.app` (HTTPS из коробки, certbot не нужен).
3. Каждый push в `main` → автодеплой.

Нюансы:

- **Авторизации нет**: приложение открыто любому, у кого есть URL. Если нужен
  пароль — повесь его на уровне прокси/хостинга (на VPS это делает nginx,
  см. ниже) или не публикуй ссылку.
- **Файсистема эфемерная**: загруженные PDF живут в `/tmp` контейнера и
  пропадают при редеплое. Для Прачки это норма — задачи и так удаляются через
  час, ничего ценного на диске нет. Volume не нужен.
- **Один воркер** (`-w 1` в Dockerfile) — по тем же причинам, что и на VPS:
  MuPDF не потокобезопасен. Тяжёлые файлы обрабатываются последовательно.
- Свой домен подключается в **Settings → Networking → Custom Domain** (CNAME).

## Деплой на свой VPS

Ниже — полная инструкция, как поднять свой экземпляр. Замени `your-domain.example`
на свой домен, `you@example.com` — на свой email (для Let's Encrypt).

### 1. Зависимости

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv fonts-dejavu nginx apache2-utils certbot python3-certbot-nginx
```

### 2. Код и виртуальное окружение

```bash
sudo mkdir -p /opt/prachka && cd /opt/prachka
# скопируйте сюда файлы проекта (git clone своего форка или rsync)
python3 -m venv venv
venv/bin/pip install -r requirements.txt
sudo chown -R www-data:www-data /opt/prachka
```

### 3. systemd-сервис (`/etc/systemd/system/prachka.service`)

```ini
[Unit]
Description=Prachka (PDF watermark cleaner)
After=network.target

[Service]
User=www-data
Group=www-data
Environment=HOME=/tmp
WorkingDirectory=/opt/prachka
ExecStart=/opt/prachka/venv/bin/gunicorn -w 1 -b 127.0.0.1:8000 --timeout 300 app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now prachka
```

> **Только 1 воркер (`-w 1`), без потоков.**
> 1. Задачи (`jobs`) хранятся durable на диске, но один процесс — единственный
>    источник правды без гонок.
> 2. PyMuPDF/MuPDF **не потокобезопасен** — с `--threads` параллельные запросы
>    роняют движок. Поэтому sync-воркер.
>
> `Environment=HOME=/tmp` нужен, иначе gunicorn 26 пишет в лог безобидный
> `Permission denied: '/var/www/.gunicorn'`.

### 4. nginx + Basic Auth + HTTPS

Заведи логин/пароль (приложение лучше держать под паролем — оно жуёт чужие PDF
и торчит в интернет):

```bash
sudo htpasswd -c /etc/nginx/.htpasswd yourlogin
```

Конфиг `/etc/nginx/sites-available/prachka`:

```nginx
server {
    listen 80;
    server_name your-domain.example;
    client_max_body_size 300M;          # под лимит Flask, иначе 413 на загрузке

    # статика (логотип, og:image) — без пароля, чтобы превью в соцсетях работало
    location ^~ /static/ {
        auth_basic off;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }

    auth_basic "Prachka";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/prachka /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your-domain.example -m you@example.com --agree-tos --redirect
```

certbot сам добавит `listen 443 ssl`, сертификат и редирект HTTP→HTTPS, а также
настроит автопродление. Сертификат Let's Encrypt живёт 90 дней и обновляется сам.

> **Нюанс с Basic Auth + certbot.** Если весь сайт под паролем, ACME-проверка
> упрётся в 401. Добавь до получения сертификата исключение:
> ```nginx
> location ^~ /.well-known/acme-challenge/ { auth_basic off; allow all; root /var/www/html; }
> ```

## Структура

```
prachka/
├── app.py              # Flask: эндпоинты /diagnose, /clean, /preview, /thumb, /download
├── cleaner.py          # вся PDF-логика: детекторы + чистильщики + open_pdf
├── fitz_worker.py      # изоляция вызовов PyMuPDF в отдельный процесс (защита от segfault)
├── jobstore.py         # durable-задачи на диске
├── requirements.txt
├── static/             # логотип, favicon, og:image
├── templates/
│   └── index.html      # одностраничный фронт
└── README.md
```

## Эндпоинты

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/` | страница |
| POST | `/diagnose` | загрузка + диагностика → список стратегий |
| POST | `/clean` | применить выбранные стратегии (список, цепочкой) |
| GET | `/preview/<job>/<original\|clean>?page=N` | PNG страницы |
| GET | `/thumb/<job>/<xref>` | эскиз картинки-кандидата |
| GET | `/download/<job>` | чистый PDF |
| GET | `/download_csv/<job>` | CSV иллюстраций (после placeholder/illustration) |

## Хранение и приватность

- Файлы — в `/tmp/<...>_jobs/`, удаляются автоматически через час.
- Никаких баз, никакого логирования содержимого.

## Лицензия

MIT (или укажите свою).
