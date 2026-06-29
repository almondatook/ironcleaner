"""
cleaner.py — мозги Железной Прачки v2.

Снимаем водяные знаки шести разновидностей + меняем placeholder-ы иллюстраций.

МЕТОДЫ:
  1. image          — растровый watermark (image XObject, в т.ч. со SMask),
                      общий объект на много страниц. Подмена на 1×1 пиксель.
  2. form_xobject   — InDesign Form XObject, помеченный /Private /Watermark.
                      Обнуление stream'а.
  3. artifact       — текстовый watermark, впечённый в content stream и
                      обёрнутый в маркер /Artifact ... BDC ... EMC.
                      Вырезание блока.
  4. annotation     — водяной знак-аннотация (тип Watermark/Stamp).
  5. text           — текстовый watermark по введённому тексту: умное
                      вырезание из потока (Artifact + повёрнутые BT…ET),
                      с откатом на redact для горизонтального изолированного.
  6. placeholder    — НЕ watermark: замена placeholder-ов иллюстраций вида
                      Intro001_001_<ISBN>.jpg на «<иллюстрация на листе XX>».

  7. vector          — полупрозрачный векторный watermark (подложка/печать):
                      вырезаем блоки рисования q...Q с прозрачным ExtGState
                      (ca/CA < threshold), не трогая непрозрачный контент.

Непрозрачный векторный watermark — только сигнал, автоматически не снимаем.

Content-stream-операции делаются по xref через update_stream — основной текст
страницы не страдает, потому что трогаем только участки потока самого знака.
"""

import fitz  # PyMuPDF
import zlib
import re
from collections import Counter


# ============================================================
# ИСКЛЮЧЕНИЯ
# ============================================================

class EncryptedPDFError(Exception):
    """PDF требует пароль, который мы не знаем."""
    pass


# ============================================================
# ОТКРЫТИЕ PDF (с обработкой шифрования)
# ============================================================

def open_pdf(path):
    """
    Открывает PDF. Permission-encryption с пустым user-паролем (Adobe DRM)
    снимается через authenticate(""). При настоящем user-пароле —
    EncryptedPDFError с понятным текстом.
    """
    doc = fitz.open(path)
    if doc.needs_pass:
        if doc.authenticate("") == 0:
            doc.close()
            raise EncryptedPDFError(
                "PDF защищён паролем на открытие. Сними защиту "
                "(в Acrobat: «Сохранить копию» без пароля) и загрузи заново."
            )
    return doc


# ============================================================
# ХЕЛПЕРЫ РАЗБОРА CONTENT STREAM
# ============================================================

_ARTIFACT_PAT = re.compile(rb'/Artifact\b[^\n]*?BDC.*?EMC', re.DOTALL)
_BT_ET_PAT = re.compile(rb'BT\b.*?\bET\b', re.DOTALL)
_TM_PAT = re.compile(
    rb'(-?\d*\.?\d+)\s+(-?\d*\.?\d+)\s+(-?\d*\.?\d+)\s+(-?\d*\.?\d+)\s+'
    rb'(-?\d*\.?\d+)\s+(-?\d*\.?\d+)\s+Tm'
)
_PDF_STR_PAT = re.compile(rb'\((?:[^()\\]|\\.)*\)', re.DOTALL)
_PLACEHOLDER_PAT = re.compile(
    r'\b[A-Za-z]+\d+_\d+_\d{13}\.(?:jpg|jpeg|png|tiff?|gif)\b',
    re.IGNORECASE
)
# Векторный watermark: прозрачность задаётся ExtGState (ca/CA < 1), сам знак —
# блок рисования q ... /GSxxx gs ... Q. Вырезаем такие блоки целиком.
_CA_RE = re.compile(r'/(?:ca|CA)\s+([0-9.]+)')
_QTOK = re.compile(rb'(?<![A-Za-z0-9_])([qQ])(?![A-Za-z0-9_])')
_GSTOK = re.compile(rb'/([A-Za-z0-9_.\-]+)\s+gs')


