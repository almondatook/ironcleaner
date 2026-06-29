"""
jobstore.py — durable-хранилище задач Железной Прачки.

Метаданные задачи лежат на диске как JSON-сайдкар <job_id>.json рядом с PDF
в WORK_DIR. Это переживает рестарт/краш воркера: задача находится по job_id из
ссылки браузера, даже если процесс перезапустился. (Раньше jobs был dict в
памяти процесса и терялся при любом рестарте -> «Задача не найдена».)

Воркер один (gunicorn -w 1, sync) — конкурентных писателей нет.

Безопасность: job_id валидируется как 32 hex-символа, а пути к файлам ВСЕГДА
деривируются из job_id, а не берутся из содержимого JSON. Это закрывает path
traversal (job_id приходит из URL) и не даёт сайдкару нести произвольный путь.
"""

import os
import re
import json
import time
import tempfile
from pathlib import Path

WORK_DIR = Path(tempfile.gettempdir()) / 'zhelpra_jobs'
WORK_DIR.mkdir(exist_ok=True)

MAX_JOB_AGE = 3600  # секунд; файлы старше — удаляются
_JOB_ID_RE = re.compile(r'\A[0-9a-f]{32}\Z')


def valid_id(job_id):
    return bool(job_id) and bool(_JOB_ID_RE.match(job_id))


# --- пути всегда из job_id, не из JSON ---
def input_path(job_id):
    return str(WORK_DIR / f'{job_id}_in.pdf')


def output_path(job_id):
    return str(WORK_DIR / f'{job_id}_clean.pdf')


def csv_path(job_id):
    return str(WORK_DIR / f'{job_id}_illustrations.csv')


def _meta_path(job_id):
    return WORK_DIR / f'{job_id}.json'


def save(job):
    """Атомарно записать метаданные задачи (tmp + os.replace). Сбой диска не роняет запрос."""
    job_id = job.get('job_id')
    if not valid_id(job_id):
        raise ValueError('bad job_id')
    p = _meta_path(job_id)
    tmp = p.with_name(p.name + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(job, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def load(job_id):
    """Прочитать задачу с диска. None, если нет или job_id невалиден."""
    if not valid_id(job_id):
        return None
    try:
        with open(_meta_path(job_id), encoding='utf-8') as f:
            job = json.load(f)
    except (OSError, ValueError):
        return None
    # пути пересобираем из job_id — не доверяем содержимому файла
    job['job_id'] = job_id
    job['input_path'] = input_path(job_id)
    if job.get('output_path'):
        job['output_path'] = output_path(job_id)
    if job.get('csv_path'):
        job['csv_path'] = csv_path(job_id)
    return job


def update(job_id, **patch):
    job = load(job_id)
    if job is None:
        return None
    job.update(patch)
    save(job)
    return job


def cleanup_old():
    """Удалить ВСЁ в WORK_DIR старше MAX_JOB_AGE (сайдкары, PDF, CSV, осиротевшие tmp)."""
    now = time.time()
    try:
        entries = list(WORK_DIR.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if now - p.stat().st_mtime <= MAX_JOB_AGE:
                continue
            p.unlink()
        except OSError:
            pass
