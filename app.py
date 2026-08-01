"""
app.py — Flask backend для Железной Прачки v2.

Локально:   python app.py
На VPS:     gunicorn -w 1 -b 127.0.0.1:8000 --timeout 300 app:app
            (-w 1 обязателен: MuPDF не потокобезопасен, а jobs durable на диске.)

Задачи хранятся durable на диске (jobstore) — переживают рестарт воркера.
Все вызовы PyMuPDF идут через fitz_worker — изоляция от нативных крашей.
"""

from flask import Flask, request, render_template, send_file, jsonify, Response
import os
import uuid
import csv
import time

import jobstore
import fitz_worker
from cleaner import EncryptedPDFError
from fitz_worker import PdfProcessingError

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024  # 300 MB


@app.route('/healthz')
def healthz():
    # healthcheck для Railway (см. railway.toml)
    return 'ok'


# Карта стратегий -> функция cleaner. Имена совпадают с функциями в cleaner.py.
_CLEAN_FUNCS = {
    'image': 'clean_image_watermark',
    'form_xobject': 'clean_form_xobject_watermark',
    'artifact': 'clean_artifact_watermark',
    'annotation': 'clean_annotation_watermark',
    'text': 'clean_text_watermark',
    'placeholder': 'replace_placeholders',
    'vector': 'clean_vector_watermark',
    'illustration': 'mark_illustrations',
    'headers': 'remove_running_content',
}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/diagnose', methods=['POST'])
def api_diagnose():
    jobstore.cleanup_old()
    if 'file' not in request.files:
        return jsonify({'error': 'Файл не загружен'}), 400
    file = request.files['file']
    if not file.filename or not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Нужен PDF-файл'}), 400

    watermark_text = (request.form.get('watermark_text') or '').strip() or None
    job_id = uuid.uuid4().hex
    input_path = jobstore.input_path(job_id)
    file.save(input_path)

    try:
        report = fitz_worker.call('diagnose', input_path, watermark_text=watermark_text)
    except EncryptedPDFError as e:
        return jsonify({'error': str(e), 'encrypted': True}), 400
    except PdfProcessingError as e:
        return jsonify({'error': e.user_message}), 422
    except Exception as e:
        return jsonify({'error': f'Не смогла прочитать PDF: {e}'}), 500

    jobstore.save({
        'job_id': job_id,
        'input_path': input_path,
        'output_path': None,
        'csv_path': None,
        'original_name': file.filename,
        'watermark_text': watermark_text,
        'report': report,
        'created_at': time.time(),
    })
    return jsonify({'job_id': job_id, 'report': report})


# Порядок применения при мультивыборе: сначала снятие знаков, потом колонтитулы,
# в конце — пометки (чтобы вставленные метки не попали под удаление колонтитулов).
_STRATEGY_ORDER = {
    'image': 1, 'form_xobject': 1, 'artifact': 1, 'text': 1, 'vector': 1, 'annotation': 1,
    'headers': 2,
    'placeholder': 3, 'illustration': 3,
}


def _clean_args(strategy, params, inp, out, job):
    """Собрать (args, kwargs) для cleaner-функции. ValueError при нехватке параметров."""
    args, kwargs = [inp, out], {}
    if strategy == 'image':
        xref = params.get('xref')
        if not xref:
            raise ValueError('Не указан xref картинки')
        args.append(int(xref))
    elif strategy == 'artifact':
        text = params.get('text')
        if not text:
            raise ValueError('Нет текста водяного знака')
        args.append(text)
    elif strategy == 'text':
        text = params.get('text') or job.get('watermark_text')
        if not text:
            raise ValueError('Нужен текст водяного знака')
        args.append(text)
    elif strategy == 'placeholder':
        kwargs['isbns'] = params.get('isbns') or None
    elif strategy == 'vector':
        kwargs['threshold'] = float(params.get('threshold', 0.5))
    # form_xobject / annotation / headers — без доп. параметров
    return args, kwargs