def _decode_pdf_string(body):
    out = bytearray()
    i, n = 0, len(body)
    esc = {0x6e: 0x0a, 0x72: 0x0d, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0c}
    while i < n:
        c = body[i]
        if c == 0x5C and i + 1 < n:
            nxt = body[i + 1]
            if nxt in esc:
                out.append(esc[nxt]); i += 2
            elif 0x30 <= nxt <= 0x37:
                j, d = i + 1, b''
                while j < n and j < i + 4 and 0x30 <= body[j] <= 0x37:
                    d += body[j:j + 1]; j += 1
                out.append(int(d, 8) & 0xFF); i = j
            else:
                out.append(nxt); i += 2
        else:
            out.append(c); i += 1
    return bytes(out)


def _block_text(block_bytes):
    parts = _PDF_STR_PAT.findall(block_bytes)
    return b''.join(_decode_pdf_string(p[1:-1]) for p in parts).decode('latin-1', errors='replace')


def _norm(s):
    return ' '.join(s.split()).lower()


def _is_rotated(block_bytes):
    for m in _TM_PAT.finditer(block_bytes):
        if abs(float(m.group(2))) > 0.01 or abs(float(m.group(3))) > 0.01:
            return True
    return False


def _page_content_xrefs(page):
    try:
        x = page.get_contents()
        return list(x) if x else []
    except Exception:
        return []


# ============================================================
# ДЕТЕКТОРЫ
# ============================================================

def _detect_image(doc, pages):
    xref_pages = {}
    for i in pages:
        for img in doc[i].get_images():
            xref_pages.setdefault(img[0], []).append(i)
    cands = []
    for xref, on in xref_pages.items():
        cov = len(on) / len(pages)
        if cov < 0.5:
            continue
        try:
            pix = fitz.Pixmap(doc, xref); w, h = pix.width, pix.height; pix = None
        except Exception:
            continue
        has_smask = 'SMask' in doc.xref_get_keys(xref)
        score = int(cov * 100) + (50 if w > 1000 and h > 1000 else 25 if w > 500 and h > 500 else 0) + (30 if has_smask else 0)
        cands.append({'xref': xref, 'coverage': cov, 'width': w, 'height': h,
                      'has_smask': has_smask, 'score': score, 'sample_page': on[0]})
    cands.sort(key=lambda x: x['score'], reverse=True)
    return cands


def _detect_form_xobject(doc):
    wm = []
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
        except Exception:
            continue
        if obj and '/Watermark' in obj and '/Subtype /Form' in obj:
            txt = ''
            try:
                s = doc.xref_stream(xref)
                if s:
                    txt = _norm(_block_text(s))
            except Exception:
                pass
            wm.append({'xref': xref, 'text': txt})
    return wm


def _detect_artifact(doc, pages):
    text_pages, text_rot, text_sample = Counter(), {}, {}
    threshold = max(2, len(pages) * 0.5)
    for i in pages:
        seen = set()
        for xref in _page_content_xrefs(doc[i]):
            raw = doc.xref_stream(xref)
            if not raw:
                continue
            for m in _ARTIFACT_PAT.finditer(raw):
                blk = m.group()
                txt = _norm(_block_text(blk))
                if len(txt) < 3 or txt in seen:
                    continue
                seen.add(txt)
                text_pages[txt] += 1
                if _is_rotated(blk):
                    text_rot[txt] = True
                text_sample.setdefault(txt, i)
    cands = [{'text': t, 'count': c, 'rotated': text_rot.get(t, False), 'sample_page': text_sample[t]}
             for t, c in text_pages.items() if c >= threshold]
    cands.sort(key=lambda x: (x['rotated'], x['count']), reverse=True)
    return cands


def _detect_annotations(doc, pages):
    out = []
    for i in pages:
        for a in doc[i].annots():
            atype = a.type[1] if a.type else ''
            if atype in ('Watermark', 'Stamp') or 'watermark' in str(a.info or {}).lower():
                out.append(i); break
    return out


def _detect_placeholders(doc, pages):
    names, isbns = [], set()
    for i in pages:
        for m in _PLACEHOLDER_PAT.finditer(doc[i].get_text()):
            names.append(m.group())
            im = re.search(r'_(\d{13})\.', m.group())
            if im:
                isbns.add(im.group(1))
    return names, isbns


def _detect_text(doc, pages, wt):
    hp, total = [], 0
    for i in pages:
        h = doc[i].search_for(wt)
        if h:
            hp.append(i); total += len(h)
    return hp, total, (len(hp) / len(pages) if pages else 0)


