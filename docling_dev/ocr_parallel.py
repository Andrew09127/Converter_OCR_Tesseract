# -*- coding: utf-8 -*-
"""
ocr_parallel.py — страницы батча распознаются параллельно (пункт 2.1).

Зачем. Пайплайн Docling 2.96 конвейерный: стадии (разбор, OCR, разметка,
таблицы) — отдельные потоки с очередями. Но внутри стадии OCR
`TesseractOcrCliModel.__call__` идёт по страницам батча строго последовательно:
один процесс tesseract на все логические ядра машины. Замер на 8 страницах в
216 DPI: 1 процесс — 28.5 s, 2 — 15.1 s, 4 — 8.0 s, 8 — 4.9 s (×5.9).
`OMP_THREAD_LIMIT` влияния не даёт — вендоренный билд OpenMP почти не
использует, процессы масштабируются линейно.

Что делаем. Патчим `__call__` и делим работу на две фазы:

  * ФАЗА А, параллельно (`ThreadPoolExecutor`): рендер страницы, временный PNG,
    OSD, tesseract, разбор TSV. Тяжёлая часть — внешние процессы, поэтому
    потоков достаточно: GIL отпущен на subprocess, cv2 и PDFium.
  * ФАЗА Б, строго по порядку страниц: сборка ячеек (`TextCell`),
    `post_process_cells`, выдача страницы. Собираем по индексу страницы, а не
    по готовности, — порядок результата не зависит от того, кто успел первым.

Картинки страниц между фазами НЕ держим: из фазы А наружу идёт только размер
изображения (он нужен для пересчёта координат), иначе при 8 воркерах в памяти
висело бы 8 полноразмерных страниц в 216 DPI.

Обработка ошибок повторяет Docling один в один: `CalledProcessError` на OSD —
в auto-режиме прямоугольник пропускается, иначе распознаём с ориентацией 0;
`CalledProcessError` на tesseract — прямоугольник пропускается. Остальные
исключения пробрасываем: конвертация должна падать громко, а не тихо терять
страницу.

ПОРЯДОК УСТАНОВКИ ВАЖЕН: кэш OSD (`ocr_osd`) тоже патчит `__call__` — там
заводятся границы документа. Его обёртка должна быть СНАРУЖИ нашей, иначе кэш
не создаётся и OSD считается на каждой странице. `install_parallel_ocr` следит
за этим сам: если патч OSD уже стоит, он снимается и ставится заново поверх.

ЧТО ЭТО ДАЛО (контролируемый A/B на 8 сканах корпуса, 27.10.2026): wall
318.5 → 211.7 s, то есть −34%. Сумма времён стадии OCR при этом не изменилась
(она теперь складывается по потокам и больше не равна wall). Выигрыш неровный:
на грязном 8-страничном файле 58.8 → 28.5 s, а на `Артемов` только 40.9 → 34.0,
потому что там узким местом стал TableFormer — он последовательный.

ИЗМЕРЕННЫЙ ПОТОЛОК: первая страница документа всегда распознаётся одна.
`ThreadedQueue.get_batch` в Docling батч не накапливает, поэтому первый батч
забирает единственную готовую страницу, и только следующий получает остальные
(в статистике это видно как «1 параллельный батч + 1 последовательный»).
Настройкой `batch_polling_interval_seconds` это НЕ лечится — она ограничивает
ожидание первого элемента, а не наполнение батча.

Число воркеров настраивается (`CONVERTER_OCR_WORKERS` или аргумент) — фаза 3
плана ограничивает его на слабых машинах.

Включается из `pipeline.build_converter`; там же `ocr_batch_size` поднимается
до числа воркеров, иначе параллелить внутри батча нечего.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

# По умолчанию ЧЕТЫРЕ, а не восемь, хотя сам tesseract масштабируется до ×5.9.
# Причина в коде Docling: `ThreadedQueue.get_batch` батч НЕ накапливает — он
# забирает то, что уже лежит в очереди, и ждёт только первый элемент. Поэтому
# одновременно в работу попадает 4-5 страниц, а не восемь, и восьмой воркер
# простаивает, занимая при этом память. Замер по корпусу: 4 воркера — 241.4 s
# при пике 2765 МБ, 8 воркеров — 245.9 s при пике 3143 МБ, то есть скорость та
# же, а пик на 378 МБ выше (каждый воркер держит свою страницу в 216 DPI,
# ~96 МБ). На многостраничных сканах, где очередь успевает наполниться, смысл
# поднять до 8 есть — для этого и переменная CONVERTER_OCR_WORKERS.
DEFAULT_MAX_WORKERS = 4
HARD_MAX_WORKERS = 32

_LOCK = threading.Lock()

# Статистика для замеров и тестов.
_STATS = {
    "batches": 0,          # вызовов __call__ с параллельной обработкой
    "batches_serial": 0,   # вызовов, отданных оригиналу (батч из одной страницы)
    "pages": 0,            # страниц, прошедших через параллельную фазу
    "peak_inflight": 0,    # максимум одновременно распознаваемых страниц
    "workers": 0,          # сколько воркеров настроено
}
_inflight = 0


def parallel_stats() -> dict:
    """Копия счётчиков."""
    with _LOCK:
        return dict(_STATS)


def reset_parallel_stats() -> None:
    global _inflight
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0
        _inflight = 0


def resolve_workers(workers: int | None = None) -> int:
    """Число процессов tesseract: аргумент → `CONVERTER_OCR_WORKERS` → по числу
    ядер, но не больше DEFAULT_MAX_WORKERS."""
    if workers is None:
        raw = os.environ.get("CONVERTER_OCR_WORKERS", "").strip()
        if raw:
            try:
                workers = int(raw)
            except ValueError:
                log.warning("CONVERTER_OCR_WORKERS=%r — не число, игнорирую", raw)
                workers = None
        if workers is None:
            workers = min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1)
    return max(1, min(int(workers), HARD_MAX_WORKERS))


def _enter_page() -> None:
    """Учёт одновременно работающих страниц — по нему тесты доказывают, что
    параллелизм действительно был, а не только задумывался."""
    global _inflight
    with _LOCK:
        _inflight += 1
        _STATS["pages"] += 1
        if _inflight > _STATS["peak_inflight"]:
            _STATS["peak_inflight"] = _inflight


def _leave_page() -> None:
    global _inflight
    with _LOCK:
        _inflight -= 1


def _docling_model_cls():
    try:
        from docling.models.stages.ocr.tesseract_ocr_cli_model import (
            TesseractOcrCliModel,
        )
    except Exception as exc:                        # noqa: BLE001
        log.warning("ocr_parallel: модель Tesseract в Docling недоступна (%s)", exc)
        return None
    return TesseractOcrCliModel


def uninstall_parallel_ocr(cls=None) -> bool:
    """Возвращает исходный `__call__` (используется тестами и переустановкой)."""
    if cls is None:
        cls = _docling_model_cls()
        if cls is None:
            return False
    if getattr(cls, "_parallel_patch_params", None) is None:
        return False
    cls.__call__ = cls._parallel_orig_call
    del cls._parallel_orig_call
    del cls._parallel_patch_params
    return True


def install_parallel_ocr(workers: int | None = None, cls=None) -> bool:
    """Ставит параллельную обработку страниц батча. Повторный вызов с тем же
    числом воркеров — no-op, с другим — переустановка поверх оригинала.

    `cls` — класс модели; по умолчанию `TesseractOcrCliModel` из Docling
    (параметр нужен тестам).
    """
    n = resolve_workers(workers)
    if cls is None:
        cls = _docling_model_cls()
        if cls is None:
            return False

    if getattr(cls, "_parallel_patch_params", None) == n:
        return True

    try:
        from docling_core.types.doc import BoundingBox, CoordOrigin
        from docling_core.types.doc.page import TextCell
        from docling.utils.ocr_utils import tesseract_box_to_bounding_rectangle
        from docling.utils.profiling import TimeRecorder
        from docling.datamodel.settings import settings
    except Exception as exc:                        # noqa: BLE001
        log.warning("ocr_parallel: не удалось взять типы Docling (%s)", exc)
        return False
    try:
        from docling.models.stages.ocr.tesseract_ocr_cli_model import (
            _parse_orientation,
        )
    except Exception:                               # noqa: BLE001
        from .ocr_osd import _orientation_of as _parse_orientation

    # Кэш OSD обёртывает тот же __call__ и должен остаться СНАРУЖИ (см.
    # «ПОРЯДОК УСТАНОВКИ ВАЖЕН» в шапке модуля). Если он уже стоит — снимаем,
    # ставим себя, возвращаем его на место.
    from . import ocr_osd
    osd_was_installed = getattr(cls, "_osd_patch_params", None) is not None
    if osd_was_installed:
        ocr_osd.uninstall_osd_cache(cls)

    uninstall_parallel_ocr(cls)
    orig_call = cls.__call__

    def _ocr_one(model, conv_res, page_i, page):
        """ФАЗА А для одной страницы: рендер, OSD, tesseract, разбор TSV.

        Возвращает (ocr_rects, parts), где parts — по кортежу на прямоугольник:
        (таблица результата, прямоугольник, ориентация, размер картинки).
        """
        _enter_page()
        try:
            ocr_rects = model.get_ocr_rects(page)
            parts = []
            for ocr_rect_i, ocr_rect in enumerate(ocr_rects):
                if ocr_rect.area() == 0:
                    continue
                image = page._backend.get_page_image(
                    scale=model.scale, cropbox=ocr_rect)
                fname = None
                try:
                    with tempfile.NamedTemporaryFile(
                            suffix=".png", mode="w+b", delete=False) as fh:
                        fname = fh.name
                        image.save(fh)
                    orientation = 0
                    df_osd = None
                    try:
                        df_osd = model._perform_osd(fname)
                        orientation = _parse_orientation(df_osd)
                    except subprocess.CalledProcessError as exc:
                        log.error("OSD не сработал (док %s, страница %s, "
                                  "прямоугольник %s, файл %s): %s",
                                  getattr(getattr(conv_res, "input", None),
                                          "file", "?"),
                                  page_i, ocr_rect_i, fname, exc.stderr)
                        # В auto-режиме без OSD не угадать язык — прямоугольник
                        # пропускаем; иначе распознаём с ориентацией 0.
                        if model._is_auto:
                            continue
                    if orientation != 0:
                        image = image.rotate(-orientation, expand=True)
                        image.save(fname)
                    try:
                        df_result = model._run_tesseract(fname, df_osd)
                    except subprocess.CalledProcessError as exc:
                        log.error("tesseract не сработал (док %s, страница %s, "
                                  "прямоугольник %s, файл %s): %s",
                                  getattr(getattr(conv_res, "input", None),
                                          "file", "?"),
                                  page_i, ocr_rect_i, fname, exc.stderr)
                        continue
                finally:
                    if fname and os.path.exists(fname):
                        try:
                            os.remove(fname)
                        except OSError as exc:
                            log.debug("не удалось убрать %s: %s", fname, exc)
                parts.append((df_result, ocr_rect, orientation, image.size))
            return ocr_rects, parts
        finally:
            _leave_page()

    def _cells_of(model, parts):
        """ФАЗА Б: таблицы tesseract → ячейки страницы. Чистая функция, идёт
        последовательно в порядке страниц."""
        cells = []
        for df_result, ocr_rect, orientation, im_size in parts:
            for ix, row in df_result.iterrows():
                text = row["text"]
                conf = row["conf"]
                left, top = float(row["left"]), float(row["top"])
                right = left + float(row["width"])
                bottom = top + row["height"]
                bbox = BoundingBox(l=left, t=top, r=right, b=bottom,
                                   coord_origin=CoordOrigin.TOPLEFT)
                rect = tesseract_box_to_bounding_rectangle(
                    bbox, original_offset=ocr_rect, scale=model.scale,
                    orientation=orientation, im_size=im_size)
                cells.append(TextCell(index=ix, text=str(text), orig=str(text),
                                      from_ocr=True, confidence=conf / 100.0,
                                      rect=rect))
        return cells

    def _call(self, conv_res, page_batch):
        if not self.enabled:
            yield from page_batch
            return

        pages = list(page_batch)
        # Параллелить нечего: отдаём работу оригиналу, чтобы не держать второй
        # путь исполнения для вырожденного случая.
        if n < 2 or len(pages) < 2:
            with _LOCK:
                _STATS["batches_serial"] += 1
                _STATS["workers"] = n
            yield from orig_call(self, conv_res, pages)
            return

        with _LOCK:
            _STATS["batches"] += 1
            _STATS["workers"] = n

        # Ключ тайминга заводим ДО пуска пула: внутри TimeRecorder делает только
        # append, а это под GIL безопасно, тогда как создание ключа — нет.
        if settings.debug.profile_pipeline_timings:
            TimeRecorder(conv_res, "ocr")

        def _work(item):
            page_i, page = item
            if page._backend is None or not page._backend.is_valid():
                return None
            with TimeRecorder(conv_res, "ocr"):
                return _ocr_one(self, conv_res, page_i, page)

        with ThreadPoolExecutor(max_workers=min(n, len(pages)),
                                thread_name_prefix="ocr") as pool:
            done = list(pool.map(_work, enumerate(pages)))

        for page, result in zip(pages, done):
            if result is None:                      # битый backend — как в Docling
                yield page
                continue
            ocr_rects, parts = result
            self.post_process_cells(_cells_of(self, parts), page)
            if settings.debug.visualize_ocr:
                self.draw_ocr_rects_and_cells(conv_res, page, ocr_rects)
            yield page

    cls.__call__ = _call
    cls._parallel_orig_call = orig_call
    cls._parallel_patch_params = n

    if osd_was_installed:
        ocr_osd.install_osd_cache(cls)

    log.info("ocr_parallel: страницы батча распознаются параллельно "
             "(%d процессов tesseract)", n)
    return True
