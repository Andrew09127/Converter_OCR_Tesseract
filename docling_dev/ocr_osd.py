# -*- coding: utf-8 -*-
"""
ocr_osd.py — OSD (определение ориентации страницы) один раз на документ.

Зачем. Docling перед распознаванием КАЖДОЙ страницы запускает отдельный процесс
`tesseract --psm 0 -l osd` ради поворота на 90/180/270°. Замер: 1.38 s и ~10
CPU-секунд на 7-страничный файл, то есть 11% времени конвертации — при том, что
ориентация у страниц одного скана одна и та же. На малотекстовых страницах OSD
к тому же падает с «Too few characters», и Docling пишет ошибку в лог.

Что делаем. Патчим `TesseractOcrCliModel`:
  * `_perform_osd` — результат первой УСПЕШНОЙ пробы кэшируется на документ и
    переиспользуется, но ТОЛЬКО если ориентация нулевая. Если страница
    действительно повёрнута, кэш не включается и каждая страница проверяется
    честно: повёрнутые сканы бывают постранично разными, экономить на них нельзя.
  * `__call__` — границы документа (у `_perform_osd` нет ссылки на документ,
    поэтому кэш сбрасывается здесь, по смене `ConversionResult`).

Кэш рассчитан на параллельный OCR (`ocr_parallel`): первая проба документа идёт
под замком, остальные воркеры ждут её и переиспользуют результат. Если кэш
отключён (страница повёрнута), OSD считается без замка — иначе повёрнутый скан
сериализовался бы целиком.

ЗАМЕРЕНО И ОТВЕРГНУТО: проставлять в PNG разрешение (pHYs, 216 DPI), чтобы
убрать «Invalid resolution 0 dpi» — tesseract с известным разрешением меняет
разбор страницы, и на `Артемов` вывод падал с 995 до 869 слов и с 26 до 16
ячеек таблиц. Варнинг безобиден, вывод важнее.

Включается из `pipeline.build_converter`; на вывод распознавания не влияет —
при нулевой ориентации Docling и так не трогает картинку.
"""
from __future__ import annotations

import logging
import threading
import weakref

log = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Статистика для замеров и тестов: сколько раз реально запускали tesseract и
# сколько раз отдали кэш.
_STATS = {"osd_runs": 0, "osd_reused": 0, "osd_failures": 0, "docs": 0}


def osd_stats() -> dict:
    """Копия счётчиков (сколько процессов OSD сэкономили)."""
    with _LOCK:
        return dict(_STATS)


def reset_osd_stats() -> None:
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0


def uninstall_osd_cache(cls=None) -> bool:
    """Возвращает исходные методы Docling (используется тестами)."""
    if cls is None:
        try:
            from docling.models.stages.ocr.tesseract_ocr_cli_model import (
                TesseractOcrCliModel,
            )
        except Exception:                           # noqa: BLE001
            return False
        cls = TesseractOcrCliModel
    if getattr(cls, "_osd_patch_params", None) is None:
        return False
    cls._perform_osd = cls._osd_orig_perform
    cls.__call__ = cls._osd_orig_call
    del cls._osd_orig_perform, cls._osd_orig_call, cls._osd_patch_params
    return True


def _orientation_of(frame) -> int:
    """Градусы поворота из таблицы OSD; 0, если ключа нет или значение битое.

    Своя реализация вместо `docling._parse_orientation`, потому что та падает на
    таблице без строки «Orientation in degrees» (а именно так выглядит вывод
    tesseract на почти пустой странице).
    """
    try:
        keys = list(frame["key"])
        values = list(frame["value"])
    except Exception:
        return 0
    for k, v in zip(keys, values):
        if str(k).strip() == "Orientation in degrees":
            try:
                return int(round(float(str(v).strip()))) % 360
            except (TypeError, ValueError):
                return 0
    return 0


def _doc_token(conv_res):
    """Метка документа. Слабая ссылка, чтобы не держать предыдущий
    ConversionResult (вместе со всеми страницами) в памяти; если объект слабых
    ссылок не поддерживает — обходимся его адресом."""
    try:
        return weakref.ref(conv_res)
    except TypeError:
        # Адрес может переиспользоваться после сборки мусора, поэтому вместе с
        # ним сравниваем имя входного файла.
        src = getattr(getattr(conv_res, "input", None), "file", "")
        return ("id", id(conv_res), str(src))