def _transparent_gs_names(doc, page_xref, threshold=0.5):
    """Имена ExtGState на странице с ca<threshold или CA<threshold (полупрозрачные)."""
    names = set()
    try:
        egs = doc.xref_get_key(page_xref, "Resources/ExtGState")
    except Exception:
        return names
    if not egs or egs[0] == 'null':
        return names
    body = egs[1]
    # /name -> либо inline <<...>>, либо ссылка "N 0 R"
    for m in re.finditer(r'/([A-Za-z0-9_.\-]+)\s*(<<.*?>>|\d+\s+0\s+R)', body, re.DOTALL):
        nm, val = m.group(1), m.group(2)
        if val.startswith('<<'):
            src = val
        else:
            try:
                src = doc.xref_object(int(val.split()[0]))
            except Exception:
                continue
        vals = [float(x) for x in _CA_RE.findall(src)]
        if vals and min(vals) < threshold:
            names.add(nm)
    return names


def _detect_vector(doc, pages, threshold=0.5):
    """Полупрозрачная векторная графика, повторяющаяся на страницах = кандидат на watermark."""
    semi_pages, total = 0, 0
    for i in pages:
        semi = [d for d in doc[i].get_drawings()
                if (d.get('fill_opacity') or 1) < threshold
                or (d.get('stroke_opacity') or 1) < threshold]
        if semi:
            semi_pages += 1
            total += len(semi)
    cov = semi_pages / len(pages) if pages else 0
    return {'semi_pages': semi_pages, 'total': total, 'coverage': cov}


def _repeated_image_xrefs(doc, threshold=0.5):
    """xref-картинки на > threshold доле страниц — это watermark/фон, а не иллюстрации."""
    n = len(doc)
    if not n:
        return set()
    cnt = {}
    for page in doc:
        for x in {img[0] for img in page.get_images(full=True)}:
            cnt[x] = cnt.get(x, 0) + 1
    return {x for x, c in cnt.items() if c / n > threshold}


def _real_image_rects(doc, page, repeated, min_area_pct=2.0, max_area_pct=90.0):
    """Прямоугольники «настоящих» иллюстраций на странице (по размеру, без повторяющихся)."""
    pa = abs(page.rect) or 1
    out = []
    for img in page.get_images(full=True):
        if img[0] in repeated:
            continue
        for r in page.get_image_rects(img[0]):
            if min_area_pct <= abs(r) / pa * 100 <= max_area_pct:
                out.append(r)
    return out


def _find_caption(blocks, img, max_gap=35, center_tol=30):
    """Ближайший центрированный узкий текстовый блок впритык под картинкой = подпись."""
    img_cx = (img.x0 + img.x1) / 2
    img_w = img.x1 - img.x0
    best, best_gap = None, 1e9
    for b in blocks:
        if b.get('type') != 0 or not b.get('lines'):
            continue
        bb = fitz.Rect(b['bbox'])
        gap = bb.y0 - img.y1
        if gap < -2 or gap > max_gap:           # не ниже картинки или слишком далеко
            continue
        if abs((bb.x0 + bb.x1) / 2 - img_cx) > center_tol:  # не центрирован под картинкой
            continue
        if (bb.x1 - bb.x0) > img_w * 1.15:      # шире картинки => основной текст
            continue
        txt = ' '.join(s['text'] for l in b['lines'] for s in l['spans']).strip()
        if not txt or len(txt) > 250:
            continue
        if gap < best_gap:
            best, best_gap = (bb, txt), gap
    return best


def _detect_illustrations(doc, pages):
    """Настоящие растровые иллюстрации (не watermark): счёт по выборке страниц."""
    repeated = _repeated_image_xrefs(doc)
    pages_with, total = 0, 0
    for i in pages:
        rects = _real_image_rects(doc, doc[i], repeated)
        if rects:
            pages_with += 1
            total += len(rects)
    return {'pages_with': pages_with, 'total': total}


