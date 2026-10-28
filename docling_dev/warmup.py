"""
warmup.py
─────────
Прогрев тяжёлой инициализации по запросу приложения (`POST /convert/warmup`).

ЗАЧЕМ ЭТО ЕСТЬ. Приложение sberAct дёргает `POST /convert/warmup` при запуске
convert-шага (fire-and-forget) и до появления этого модуля получало 404 —
ошибка там проглатывается, так что дефект был не виден ни нам, ни пользователю.

ЧТО ИМЕННО ГРЕЕМ (замерено 28.10.2026 в холодном процессе):
  • `DocumentConverter.initialize_pipeline` — загрузка моделей разметки и
    TableFormer. Публичный способ построить пайплайн без конвертации.
    1.83 s и +239 МБ — это ВЕСЬ прогрев;
  • `scipy.ndimage` (грузится внутри `base_ocr_model.get_ocr_rects`) и словарь
    pymorphy оказались уже загруженными — их затягивает построение конвертера
    на шаге выше, так что сами по себе эти шаги стоят 0.00 s. Оставлены как
    явная гарантия: цепочка импортов Docling не наша и может измениться.
Через HTTP: ответ за 11 ms, `state: ready` через 1.34 s, +233 МБ резидентно.

ЧЕГО ЗДЕСЬ СПЕЦИАЛЬНО НЕТ: прогона файла-заготовки через `convert()`. Он грел
ещё и OCR-стадию, но стоил 6 s в общей однопоточной очереди задач — пользователь,
нажавший кнопку сразу после запуска, ждал бы ДОЛЬШЕ, чем вообще без прогрева.

Прогрев необязателен по определению: любая неудача логируется и не ломает
конвертацию, которая просто заплатит холодный старт внутри задачи.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# cold → warming → ready | failed | skipped
_STATE: dict = {"state": "cold", "seconds": None, "steps": {}, "error": None}
_LOCK = threading.Lock()

_OFF = ("off", "0", "no", "false", "никогда")


def disabled() -> bool:
    """CONVERTER_WARMUP=off — не греть вообще (слабая машина: +233 МБ резидентно)."""
    return os.environ.get("CONVERTER_WARMUP", "").strip().lower() in _OFF


def state() -> dict:
    """Снимок состояния прогрева (копия — наружу отдаём не живой словарь)."""
    with _LOCK:
        return {**_STATE, "steps": dict(_STATE["steps"])}


def reset() -> None:
    """Сбросить состояние. Нужен тестам и повторному прогреву после сбоя."""
    with _LOCK:
        _STATE.update(state="cold", seconds=None, steps={}, error=None)


def _warm_models() -> None:
    """Модели разметки и таблиц. Греем ТОТ ЖЕ кэшированный конвертер, который
    возьмёт скан-задача: иначе в памяти окажутся два набора моделей."""
    from docling.datamodel.base_models import InputFormat
    from .api import _get_scan_converter
    _get_scan_converter(False).initialize_pipeline(InputFormat.PDF)


def _warm_scipy() -> None:
    """scipy.ndimage грузится внутри get_ocr_rects — первый раз 0.91 s."""
    import scipy.ndimage  # noqa: F401


def _warm_dictionary() -> None:
    """Словарь pymorphy строится при импорте highlight; вызов — чтобы ленивый
    разбор тоже прошёл хотя бы раз."""
    from .highlight import _pymorphy_known
    _pymorphy_known("прошу")


_STEPS = (("models", _warm_models), ("scipy", _warm_scipy),
          ("dictionary", _warm_dictionary))


def warmup() -> dict:
    """Прогреть синхронно. Идемпотентно: второй вызов ничего не делает.

    Возвращает состояние. Шаги независимы — падение одного не отменяет
    остальные, его текст попадает в `steps[имя]` и в `error`.
    """
    if disabled():
        with _LOCK:
            _STATE.update(state="skipped", error=None)
        log.info("warmup: отключён (CONVERTER_WARMUP=off)")
        return state()
    with _LOCK:
        if _STATE["state"] in ("warming", "ready"):
            return state()
        _STATE.update(state="warming", seconds=None, steps={}, error=None)

    t0 = time.perf_counter()
    errors: list[str] = []
    steps: dict[str, str] = {}
    for name, fn in _STEPS:
        step_t0 = time.perf_counter()
        try:
            fn()
            steps[name] = f"{time.perf_counter() - step_t0:.2f} s"
        except Exception as exc:                     # noqa: BLE001
            # Прогрев необязателен: сообщаем, но не мешаем работе конвертера.
            steps[name] = f"ошибка: {exc}"
            errors.append(f"{name}: {exc}")
            log.warning("warmup: шаг %s не прошёл: %s", name, exc)
    seconds = round(time.perf_counter() - t0, 2)

    with _LOCK:
        _STATE.update(state="failed" if len(errors) == len(_STEPS) else "ready",
                      seconds=seconds, steps=steps,
                      error="; ".join(errors) or None)
    log.info("warmup: %s за %.2f s (%s)", _STATE["state"], seconds,
             ", ".join(f"{k} {v}" for k, v in steps.items()))
    return state()
