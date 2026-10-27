# -*- coding: utf-8 -*-
"""
progress.py — прогресс по страницам для интерфейса (пункт 2.5).

Зачем. `api.py` ставил три отметки: 0.1 → 0.35 → 1.0. На скане, который идёт
минуту и больше, полоса стоит на 35% почти всё время, и это выглядит как
зависание — хотя работа идёт.

ГДЕ СЧИТАТЬ. Не на OCR: после пункта 2.1 распознавание идёт параллельно и
заканчивается рано, а дальше ещё работают разметка и таблицы (в профиле
table_structure 37%, layout 25%) — полоса доехала бы до конца и снова
«зависла». Считаем на последней стадии конвейера, `PageAssembleModel`: стадии
идут preprocess → ocr → layout → table → assemble, и страница, дошедшая до
сборки, прошла ВСЁ. Батч этой стадии равен 1, поэтому сигнал постраничный.

Счётчик модульный, а не на задачу: конвертации в API сериализованы (один
воркер `_EXECUTOR`), параллельных прогонов не бывает. В CLI подписчика нет —
патч работает как no-op.

Включается из `pipeline.build_converter`.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
# cb — подписчик (done, total); None — считать некому, патч ничего не делает.
_STATE: dict = {"total": 0, "done": 0, "cb": None}


def start(total_pages: int, callback=None) -> None:
    """Начать отсчёт для документа. `callback(done, total)` — куда сообщать."""
    with _LOCK:
        _STATE.update(total=max(int(total_pages or 0), 0), done=0, cb=callback)


def finish() -> None:
    """Снять подписчика. Обязательно в `finally`: иначе следующая конвертация
    будет досчитывать страницы в чужую задачу."""
    with _LOCK:
        _STATE.update(total=0, done=0, cb=None)


def stats() -> dict:
    with _LOCK:
        return {"total": _STATE["total"], "done": _STATE["done"],
                "listening": _STATE["cb"] is not None}


def page_done(n: int = 1) -> None:
    """Страница прошла конвейер целиком. Зовётся из патча сборки страниц."""
    with _LOCK:
        if _STATE["cb"] is None:
            return
        _STATE["done"] += n
        done, total, cb = _STATE["done"], _STATE["total"], _STATE["cb"]
    # Подписчика зовём БЕЗ нашего замка: он берёт свои (в api.py — замок задач),
    # и держать два замка в разном порядке — прямой путь к взаимной блокировке.
    try:
        cb(done, total)
    except Exception as exc:                        # noqa: BLE001
        log.debug("progress: подписчик упал (%s)", exc)


def _model_cls():
    try:
        from docling.models.stages.page_assemble.page_assemble_model import (
            PageAssembleModel,
        )
    except Exception as exc:                        # noqa: BLE001
        log.warning("progress: модель сборки страниц недоступна (%s)", exc)
        return None
    return PageAssembleModel


def uninstall_page_progress(cls=None) -> bool:
    """Возвращает исходный `__call__` (используется тестами)."""
    if cls is None:
        cls = _model_cls()
        if cls is None:
            return False
    if getattr(cls, "_progress_patched", None) is None:
        return False
    cls.__call__ = cls._progress_orig_call
    del cls._progress_orig_call
    del cls._progress_patched
    return True


def install_page_progress(cls=None) -> bool:
    """Патчит `PageAssembleModel.__call__`: считает прошедшие страницы.
    Повторный вызов — no-op."""
    if cls is None:
        cls = _model_cls()
        if cls is None:
            return False
    if getattr(cls, "_progress_patched", None) is not None:
        return True

    orig_call = cls.__call__

    def _call(self, conv_res, page_batch):
        for page in orig_call(self, conv_res, page_batch):
            page_done()
            yield page

    cls.__call__ = _call
    cls._progress_orig_call = orig_call
    cls._progress_patched = True
    log.info("progress: страницы считаются на стадии сборки")
    return True