def _running_groups(doc, page_indices, margin_frac=0.18, max_chars=100):
    """
    Сгруппировать строки-колонтитулы по (зона, позиция, нормализованный текст).

    Колонтитул определяется ПОВТОРЯЕМОСТЬЮ ТЕКСТА в одной позиции поля: тот же
    нормализованный текст (цифры→#, поэтому номера страниц и «indd 2/3» — один
    паттерн) в той же позиции (round cy/6) на >= min_repeats страницах (порог
    проверяется в вызывающем коде).

    Группировка ИМЕННО ПО ТЕКСТУ (а не по голой позиции) — главная защита контента:
    основной текст уникален на каждой странице, поэтому НЕ группируется и НЕ
    удаляется, даже если первые/последние строки тела попадают в поле (так бывает
    при плотной вёрстке — в одной книге текст начинается на 10% высоты, там же, где
    в другой сидит колонтитул). Подход по голой позиции этого не давал и резал тело.
    Плата: бегущий заголовок совсем короткой главы может не набрать порог — но это
    несравнимо безопаснее потери текста. Фильтры зоны (margin_frac) и длины
    (max_chars) дополнительно сужают набор кандидатов.
    """
    groups = {}
    for i in page_indices:
        page = doc[i]
        H = page.rect.height
        mtop, mbot = H * margin_frac, H * (1 - margin_frac)
        for block in page.get_text("dict")['blocks']:
            if block.get('type') != 0:
                continue
            for line in block.get('lines', []):
                lb = fitz.Rect(line['bbox'])
                cy = (lb.y0 + lb.y1) / 2
                if not (cy < mtop or cy > mbot):
                    continue
                txt = ''.join(s['text'] for s in line['spans']).strip()
                if not txt or len(txt) > max_chars:
                    continue
                zone = 'T' if cy < mtop else 'B'
                key = (zone, round(cy / 6), re.sub(r'\d+', '#', txt))
                groups.setdefault(key, []).append((i, lb, txt))
    return groups


def _detect_running_content(doc, pages, min_repeats=2):
    """Повторяющиеся колонтитулы/техмусор в полях страниц."""
    groups = _running_groups(doc, pages)
    examples = []
    patterns = 0
    for occ in groups.values():
        if len({i for i, _, _ in occ}) >= min_repeats:
            patterns += 1
            if len(examples) < 4:
                for _, _, txt in occ:
                    if txt:
                        examples.append(txt[:45])
                        break
    return {'patterns': patterns, 'examples': examples}


# ============================================================
# ГЛАВНАЯ ДИАГНОСТИКА
# ============================================================