@app.route('/clean', methods=['POST'])
def api_clean():
    data = request.get_json(force=True)
    job_id = data.get('job_id')

    job = jobstore.load(job_id)
    if job is None:
        return jsonify({'error': 'Задача не найдена или истёк срок хранения'}), 404

    # Поддерживаем и список стратегий (мультивыбор), и одиночную (обратная совместимость).
    steps = data.get('strategies')
    if not steps:
        steps = [{'strategy': data.get('strategy'), 'params': data.get('params') or {}}]
    steps = [s for s in steps if s.get('strategy') in _CLEAN_FUNCS]
    if not steps:
        return jsonify({'error': 'Не выбрана ни одна поддерживаемая стратегия'}), 400
    steps.sort(key=lambda s: _STRATEGY_ORDER.get(s['strategy'], 99))

    final_out = jobstore.output_path(job_id)
    current = job['input_path']
    tmp_files, results = [], []
    csv_written = False

    try:
        for i, step in enumerate(steps):
            strategy = step['strategy']
            params = step.get('params') or {}
            is_last = (i == len(steps) - 1)
            out = final_out if is_last else str(jobstore.WORK_DIR / f'{job_id}_step{i}.pdf')
            try:
                args, kwargs = _clean_args(strategy, params, current, out, job)
            except ValueError as e:
                return jsonify({'error': str(e)}), 400

            result = fitz_worker.call(_CLEAN_FUNCS[strategy], *args, **kwargs)

            # CSV-репорт (placeholder: page+filename; illustration: page+caption) — первый, что дал report.
            if result.get('report') and not csv_written:
                rows = result['report']
                fields = list(rows[0].keys()) if rows else ['page']
                csv_path = jobstore.csv_path(job_id)
                try:
                    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
                        w = csv.DictWriter(f, fieldnames=fields)
                        w.writeheader()
                        w.writerows(rows)
                    job['csv_path'] = csv_path
                    csv_written = True
                except OSError:
                    pass

            results.append({'strategy': strategy, **{k: v for k, v in result.items() if k != 'report'}})
            if not is_last:
                tmp_files.append(out)
            current = out
    except EncryptedPDFError as e:
        return jsonify({'error': str(e), 'encrypted': True}), 400
    except PdfProcessingError as e:
        return jsonify({'error': e.user_message}), 422
    except Exception as e:
        return jsonify({'error': f'Сорвалось при очистке: {e}'}), 500
    finally:
        for t in tmp_files:
            try:
                os.remove(t)
            except OSError:
                pass

    job['output_path'] = final_out
    jobstore.save(job)
    total = sum(r.get('removed', 0) for r in results)
    return jsonify({'success': True, 'steps': results, 'removed': total})


@app.route('/preview/<job_id>/<which>')
def preview(job_id, which):
    job = jobstore.load(job_id)
    if job is None:
        return 'Not found', 404
    page = int(request.args.get('page', 0))
    path = job['input_path'] if which == 'original' else job.get('output_path')
    if which not in ('original', 'clean') or not path:
        return 'Чистая версия ещё не готова', 404
    try:
        png = fitz_worker.call('render_page_preview', path, page)
        return Response(png, mimetype='image/png')
    except PdfProcessingError as e:
        return e.user_message, 422
    except Exception as e:
        return f'Ошибка рендера: {e}', 500


@app.route('/thumb/<job_id>/<int:xref>')
def thumb(job_id, xref):
    job = jobstore.load(job_id)
    if job is None:
        return 'Not found', 404
    try:
        data, ext = fitz_worker.call('render_image_thumbnail', job['input_path'], xref)
        mime = 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'
        return Response(data, mimetype=mime)
    except PdfProcessingError as e:
        return e.user_message, 422
    except Exception as e:
        return f'Ошибка: {e}', 500


@app.route('/download/<job_id>')
def download(job_id):
    job = jobstore.load(job_id)
    if job is None:
        return 'Not found', 404
    if not job.get('output_path'):
        return 'Чистая версия ещё не готова', 404
    base = os.path.basename(job.get('original_name') or 'document.pdf')
    name = base.rsplit('.', 1)[0] + '_clean.pdf'
    return send_file(job['output_path'], as_attachment=True, download_name=name)


@app.route('/download_csv/<job_id>')
def download_csv(job_id):
    job = jobstore.load(job_id)
    if job is None or not job.get('csv_path'):
        return 'CSV не найден', 404
    return send_file(job['csv_path'], as_attachment=True, download_name='illustrations.csv')


if __name__ == '__main__':
    # debug включается только явно через FLASK_DEBUG=1 (debug=True наружу = RCE).
    # На проде всё равно gunicorn, см. README.
    debug = os.environ.get('FLASK_DEBUG') == '1'
    app.run(debug=debug, host='127.0.0.1', port=5000)
