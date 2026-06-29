"""
fitz_worker.py — изоляция вызовов PyMuPDF в отдельный процесс.

Зачем: MuPDF (C-ядро под fitz) на битых/нестандартных PDF может падать в
SIGSEGV (реальный кейс: Pixmap на картинке со SMask+ICCBased в файле с битым
xref). Нативный краш убивает весь процесс — поэтому каждую операцию с fitz
гоняем в дочернем процессе. Сегфолт / зависание / нехватка памяти убивают
дочерний, а gunicorn-воркер выживает и отдаёт пользователю понятную ошибку
вместо немого зависания и потери задачи.

Воркер один, sync, без потоков -> fork безопасен и дёшев. На Windows (нет
fork) или при ZHELPRA_NO_ISOLATION -> прямой синхронный вызов (для локальной
отладки на машине разработки).

Намеренно НЕ ставим RLIMIT_AS: MuPDF мапит PDF в адресное пространство, лимит
на виртуалку выбивался бы на легитимных больших книгах. Предохранитель —
таймаут на операцию.
"""

import os
import sys
import time
import queue as _queue
import multiprocessing

import cleaner
from cleaner import EncryptedPDFError

# Таймаут на операцию, секунд.
DEFAULT_TIMEOUTS = {
    'diagnose': 180,
    'clean_image_watermark': 300,
    'clean_form_xobject_watermark': 300,
    'clean_artifact_watermark': 300,
    'clean_annotation_watermark': 300,
    'clean_text_watermark': 300,
    'clean_vector_watermark': 300,
    'replace_placeholders': 300,
    'mark_illustrations': 300,
    'remove_running_content': 300,
    'render_page_preview': 60,
    'render_image_thumbnail': 60,
}

# Изоляция только там, где есть fork (Linux). Локально (Windows) или по флагу —
# прямой вызов, чтобы отладка не требовала forka/сигналов.
_ISOLATION = (sys.platform != 'win32') and not os.environ.get('ZHELPRA_NO_ISOLATION')


class PdfProcessingError(Exception):
    """Обработка PDF не удалась: краш/таймаут/нехватка памяти/прочая ошибка."""

    def __init__(self, user_message, kind='error'):
        super().__init__(user_message)
        self.user_message = user_message
        self.kind = kind


def _entry(func_name, args, kwargs, q):
    """Тело дочернего процесса: выполнить cleaner.<func_name> и вернуть итог через очередь."""
    try:
        result = getattr(cleaner, func_name)(*args, **kwargs)
        q.put(('ok', result))
    except EncryptedPDFError as e:
        q.put(('encrypted', str(e)))
    except Exception as e:
        q.put(('err', f'{type(e).__name__}: {e}'))


def call(func_name, *args, timeout=None, **kwargs):
    """
    Выполнить cleaner.<func_name>(*args, **kwargs) в изолированном процессе.

    Возвращает результат функции. Бросает:
      - EncryptedPDFError    — PDF под паролем (контракт сохранён для app.py);
      - PdfProcessingError   — краш/таймаут/нехватка памяти/ошибка обработки.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUTS.get(func_name, 120)

    # Локальная отладка / Windows: без изоляции.
    if not _ISOLATION:
        try:
            return getattr(cleaner, func_name)(*args, **kwargs)
        except EncryptedPDFError:
            raise
        except Exception as e:
            raise PdfProcessingError(f'Не удалось обработать PDF: {e}', 'error')

    ctx = multiprocessing.get_context('fork')
    q = ctx.Queue()
    p = ctx.Process(target=_entry, args=(func_name, args, kwargs, q), daemon=True)
    p.start()

    # КРИТИЧНО: читать из очереди ДО join. Иначе при крупном результате (PNG-
    # превью) дочерний блокируется на записи в пайп, а мы виснем в join — deadlock.
    # Поллим короткими интервалами, чтобы быстро отличить КРАШ (дочерний умер,
    # ничего не положив) от настоящего ТАЙМАУТА (дочерний ещё жив и завис).
    # Иначе при сегфолте пришлось бы ждать весь timeout и врать «слишком долго».
    status, payload, timed_out = None, None, False
    deadline = time.monotonic() + timeout
    while True:
        try:
            status, payload = q.get(timeout=0.2)
            break
        except _queue.Empty:
            if not p.is_alive():
                break  # дочерний умер не записав — это нативный краш, разберём по exitcode
            if time.monotonic() >= deadline:
                timed_out = True
                break

    # Гарантированно завершить дочерний.
    p.join(timeout=5)
    if p.is_alive():
        p.terminate()
        p.join(timeout=5)
        if p.is_alive():
            p.kill()
            p.join()

    if timed_out:
        raise PdfProcessingError(
            'Обработка заняла слишком долго и была прервана — файл слишком большой или сложный.',
            'timeout')

    if status is None:
        # Дочерний умер, ничего не записав, — это нативный краш. Разбираем по коду.
        code = p.exitcode
        if code is not None and code < 0:
            sig = -code
            if sig in (11, 6, 7):  # SIGSEGV / SIGABRT / SIGBUS
                raise PdfProcessingError(
                    'PDF повреждён — движок не смог его обработать. Попробуй пересохранить '
                    'файл (в Acrobat: «Файл → Сохранить как») и загрузить заново.',
                    'crash')
            if sig == 9:  # SIGKILL — обычно системный OOM-killer
                raise PdfProcessingError(
                    'Не хватило памяти на обработку этого файла. Попробуй файл поменьше '
                    'или разбей на части.',
                    'oom')
        raise PdfProcessingError(f'Обработка не удалась (код {code}).', 'error')

    if status == 'ok':
        return payload
    if status == 'encrypted':
        raise EncryptedPDFError(payload)
    raise PdfProcessingError(f'Не удалось обработать PDF: {payload}', 'error')