def diagnose(pdf_path, watermark_text=None, sample_pages=20):
    doc = open_pdf(pdf_path)
    total_pages = len(doc)
    pages = list(range(min(sample_pages, total_pages)))
    report = {
        'total_pages': total_pages,
        'metadata': {k: v for k, v in (doc.metadata or {}).items() if v},
        'pages_sampled': len(pages),
        'strategies': [],
        'recommended_index': None,
    }
    S = report['strategies']

    # form_xobject — Adobe сам пометил, максимальная уверенность
    for w in _detect_form_xobject(doc):
        snip = w['text'][:60] if w['text'] else '(текст не извлечён)'
        S.append({'type': 'form_xobject', 'confidence': 'high',
                  'title': 'Водяной знак во Form XObject (InDesign)',
                  'description': (f'InDesign пометил объект как watermark. Содержимое: «{snip}». '
                                  f'Обнулим все такие объекты разом, основной текст не трогаем.'),
                  'params': {}, 'sample_page': 0})
        break

    # artifact — текстовый в Artifact-обёртке
    for c in _detect_artifact(doc, pages):
        conf = 'high' if c['rotated'] else 'low'
        note = 'по диагонали' if c['rotated'] else 'горизонтально (возможно колонтитул!)'
        S.append({'type': 'artifact', 'confidence': conf,
                  'title': f'Текстовый водяной знак: «{c["text"][:50]}»',
                  'description': (f'Повторяется на {c["count"]} из {len(pages)} проверенных страниц, {note}. '
                                  f'Вырежем из потока на всех страницах.'),
                  'params': {'text': c['text']}, 'sample_page': c['sample_page']})

    # image — растровый
    for c in _detect_image(doc, pages)[:5]:
        conf = 'high' if c['score'] > 150 else ('medium' if c['score'] > 100 else 'low')
        smask = ', с маской прозрачности' if c['has_smask'] else ''
        S.append({'type': 'image', 'confidence': conf,
                  'title': f'Растровый водяной знак (картинка #{c["xref"]})',
                  'description': (f'Картинка {c["width"]}×{c["height"]} на {int(c["coverage"]*100)}% страниц{smask}. '
                                  f'Заменим на прозрачный 1×1 пиксель.'),
                  'params': {'xref': c['xref']}, 'sample_page': c['sample_page']})

    # annotation
    ap = _detect_annotations(doc, pages)
    if ap:
        cov = len(set(ap)) / len(pages)
        S.append({'type': 'annotation', 'confidence': 'high' if cov > 0.5 else 'medium',
                  'title': 'Водяной знак-аннотация',
                  'description': f'Аннотации Watermark/Stamp на {len(set(ap))} страницах.',
                  'params': {}, 'sample_page': ap[0]})

    # text — по введённому тексту, если не покрыт artifact'ом
    if watermark_text:
        already = any(_norm(watermark_text) in _norm(s['params'].get('text', ''))
                      for s in S if s['type'] == 'artifact')
        if not already:
            hp, total, cov = _detect_text(doc, pages, watermark_text)
            if cov >= 0.3:
                S.append({'type': 'text', 'confidence': 'high' if cov > 0.8 else 'medium',
                          'title': f'Текстовый водяной знак: «{watermark_text}»',
                          'description': f'Найден на {len(hp)} из {len(pages)} страниц ({int(cov*100)}%). Умное вырезание из потока.',
                          'params': {'text': watermark_text}, 'sample_page': hp[0] if hp else 0})

    # placeholder — замена, не снятие
    names, isbns = _detect_placeholders(doc, pages)
    if names:
        isbn_note = f' ISBN {", ".join(sorted(isbns))}' if isbns else ''
        S.append({'type': 'placeholder', 'confidence': 'high',
                  'title': 'Placeholder-ы иллюстраций',
                  'description': (f'Найдены метки-заглушки картинок{isbn_note} (в выборке: {len(names)}). '
                                  f'Заменю каждую на «<иллюстрация на листе XX>» с номером листа PDF. '
                                  f'Это не водяной знак, а подготовка к переводу.'),
                  'params': {'isbns': sorted(isbns)}, 'sample_page': 0})

    # illustration — пометка РЕАЛЬНЫХ растровых иллюстраций (подготовка к переводу)
    ill = _detect_illustrations(doc, pages)
    if ill['total'] > 0:
        S.append({'type': 'illustration', 'confidence': 'high',
                  'title': 'Реальные иллюстрации (растровые)',
                  'description': (f'Настоящих иллюстраций в выборке: {ill["total"]} '
                                  f'на {ill["pages_with"]} страницах. Под каждой поставлю метку '
                                  f'«<иллюстрация на листе N>» (N — порядковый лист PDF), а под подписью к ней — '
                                  f'«<подпись под иллюстрацией>». Картинки и подписи остаются на месте. '
                                  f'Это подготовка к переводу, не снятие. Подпись ищется эвристикой — сверь результат.'),
                  'params': {}, 'sample_page': 0})

    # headers — повторяющиеся колонтитулы и техмусор (чистота текста при конвертации в Word)
    rc = _detect_running_content(doc, pages)
    if rc['patterns'] > 0:
        ex = '; '.join(rc['examples'][:3])
        S.append({'type': 'headers', 'confidence': 'high',
                  'title': 'Колонтитулы и повторяющийся техтекст',
                  'description': (f'Повторяющихся элементов в полях страниц: {rc["patterns"]} '
                                  f'(напр.: {ex}). Это колонтитулы, номера страниц, имя .indd-файла, даты — '
                                  f'всё, что лезет в копируемый текст при конвертации в Word. Удалю со всех '
                                  f'страниц; основной текст и сноски не трону (они не повторяются в полях).'),
                  'params': {}, 'sample_page': 0})

    # vector — полупрозрачная векторная подложка (теперь умеем снимать)
    v = _detect_vector(doc, pages)
    if v['coverage'] >= 0.5 and v['total'] > 0:
        S.append({'type': 'vector',
                  'confidence': 'high' if v['coverage'] > 0.8 else 'medium',
                  'title': 'Векторный водяной знак (полупрозрачный)',
                  'description': (f'Полупрозрачная векторная графика на {int(v["coverage"]*100)}% '
                                  f'проверенных страниц — типичная подложка-знак. Вырежу полупрозрачные '
                                  f'блоки рисования; непрозрачный контент (текст, таблицы, схемы) не трону. '
                                  f'Векторный случай хитрый — обязательно сверь превью «до/после».'),
                  'params': {'threshold': 0.5}, 'sample_page': 0})
    else:
        # много векторов, но не полупрозрачных — надёжно снять не берёмся
        vec = sum(len(doc[i].get_drawings()) for i in pages[:3])
        if vec > 200 and not any(s['type'] in ('artifact', 'form_xobject') for s in S):
            S.append({'type': 'vector', 'confidence': 'low',
                      'title': 'Возможен векторный водяной знак',
                      'description': f'{vec} векторных объектов на первых страницах, но не полупрозрачные — '
                                     f'автоматически снять не берусь, глянь вручную.',
                      'params': {}, 'sample_page': 0, 'manual_only': True})

    # рекомендация
    for i, s in enumerate(S):
        if s['confidence'] == 'high' and not s.get('manual_only'):
            report['recommended_index'] = i; break
    if report['recommended_index'] is None:
        for i, s in enumerate(S):
            if not s.get('manual_only'):
                report['recommended_index'] = i; break

    doc.close()
    return report