def _same_doc(cache: dict, conv_res) -> bool:
    token = cache.get("token")
    if isinstance(token, tuple):
        return token[1] == id(conv_res)
    return token is not None and token() is conv_res


def install_osd_cache(cls=None) -> bool:
    """Ставит патч кэширования OSD. Повторный вызов — no-op.

    `cls` — класс модели; по умолчанию `TesseractOcrCliModel` из Docling
    (параметр нужен тестам, чтобы не тащить Docling).
    """
    if cls is None:
        try:
            from docling.models.stages.ocr.tesseract_ocr_cli_model import (
                TesseractOcrCliModel,
            )
        except Exception as exc:                    # noqa: BLE001
            log.warning("ocr_osd: модель Tesseract в Docling недоступна (%s)", exc)
            return False
        cls = TesseractOcrCliModel

    if getattr(cls, "_osd_patch_params", None) is not None:
        return True

    # Оригиналы берём из того, что положили при первой установке, иначе
    # повторный вызов обернул бы уже обёрнутое.
    orig_osd = getattr(cls, "_osd_orig_perform", None) or cls._perform_osd
    orig_call = getattr(cls, "_osd_orig_call", None) or cls.__call__

    def _run_osd(self, ifilename: str):
        """Честный запуск OSD со счётчиками."""
        try:
            frame = orig_osd(self, ifilename)
        except Exception:
            # Падение OSD (обычно «Too few characters») НЕ кэшируем: следующая
            # страница может оказаться текстовой и дать честный ответ. Исключение
            # пробрасываем — Docling сам решает, что делать (в не-auto режиме
            # продолжает распознавание с ориентацией 0).
            with _LOCK:
                _STATS["osd_failures"] += 1
            raise
        with _LOCK:
            _STATS["osd_runs"] += 1
        return frame

    def _perform_osd(self, ifilename: str):
        cache = getattr(self, "_osd_doc_cache", None)
        if cache is None:
            return _run_osd(self, ifilename)
        # Первую пробу документа держим под замком: при параллельном OCR
        # (см. ocr_parallel) иначе все воркеры разом увидят пустой кэш и
        # запустят по процессу OSD вместо одного.
        with cache["lock"]:
            if cache["reuse"]:
                with _LOCK:
                    _STATS["osd_reused"] += 1
                return cache["frame"]
            if not cache["decided"]:
                frame = _run_osd(self, ifilename)
                orientation = _orientation_of(frame)
                cache["frame"] = frame
                cache["reuse"] = orientation == 0
                cache["decided"] = True
                if orientation:
                    log.info("ocr_osd: страница повёрнута на %d° — кэш отключён "
                             "на ВЕСЬ документ, OSD будет честно считаться на "
                             "каждой странице", orientation)
                return frame
        # Кэш отключён (в документе есть повёрнутые страницы): считаем честно и
        # БЕЗ замка, иначе параллельный OCR такого скана сериализуется целиком.
        # Решение «отключён» не отменяется до конца документа: если одна
        # страница повёрнута, у остальных ориентация тоже под вопросом.
        return _run_osd(self, ifilename)

    def _call(self, conv_res, page_batch):
        cache = getattr(self, "_osd_doc_cache", None)
        if cache is None or not _same_doc(cache, conv_res):
            self._osd_doc_cache = {"token": _doc_token(conv_res),
                                   "frame": None, "reuse": False,
                                   "decided": False, "lock": threading.Lock()}
            with _LOCK:
                _STATS["docs"] += 1
        return orig_call(self, conv_res, page_batch)

    cls._osd_orig_perform = orig_osd
    cls._osd_orig_call = orig_call
    cls._perform_osd = _perform_osd
    cls.__call__ = _call
    cls._osd_patch_params = True
    log.info("ocr_osd: OSD кэшируется на документ (один процесс вместо одного "
             "на страницу)")
    return True
