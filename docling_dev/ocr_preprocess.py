"""Предобработка изображения перед Tesseract — поднимает качество распознавания
плохих сканов (наклон, серый фон, шум, бледность).

КАК ВСТРАИВАЕТСЯ: Tesseract вызывается ВНУТРИ Docling (TesseractOcrCliModel):
страница рендерится во временный PNG на диске, затем запускается tesseract CLI.
Перехватываем `TesseractOcrCliModel._run_tesseract` (monkeypatch): читаем
temp-PNG через cv2, предобрабатываем и перезаписываем файл перед запуском CLI.
К этому моменту OSD-поворот (90°-шаги) уже учтён Docling'ом; наш deskew
дополняет его выравниванием малых углов.

По умолчанию предобработка ЛЁГКАЯ: deskew + CLAHE-контраст; denoise и
бинаризация — опции, включать только если проверено, что помогает.

Полностью офлайн (только cv2/numpy, уже в зависимостях). Если cv2 недоступен —
no-op (OCR работает как раньше).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Для статического анализатора — реальные модули (без union с None),
    # чтобы cv2.*/np.* разрешались. В рантайме работает try/except ниже.
    import cv2
    import numpy as np
else:
    try:
        import cv2
        import numpy as np
    except Exception:                   # cv2/numpy всегда есть, но на всякий
        cv2 = None
        np = None


def _estimate_skew_deg(gray) -> float:
    """Оценивает угол наклона текста в градусах (через minAreaRect тёмных точек).
    Возвращает 0.0, если оценить нельзя."""
    try:
        thr = cv2.threshold(gray, 0, 255,
                            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        coords = np.column_stack(np.where(thr > 0))
        if coords.shape[0] < 200:
            return 0.0
        angle = cv2.minAreaRect(coords)[-1]
        # minAreaRect возвращает угол в (-90, 0]; нормируем к (-45, 45]
        if angle < -45:
            angle = 90.0 + angle
        elif angle > 45:
            angle = angle - 90.0
        return float(angle)
    except Exception:
        return 0.0


def _rotate(img, angle: float):
    """Поворот изображения на angle градусов с белым фоном."""
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    border = 255 if img.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=border)


def _frame_side_coverage(labels, comp_id, x, y, w, h,
                         band: int = 5) -> tuple[float, float]:
    """Покрытие сторон bbox штрихом компоненты: (гориз., верт.).

    гориз. = max(доля колонок со штрихом в верхней полосе, в нижней);
    верт.  = max(доля строк со штрихом в левой полосе, в правой).
    У прямоугольной РАМКИ обе ≈ 1.0 (сплошные прямые борта); у подписи-росчерка
    или кляксы штрих гуляет по центру и покрытие сторон низкое. Приросший к
    рамке рукописный текст на метрику не влияет."""
    sub = labels[y:y + h, x:x + w] == comp_id
    b = min(band, h, w)
    top    = float(sub[:b, :].any(axis=0).mean())
    bottom = float(sub[-b:, :].any(axis=0).mean())
    left   = float(sub[:, :b].any(axis=1).mean())
    right  = float(sub[:, -b:].any(axis=1).mean())
    return max(top, bottom), max(left, right)


def _find_stamp_frames(labels, stats, n, mh, W, H,
                       max_w_frac: float = 0.5,
                       max_h_frac: float = 0.35) -> list[tuple[int, int, int, int, int]]:
    """Ищет рамки штампов: полая (плотность <= 25%) тонкоштриховая компонента
    заметного размера, чей штрих лежит у периметра bbox. Размер ограничен
    (по умолч. <=50% ширины, <=35% высоты листа) — таблицы на полстраницы сюда
    не попадают; для кропа картинки лимиты передаются >1 (рамка на весь кроп).
    Возвращает [(comp_id, x, y, w, h)]."""
    frames = []
    for i in range(1, n):
        x = stats[i, cv2.CC_STAT_LEFT];  y = stats[i, cv2.CC_STAT_TOP]
        w = stats[i, cv2.CC_STAT_WIDTH]; h = stats[i, cv2.CC_STAT_HEIGHT]
        area = float(stats[i, cv2.CC_STAT_AREA])
        if not (w >= 4 * mh and h >= 3 * mh
                and w <= max_w_frac * W and h <= max_h_frac * H):
            continue
        if area / max(w * h, 1.0) > 0.25:
            continue
        # Средняя толщина штриха рамки: площадь / периметр bbox
        if area / max(2 * (w + h), 1.0) > 0.35 * mh:
            continue
        cov_h, cov_v = _frame_side_coverage(labels, i, x, y, w, h)
        if cov_h < 0.8 or cov_v < 0.8:
            continue
        frames.append((i, x, y, w, h))
    return frames


def remove_stains(gray):
    """Удаляет со скана всё, что не является текстом: грязь копира, полосы от
    сшивки, чёрные края, россыпь точек.

    Принцип «строкоцентричный»: буквы живут в ТЕКСТОВЫХ СТРОКАХ. Склеиваем
    бинаризованное изображение горизонтальной дилатацией — буквы и слова
    сливаются в вытянутые строки-блобы (ширина >> высота). Всё, что в строки
    не попало и не является линией разметки, — мусор. Так жирные слипшиеся
    слова («МКК», «ИНН») не страдают: они внутри своих строк, а обломки
    грязевых полос строк не образуют (короткие и не вытянутые).

    Дополнительные правила:
      линия  — тонкая длинная компонента (рамка таблицы, подчёркивание) → оставить;
      speck  — компонента в разы мельче точки/запятой → мусор;
      кромка — плотная тёмная полоса, касающаяся края листа (тень сканера) → мусор.

    Возвращает очищенную копию grayscale (мусор → белый фон).
    """
    H, W = gray.shape[:2]
    thr = cv2.threshold(gray, 0, 255,
                        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    n, labels, stats, _cents = cv2.connectedComponentsWithStats(thr, 8)
    if n <= 1:
        return gray

    ws = stats[1:, cv2.CC_STAT_WIDTH].astype(float)
    hs = stats[1:, cv2.CC_STAT_HEIGHT].astype(float)

    # Медианная высота текстоподобных компонент ≈ высота строчной буквы.
    # 216 dpi (рендер Docling): 11pt ≈ 30px. Медиана устойчива: букв на
    # странице сотни, обломков грязи — десятки.
    text_like = (hs >= 8) & (hs <= 120) & (ws >= 3)
    mh = float(np.median(hs[text_like])) if text_like.any() else 30.0
    mh = min(max(mh, 10.0), 80.0)

    thin      = max(3.0, 0.08 * mh)          # толщина линии рамки/подчёркивания
    speck_max = max(4.0, (0.09 * mh) ** 2)   # заведомо мельче точки/запятой

    # Сетка таблицы: все линии соединены в ОДНУ огромную тонкоштриховую
    # компоненту (bbox не «тонкий», правило линий её не ловит). Решётку
    # защищаем и исключаем из построения строк — иначе она склеивает весь
    # текст таблицы в мегаблоб, строки внутри ячеек не образуются и текст
    # ячеек уничтожается правилом «вне строк».
    def _is_lattice(w: float, h: float, density: float) -> bool:
        return min(w, h) >= 8 * mh and density <= 0.12

    lattice_ids: set[int] = set()
    lattice_zones: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        w, h = ws[i - 1], hs[i - 1]
        d = float(stats[i, cv2.CC_STAT_AREA]) / max(w * h, 1.0)
        if _is_lattice(w, h, d):
            lattice_ids.add(i)
            lattice_zones.append((stats[i, cv2.CC_STAT_LEFT],
                                  stats[i, cv2.CC_STAT_TOP], int(w), int(h)))

    def _in_lattice_zone(x: int, y: int, w: int, h: int) -> bool:
        """Компонента внутри bbox сетки таблицы: содержимое ячеек не трогаем —
        JUSTIFY внутри узких ячеек растягивает пробелы так, что короткие
        токены («от», «7») выпадают из строк и спасательного зазора."""
        for zx, zy, zw, zh in lattice_zones:
            if x >= zx - 2 and y >= zy - 2 and x + w <= zx + zw + 2 \
                    and y + h <= zy + zh + 2:
                return True
        return False

    # ── Рамки: штампы и эмблемы ──────────────────────────────────────────────
    # Канцелярские печати («Вх. №…» с рукописными датами) портят и текстовый
    # слой, и вёрстку — удаляем рамку и всё внутри (>=60% площади в зоне
    # рамка±mh; текст документа, пересекающий рамку снаружи, остаётся).
    # Но рамка бывает и у ГЕРБА/эмблемы письма — её содержимое наоборот
    # ЗАЩИЩАЕМ от остальных правил (иначе орёл сотрётся как «не-текст» и
    # Tesseract прочитает его бледный след как мусорные буквы).
    # Штамп: ШИРОКИЙ (w>=1.4h) и без крупной графики внутри.
    stamp_ids: set[int] = set()
    protect_zones: list[tuple[int, int, int, int]] = []
    frames = _find_stamp_frames(labels, stats, n, mh, W, H)
    for fid, fx, fy, fw, fh in frames:
        _has_graphic = False
        for i in range(1, n):
            if i == fid:
                continue
            x = stats[i, cv2.CC_STAT_LEFT];  y = stats[i, cv2.CC_STAT_TOP]
            w = stats[i, cv2.CC_STAT_WIDTH]; h = stats[i, cv2.CC_STAT_HEIGHT]
            if (x >= fx and y >= fy and x + w <= fx + fw and y + h <= fy + fh
                    and float(stats[i, cv2.CC_STAT_AREA]) >= 0.12 * fw * fh):
                _has_graphic = True
                break
        if fw < 1.4 * fh or _has_graphic:
            protect_zones.append((fx, fy, fw, fh))
            log.debug("ocr_preprocess: рамка %dx%d — эмблема/герб, содержимое защищено",
                      fw, fh)
            continue
        stamp_ids.add(fid)
        # Зону штампа расширяем на высоту строки: подписи штампа выступают
        # чуть за рамку («КАМЕНСКИЙ…» под нижним бортом).
        m = int(mh)
        zx, zy = fx - m, fy - m
        zw, zh = fw + 2 * m, fh + 2 * m
        for i in range(1, n):
            if i == fid:
                continue
            x = stats[i, cv2.CC_STAT_LEFT];  y = stats[i, cv2.CC_STAT_TOP]
            w = stats[i, cv2.CC_STAT_WIDTH]; h = stats[i, cv2.CC_STAT_HEIGHT]
            ov_w = min(x + w, zx + zw) - max(x, zx)
            ov_h = min(y + h, zy + zh) - max(y, zy)
            if ov_w > 0 and ov_h > 0 and ov_w * ov_h >= 0.6 * w * h:
                stamp_ids.add(i)
    if frames:
        log.debug("ocr_preprocess: рамок: %d (штампы: %d id, эмблемы: %d)",
                  len(frames), len(stamp_ids), len(protect_zones))

    def _in_protect_zone(x: int, y: int, w: int, h: int) -> bool:
        for zx, zy, zw, zh in protect_zones:
            if x >= zx - 2 and y >= zy - 2 and x + w <= zx + zw + 2 \
                    and y + h <= zy + zh + 2:
                return True
        return False

    # Из входа построения строк исключаем всё, что заведомо не буквы:
    #   линии разметки — иначе строка, пересекающая рамку штампа, слипается
    #     с ней в высокий блоб и перестаёт быть «строкой»;
    #   специ и кромки — точки-«дорожки» и тени сканера сцепляют соседние
    #     строки диагонально в блоб-«змею» через всю страницу, которая
    #     поглощает случайные буквы и лишает их своей строки.
    drop_ids = list(stamp_ids | lattice_ids)
    for i in range(1, n):
        if i in stamp_ids or i in lattice_ids:
            continue
        w, h = ws[i - 1], hs[i - 1]
        area = float(stats[i, cv2.CC_STAT_AREA])
        x = stats[i, cv2.CC_STAT_LEFT]; y = stats[i, cv2.CC_STAT_TOP]
        if min(w, h) <= thin and max(w, h) >= 3 * mh:
            drop_ids.append(i)                                   # линия
        elif area <= speck_max:
            drop_ids.append(i)                                   # спек
        elif ((x <= 2 or y <= 2 or x + w >= W - 2 or y + h >= H - 2)
              and max(w, h) >= 4 * mh and area / max(w * h, 1.0) >= 0.5):
            drop_ids.append(i)                                   # кромка
    thr_text = thr.copy()
    if drop_ids:
        thr_text[np.isin(labels, np.asarray(drop_ids))] = 0

    # ── Маска текстовых строк ────────────────────────────────────────────────
    # Горизонтальная дилатация склеивает буквы (зазор 2-8px) и слова в строки.
    # 1.4mh: в выключенных по ширине (JUSTIFY) строках пробелы растянуты до
    # ~1.5 высоты строчной буквы — меньшее ядро рвёт строку на куски.
    # Строка-блоб: широкая (>=5mh), вытянутая (w>=3h), не выше 4mh.
    kx = max(int(1.4 * mh), 5)
    rows_dil = cv2.dilate(thr_text, np.ones((1, kx), np.uint8))
    rn, rlabels, rstats, _ = cv2.connectedComponentsWithStats(rows_dil, 8)
    row_mask  = np.zeros_like(thr)
    row_boxes: list[tuple[int, int, int, int]] = []   # (x, y, w, h) принятых строк
    for j in range(1, rn):
        rw = rstats[j, cv2.CC_STAT_WIDTH]
        rh = rstats[j, cv2.CC_STAT_HEIGHT]
        # Обычная строка: широкая, вытянутая, не выше 4mh. Очень широкий блоб
        # (>=10mh) принимаем с мягким ограничением высоты (8mh): это текст,
        # слипшийся с текстом штампа/печати поверх него. Совсем высокие
        # мега-блобы (текст, сцепившийся с грязевой полосой через всю
        # страницу) строками не считаем — их спасут собственные строки.
        if (rw >= 5 * mh and rw >= 3 * rh and rh <= 4 * mh) \
                or (rw >= 10 * mh and rh <= 8 * mh):
            row_mask[rlabels == j] = 255
            row_boxes.append((rstats[j, cv2.CC_STAT_LEFT],
                              rstats[j, cv2.CC_STAT_TOP], rw, rh))
    # Вертикальный запас: диакритика («ё», «й») и выносные элементы чуть
    # выше/ниже тела строки — не должны считаться мусором.
    ky = max(int(0.4 * mh) * 2 + 1, 3)
    row_mask = cv2.dilate(row_mask, np.ones((ky, 1), np.uint8))
    has_rows = bool(row_boxes)

    def _near_row(x: int, y: int, w: int, h: int) -> bool:
        """Компонента на одной строке с принятой строкой и недалеко от неё?
        Спасает короткие токены, отделённые широким пробелом: номера пунктов
        («1.»), последнее слово строки после табуляции и т.п."""
        for rx, ry, rw, rh in row_boxes:
            v_overlap = min(y + h, ry + rh) - max(y, ry)
            if v_overlap < 0.5 * h:
                continue
            gap = max(rx - (x + w), x - (rx + rw))
            if gap <= 4 * mh:
                return True
        return False

    remove_ids = []
    for i in range(1, n):
        x = stats[i, cv2.CC_STAT_LEFT];  y = stats[i, cv2.CC_STAT_TOP]
        w = stats[i, cv2.CC_STAT_WIDTH]; h = stats[i, cv2.CC_STAT_HEIGHT]
        area    = float(stats[i, cv2.CC_STAT_AREA])
        density = area / max(w * h, 1.0)

        # Штамп (рамка и содержимое) — удаляем ДО защиты линий:
        # стороны рамки сами похожи на линии разметки.
        if i in stamp_ids:
            remove_ids.append(i)
            continue
        # Сетка таблицы и всё внутри неё — разметка и текст ячеек, не трогаем
        if i in lattice_ids:
            continue
        if lattice_zones and _in_lattice_zone(x, y, w, h):
            continue
        # Содержимое рамки герба/эмблемы — не трогаем целиком
        if protect_zones and _in_protect_zone(x, y, w, h):
            continue
        # Крупная графика (эмблема без рамки, логотип): большая в обоих
        # измерениях и с заметной чернильностью — это содержимое документа,
        # не грязь (грязевые обломки мельче, кляксы ловятся правилом ниже).
        if w >= 3 * mh and h >= 3 * mh and 0.15 <= density < 0.45:
            continue
        # Тонкие длинные линии (границы таблиц, подчёркивания) — не трогаем
        if min(w, h) <= thin and max(w, h) >= 3 * mh:
            continue
        if area <= speck_max:                # мелкий мусор («соль-перец»)
            remove_ids.append(i)
            continue
        # Плотная тёмная полоса у кромки листа (тень сканера, чёрный край)
        touches_edge = x <= 2 or y <= 2 or x + w >= W - 2 or y + h >= H - 2
        if touches_edge and max(w, h) >= 4 * mh and density >= 0.5:
            remove_ids.append(i)
            continue
        # Вне текстовых строк — грязь (полосы сшивки, кляксы, случайные метки).
        # Исключение: токен на одной строке с текстом и близко к нему.
        if has_rows:
            sub_rows = row_mask[y:y + h, x:x + w]
            sub_lab  = labels[y:y + h, x:x + w]
            if not np.any(sub_rows[sub_lab == i]) and not _near_row(x, y, w, h):
                remove_ids.append(i)

    if not remove_ids:
        return gray
    # Раздуваем маску удаления на 2px: убираем антиалиасный ореол вокруг
    # пятна (светлее порога Otsu), иначе Tesseract видит бледный «призрак».
    rm_mask = np.isin(labels, np.asarray(remove_ids)).astype(np.uint8)
    rm_mask = cv2.dilate(rm_mask, np.ones((5, 5), np.uint8))
    cleaned = gray.copy()
    cleaned[rm_mask.astype(bool)] = 255
    log.debug("ocr_preprocess: удалено пятен/точек: %d (median_h=%.0fpx)",
              len(remove_ids), mh)
    return cleaned


def is_junk_image(pil_img) -> bool:
    """True если картинка, которую layout-модель Docling приняла за
    логотип/рисунок, — на деле мусор скана и её НЕ надо вставлять в DOCX:

      россыпь — грязевая полоса/пятно: много мелких крапинок, крупнейшая
                компонента — малая доля чернил, без крупной структуры
                (у логотипа есть крупные глифы/графика);
      штамп   — тонкоштриховая полая рамка, занимающая заметную часть кропа
                (канцелярская печать «Вх. №…»).

    Подписи сюда НЕ попадают: путь рендера подписи не вызывает этот фильтр.
    При недоступном cv2 — False (ничего не фильтруем).
    """
    if cv2 is None or np is None:
        return False
    try:
        gray = np.asarray(pil_img.convert("L"))
        if gray.size == 0:
            return True
        Hc, Wc = gray.shape[:2]
        thr = cv2.threshold(gray, 0, 255,
                            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        n, labels, stats, _ = cv2.connectedComponentsWithStats(thr, 8)
        if n <= 1:
            return True                       # пустой кроп
        areas = stats[1:, cv2.CC_STAT_AREA].astype(float)
        total = float(areas.sum())
        if total <= 0:
            return True

        hs = stats[1:, cv2.CC_STAT_HEIGHT].astype(float)
        ws = stats[1:, cv2.CC_STAT_WIDTH].astype(float)
        text_like = (hs >= 8) & (hs <= 120) & (ws >= 3)
        mh = float(np.median(hs[text_like])) if text_like.any() else 20.0
        mh = min(max(mh, 8.0), 80.0)

        # (а) россыпь крапинок: МНОГО компонент и НИ ОДНОЙ текстовой строки.
        # Текст организован в строки; у логотипа-эмблемы строк тоже нет, но
        # компонент мало (единицы крупных глифов) — он не попадает под правило.
        if n - 1 >= 15:
            text_ids = np.where(text_like)[0] + 1
            tmask = np.isin(labels, text_ids)
            kx = max(int(1.4 * mh), 5)
            rows_dil = cv2.dilate(thr, np.ones((1, kx), np.uint8))
            rn, rl, rstats, _ = cv2.connectedComponentsWithStats(rows_dil, 8)
            has_rows = False
            for j in range(1, rn):
                rx = rstats[j, cv2.CC_STAT_LEFT]
                ry = rstats[j, cv2.CC_STAT_TOP]
                rw = rstats[j, cv2.CC_STAT_WIDTH]
                rh = rstats[j, cv2.CC_STAT_HEIGHT]
                if not ((rw >= 5 * mh and rw >= 3 * rh and rh <= 4 * mh)
                        or (rw >= 10 * mh and rh <= 8 * mh)):
                    continue
                # Строка должна состоять из букво-подобных компонент, а не из
                # крапинок: >=50% чернил блоба — от текстоподобных компонент.
                blob = rl[ry:ry + rh, rx:rx + rw] == j
                ink  = thr[ry:ry + rh, rx:rx + rw] > 0
                ink_in_blob = ink & blob
                total_ink = int(ink_in_blob.sum())
                text_ink  = int((ink_in_blob & tmask[ry:ry + rh, rx:rx + rw]).sum())
                if total_ink > 0 and text_ink >= 0.5 * total_ink:
                    has_rows = True
                    break
            if not has_rows:
                return True

        # (б) рамка штампа на заметную часть кропа
        for fid, fx, fy, fw, fh in _find_stamp_frames(
                labels, stats, n, mh, Wc, Hc,
                max_w_frac=1.01, max_h_frac=1.01):
            if fw >= 0.5 * Wc and fh >= 0.4 * Hc:
                return True
        return False
    except Exception as exc:
        log.debug("is_junk_image: пропуск (%s)", exc)
        return False


def preprocess_for_ocr(
    image,
    deskew: bool = True,
    denoise: bool = False,   # medianBlur размывает текст и СНИЖАЕТ confidence на всех
    clahe: bool = True,      # сканах (A/B-тест) — по умолчанию выкл.
    binarize: bool = False,
    despeckle: bool = True,  # удаление пятен/грязи скана (remove_stains)
    max_skew_deg: float = 7.0,
):
    """Предобрабатывает numpy-изображение для OCR. Возвращает RGB numpy (3 канала).

    Не-numpy вход (путь/None) или отсутствие cv2 - возвращаем как есть (no-op).
    max_skew_deg — углы больше считаем ошибкой детекции (не вращаем).
    """
    if cv2 is None or np is None or not isinstance(image, np.ndarray):
        return image
    try:
        img = image
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img.copy()

        # 1) Deskew — выравниваем наклон (только небольшие углы 0.2..max_skew_deg)
        if deskew:
            angle = _estimate_skew_deg(gray)
            if 0.2 < abs(angle) <= max_skew_deg:
                gray = _rotate(gray, angle)
                log.debug("ocr_preprocess: deskew на %.2f°", angle)

        # 2) Despeckle — удаление пятен/грязи скана (полосы сшивки, чёрные края,
        # россыпь точек) по связным компонентам. ДО CLAHE: иначе контраст
        # усиливает и пятна тоже.
        if despeckle:
            gray = remove_stains(gray)

        # 2b) Denoise — медианный фильтр 3×3: быстро убирает точки «соль-перец»
        # (которые читаются как «[», «]», «1»), сохраняя штрихи текста ≥3px.
        # NL-means точнее, но ~1s/страницу — для батча неприемлемо.
        if denoise:
            gray = cv2.medianBlur(gray, 3)

        # 3) CLAHE — адаптивный контраст (вытягивает бледный текст)
        if clahe:
            gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

        # 4) Бинаризация — ОПЦИЯ (по умолчанию выкл; включать после проверки)
        if binarize:
            gray = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 15)

        return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    except Exception as exc:
        log.debug("ocr_preprocess: пропуск (ошибка: %s)", exc)
        return image


_ORIG_RUN = None            # оригинальный _run_tesseract (сохраняется один раз)
_INSTALLED: tuple | None = None   # параметры установленного патча


def install_tesseract_preprocess(
    deskew: bool = True,
    denoise: bool = False,
    clahe: bool = True,
    binarize: bool = False,
    despeckle: bool = True,
) -> bool:
    """Monkeypatch TesseractOcrCliModel._run_tesseract: предобрабатывает temp-PNG
    страницы перед запуском tesseract CLI. Повторный вызов с теми же параметрами —
    no-op; с другими — переустанавливает патч поверх оригинала (не наслаивается).
    Возвращает True если патч установлен."""
    global _ORIG_RUN, _INSTALLED
    params = (deskew, denoise, clahe, binarize, despeckle)
    if _INSTALLED == params:
        return True
    if cv2 is None:
        log.warning("ocr_preprocess: cv2 недоступен — предобработка отключена")
        return False
    try:
        from docling.models.stages.ocr.tesseract_ocr_cli_model import (
            TesseractOcrCliModel,
        )
    except Exception as exc:
        log.warning("ocr_preprocess: модель Tesseract в Docling недоступна (%s)", exc)
        return False

    if _ORIG_RUN is None:
        _ORIG_RUN = TesseractOcrCliModel._run_tesseract
    _orig_run = _ORIG_RUN

    def _run_pre(self, ifilename, osd=None):
        try:
            bgr = cv2.imread(ifilename, cv2.IMREAD_COLOR)
            if bgr is not None:
                rgb = preprocess_for_ocr(bgr[:, :, ::-1], deskew=deskew,
                                         denoise=denoise, clahe=clahe,
                                         binarize=binarize, despeckle=despeckle)
                cv2.imwrite(ifilename, rgb[:, :, ::-1])
        except Exception as exc:
            log.debug("ocr_preprocess: пропуск (%s)", exc)
        return _orig_run(self, ifilename, osd)

    TesseractOcrCliModel._run_tesseract = _run_pre
    _INSTALLED = params
    log.info("ocr_preprocess: предобработка включена (deskew=%s denoise=%s clahe=%s "
             "binarize=%s despeckle=%s)",
             deskew, denoise, clahe, binarize, despeckle)
    return True