# ============================================================
# ЧИСТИЛЬЩИКИ
# ============================================================

def clean_image_watermark(input_path, output_path, xref):
    doc = open_pdf(input_path)
    smask = None
    if 'SMask' in doc.xref_get_keys(xref):
        v = doc.xref_get_key(xref, 'SMask')
        if v[0] == 'xref':
            smask = int(v[1].split()[0])
    white, black = zlib.compress(bytes([0xFF])), zlib.compress(bytes([0x00]))
    for x, px in [(xref, white), (smask, black)]:
        if x is None:
            continue
        doc.xref_set_key(x, "Width", "1")
        doc.xref_set_key(x, "Height", "1")
        doc.xref_set_key(x, "DecodeParms", "null")
        doc.update_stream(x, px, new=False, compress=False)
        doc.xref_set_key(x, "Filter", "/FlateDecode")
    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()
    return {'removed': 1}


def clean_form_xobject_watermark(input_path, output_path):
    doc = open_pdf(input_path)
    targets = [w['xref'] for w in _detect_form_xobject(doc)]
    empty = zlib.compress(b'q Q')
    for xref in targets:
        doc.update_stream(xref, empty, new=False, compress=False)
        doc.xref_set_key(xref, "Filter", "/FlateDecode")
    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()
    return {'removed': len(targets)}


def clean_artifact_watermark(input_path, output_path, marker_text):
    doc = open_pdf(input_path)
    target = _norm(marker_text)
    removed = 0
    for page in doc:
        for xref in _page_content_xrefs(page):
            raw = doc.xref_stream(xref)
            if not raw:
                continue
            spans = [(m.start(), m.end()) for m in _ARTIFACT_PAT.finditer(raw)
                     if _norm(_block_text(m.group())) == target]
            if not spans:
                continue
            new_raw = raw
            for s, e in reversed(spans):
                new_raw = new_raw[:s] + new_raw[e:]
            doc.update_stream(xref, new_raw)
            removed += len(spans)
    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()
    return {'removed': removed}


def clean_annotation_watermark(input_path, output_path):
    doc = open_pdf(input_path)
    removed = 0
    for page in doc:
        dels = [a for a in page.annots()
                if (a.type[1] if a.type else '') in ('Watermark', 'Stamp')
                or 'watermark' in str(a.info or {}).lower()]
        for a in dels:
            page.delete_annot(a); removed += 1
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return {'removed': removed}


def clean_text_watermark(input_path, output_path, text):
    doc = open_pdf(input_path)
    target = _norm(text)
    removed = 0

    def matches(bt_raw):
        bt = _norm(bt_raw)
        if not bt:
            return False
        return target in bt or bt in target or any(w in bt for w in target.split() if len(w) > 3)

    for page in doc:
        for xref in _page_content_xrefs(page):
            raw = doc.xref_stream(xref)
            if not raw:
                continue
            spans = [(m.start(), m.end()) for m in _ARTIFACT_PAT.finditer(raw)
                     if matches(_block_text(m.group()))]
            covered = lambda pos: any(s <= pos < e for s, e in spans)
            for m in _BT_ET_PAT.finditer(raw):
                if covered(m.start()):
                    continue
                blk = m.group()
                if _is_rotated(blk) and matches(_block_text(blk)):
                    spans.append((m.start(), m.end()))
            if not spans:
                continue
            spans.sort()
            new_raw = raw
            for s, e in reversed(spans):
                new_raw = new_raw[:s] + new_raw[e:]
            doc.update_stream(xref, new_raw)
            removed += len(spans)

    if removed == 0:  # откат на redact
        for page in doc:
            hits = page.search_for(text)
            for r in hits:
                page.add_redact_annot(r, fill=None)
            if hits:
                page.apply_redactions(images=0, graphics=0)
                removed += len(hits)

    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()
    return {'removed': removed}


_CYR_FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf'
_HL_YELLOW = (1, 1, 0)  # жёлтый highlight для машинных меток


def _insert_tag(page, x, baseline, text, fontsize):
    """
    Вставить машинную метку: чёрный текст на жёлтом highlight-фоне (как маркером).
    x, baseline — левый край и базовая линия (низ букв). Возвращает ширину текста.
    Жёлтая подложка сразу показывает, что метка добавлена машиной, и не теряется
    на фоне при конвертации в Word.
    """
    try:
        tw = fitz.Font(fontfile=_CYR_FONT).text_length(text, fontsize=fontsize)
    except Exception:
        tw = fontsize * 0.5 * len(text)  # грубая оценка, если метрики шрифта недоступны
    pad = fontsize * 0.15
    page.draw_rect(fitz.Rect(x - pad, baseline - fontsize, x + tw + pad, baseline + fontsize * 0.3),
                   color=None, fill=_HL_YELLOW)
    page.insert_text(fitz.Point(x, baseline), text, fontsize=fontsize,
                     color=(0, 0, 0), fontname="CyrSerif", fontfile=_CYR_FONT)
    return tw


def replace_placeholders(input_path, output_path, isbns=None, fmt='<иллюстрация на листе {page}>'):
    doc = open_pdf(input_path)
    isbns = set(isbns or [])
    report, total = [], 0
    for page in doc:
        page_no = page.number + 1
        found = []
        for block in page.get_text("dict")['blocks']:
            if block.get('type') != 0:
                continue
            for line in block.get('lines', []):
                for span in line.get('spans', []):
                    m = _PLACEHOLDER_PAT.search(span.get('text', ''))
                    if not m:
                        continue
                    if isbns:
                        im = re.search(r'_(\d{13})\.', m.group())
                        if not im or im.group(1) not in isbns:
                            continue
                    found.append({'filename': m.group(), 'bbox': fitz.Rect(span['bbox']), 'size': span['size']})
        if not found:
            continue
        for f in found:
            report.append({'page': page_no, 'filename': f['filename']})
        for f in found:
            page.add_redact_annot(f['bbox'], fill=None)
        page.apply_redactions(images=0, graphics=0)
        for f in found:
            b = f['bbox']
            _insert_tag(page, b.x0, b.y0 + f['size'], fmt.format(page=page_no), f['size'])
            total += 1
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return {'removed': total, 'report': report}


def _strip_transparent_blocks(content, gs_names):
    """Вырезать top-level q...Q блоки, использующие прозрачный gs из gs_names."""
    name_bytes = {n.encode('latin-1') for n in gs_names}
    cuts, stack = [], []
    for m in _QTOK.finditer(content):
        if m.group(1) == b'q':
            stack.append(m.start())
        else:  # Q
            if not stack:
                continue
            start = stack.pop()
            if not stack:  # закрыт top-level блок [start, m.end())
                block = content[start:m.end()]
                used = {g.group(1) for g in _GSTOK.finditer(block)}
                if used & name_bytes:
                    cuts.append((start, m.end()))
    if not cuts:
        return content, 0
    out = content
    for s, e in reversed(cuts):
        out = out[:s] + out[e:]
    return out, len(cuts)


def clean_vector_watermark(input_path, output_path, threshold=0.5):
    """
    Снятие полупрозрачного векторного watermark: вырезаем блоки рисования q...Q,
    которые используют прозрачный ExtGState (ca/CA < threshold). Непрозрачный
    контент (текст, таблицы, схемы) не трогаем — даже если знак нарисован поверх.

    Эвристика: watermark обычно полупрозрачный, контент — нет. На файлах, где
    легитимная графика тоже полупрозрачна, возможны ложные срабатывания, поэтому
    результат ОБЯЗАТЕЛЬНО смотреть в превью «до/после».
    """
    doc = open_pdf(input_path)
    removed = 0
    for page in doc:
        names = _transparent_gs_names(doc, page.xref, threshold)
        if not names:
            continue
        for xref in _page_content_xrefs(page):
            raw = doc.xref_stream(xref)
            if not raw:
                continue
            new_raw, n = _strip_transparent_blocks(raw, names)
            if n:
                doc.update_stream(xref, new_raw)
                removed += n
    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()
    return {'removed': removed}


def mark_illustrations(input_path, output_path,
                       fmt='<иллюстрация на листе {page}>',
                       caption_label='<подпись под иллюстрацией>'):
    """
    Пометка РЕАЛЬНЫХ растровых иллюстраций — подготовка к переводу, НЕ снятие.

    Под каждой иллюстрацией ставит «<иллюстрация на листе N>» (N — порядковый лист
    PDF), а под её подписью — «<подпись под иллюстрацией>». Сами картинки и подписи
    остаются на месте. Повторяющиеся картинки (watermark/фон) пропускаются.

    Подпись определяется эвристикой: ближайший узкий центрированный текстовый блок
    впритык под картинкой. На хитрой вёрстке может промахнуться — сверять результат.
    Метки извлекаются в правильном порядке при чтении текста с сортировкой по позиции.
    """
    doc = open_pdf(input_path)
    repeated = _repeated_image_xrefs(doc)
    illos, caps, report = 0, 0, []
    for page in doc:
        rects = _real_image_rects(doc, page, repeated)
        if not rects:
            continue
        blocks = page.get_text("dict")['blocks']
        page_no = page.number + 1
        for r in rects:
            cap = _find_caption(blocks, r)
            # метка иллюстрации — под картинкой (над подписью, если она есть)
            illo_y = (cap[0].y0 - 1) if cap else (r.y1 + 11)
            _insert_tag(page, r.x0, illo_y, fmt.format(page=page_no), 8)
            illos += 1
            report.append({'page': page_no, 'caption': cap[1] if cap else ''})
            # метка подписи — под подписью
            if cap:
                cb = cap[0]
                _insert_tag(page, cb.x0, cb.y1 + 9, caption_label, 8)
                caps += 1
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return {'removed': illos, 'captions': caps, 'report': report}


def remove_running_content(input_path, output_path, min_repeats=2):
    """
    Удаление повторяющихся колонтитулов и техмусора: номера страниц, имя автора,
    название главы, имя .indd-файла, даты экспорта — всё, что лезет в копируемый
    текст при конвертации в Word.

    Критерий: строка в верхнем/нижнем поле страницы (margin_frac высоты),
    повторяющаяся в той же позиции на >= min_repeats страницах (цифры нормализуются,
    поэтому «...indd 2», «...indd 3» и номера страниц ловятся как один паттерн).
    Основной текст и сноски в полях не повторяются — не трогаются. Удаление через
    redaction (только текст; картинки и графику не трогаем).
    """
    doc = open_pdf(input_path)
    groups = _running_groups(doc, range(len(doc)))
    by_page = {}
    for occ in groups.values():
        if len({pno for pno, _, _ in occ}) >= min_repeats:
            for pno, bb, _ in occ:
                by_page.setdefault(pno, []).append(bb)
    removed = 0
    for pno, boxes in by_page.items():
        page = doc[pno]
        for b in boxes:
            page.add_redact_annot(b)
            removed += 1
        page.apply_redactions(images=0, graphics=0)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return {'removed': removed}


# ============================================================
# ПРЕВЬЮ
# ============================================================

def render_page_preview(pdf_path, page_num=0, dpi=110):
    doc = open_pdf(pdf_path)
    page_num = max(0, min(page_num, len(doc) - 1))
    png = doc[page_num].get_pixmap(dpi=dpi).tobytes("png")
    doc.close()
    return png


def render_image_thumbnail(pdf_path, xref):
    """
    Возвращает (bytes, ext) превью картинки-кандидата.

    Используем doc.extract_image (достаёт сжатый поток картинки напрямую), а НЕ
    fitz.Pixmap(doc, xref): последний рендерит через пайплайн с композитингом
    SMask и разбором ICCBased-цветового профиля и роняет MuPDF в SIGSEGV на
    битых/нестандартных картинках. Реальный кейс: картинка 2550×3300 со SMask и
    ICCBased в PDF с повреждённым xref — Pixmap сегфолтил и убивал весь воркер
    (а с ним и in-memory jobs → «Задача не найдена»). extract_image не роняет.
    """
    doc = open_pdf(pdf_path)
    try:
        img = doc.extract_image(xref)
        return img["image"], (img.get("ext") or "png")
    finally:
        doc.close()
