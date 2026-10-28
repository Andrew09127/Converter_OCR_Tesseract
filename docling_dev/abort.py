"""
abort.py
────────
Прерывание идущей конвертации: отмена по кнопке и срок выполнения (пункт 4.6).

ПОЧЕМУ ТАК СЛОЖНО. Конвертация — один блокирующий вызов `converter.convert()`
в рабочем потоке, а поток в Python убить нельзя. Значит прерывание только
кооперативное: код сам должен проверять, не пора ли остановиться.

ГДЕ ПРОВЕРЯТЬ — выяснено замером. Сначала проверка стояла на границе страницы
в счётчике прогресса (`progress.page_done`), но это НЕ ПОМОГЛО: Docling ловит
исключение постранично, записывает его в ошибки страницы и СПОКОЙНО ИДЁТ
ДАЛЬШЕ по всем страницам и стадиям. Прогон занял те же 21 s вместо 22 s —
отмена не экономила ничего. Поэтому проверка стоит на входе в КАЖДУЮ тяжёлую
стадию (OCR, разметка, таблицы, сборка): тогда оставшиеся страницы пролетают
через конвейер, ничего не делая.

Итог один и тот же: Docling завершает прогон со статусом FAILURE и бросает
`ConversionError`. Разбирать его текст не нужно — задача и так знает, что
просила остановиться, и ставит свой код (`CANCELLED` или `TIMEOUT`).

Состояние модульное, как и у счётчика страниц: конвертации в API сериализованы
одним воркером, параллельных прогонов не бывает.
"""
from __future__ import annotations

import logging
import threading
import time

from .errors import JobAborted

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
# code — запрошенный код прерывания (CANCELLED | TIMEOUT), None — работаем;
# deadline — момент времени (time.monotonic), после которого срок истёк.
_STATE: dict = {"code": None, "detail": "", "deadline": None}


def request(code: str = "CANCELLED", detail: str = "") -> None:
    """Попросить прекратить текущую конвертацию."""
    with _LOCK:
        _STATE.update(code=code, detail=detail)
    log.info("abort: запрошено прекращение (%s) %s", code, detail)


def start(timeout_s: float | None = None) -> None:
    """Начать новую задачу: снять прошлый запрос и задать срок (0/None — без)."""
    with _LOCK:
        _STATE.update(code=None, detail="",
                      deadline=(time.monotonic() + timeout_s
                                if timeout_s and timeout_s > 0 else None))


def finish() -> None:
    """Снять запрос и срок. Обязательно в `finally`: иначе следующая задача
    прекратится сразу же, за чужой счёт."""
    start(None)


def requested() -> str | None:
    """Код запрошенного прерывания или None."""
    with _LOCK:
        return _STATE["code"]


def state() -> dict:
    with _LOCK:
        left = (None if _STATE["deadline"] is None
                else round(_STATE["deadline"] - time.monotonic(), 1))
        return {"code": _STATE["code"], "detail": _STATE["detail"],
                "seconds_left": left}


def check() -> None:
    """Бросает `JobAborted`, если пора остановиться. Зовётся из стадий."""
    with _LOCK:
        code, detail, deadline = (_STATE["code"], _STATE["detail"],
                                  _STATE["deadline"])
        if code is None and deadline is not None and time.monotonic() > deadline:
            # Срок считаем здесь же: отдельный таймер потребовал бы ещё одного
            # потока, а проверка и так вызывается на каждой странице.
            code = "TIMEOUT"
            detail = "истёк срок выполнения задачи"
            _STATE.update(code=code, detail=detail)
            log.warning("abort: %s", detail)
    if code:
        raise JobAborted(code, detail)


# Стадии конвейера, на входе в которые проверяем прерывание. Порядок не важен:
# важно покрыть всё тяжёлое, иначе отмена не экономит времени.
_TARGETS = (
    ("docling.models.stages.ocr.tesseract_ocr_cli_model", "TesseractOcrCliModel"),
    ("docling.models.stages.layout.layout_model", "LayoutModel"),
    ("docling.models.stages.table_structure.table_structure_model",
     "TableStructureModel"),
    ("docling.models.stages.page_assemble.page_assemble_model",
     "PageAssembleModel"),
)


def _classes() -> list:
    out = []
    for module_name, cls_name in _TARGETS:
        try:
            module = __import__(module_name, fromlist=[cls_name])
            out.append(getattr(module, cls_name))
        except Exception as exc:                    # noqa: BLE001
            log.warning("abort: стадия %s недоступна (%s)", cls_name, exc)
    return out


def _wrap(cls) -> bool:
    if getattr(cls, "_abort_patched", None) is not None:
        return True
    orig_call = cls.__call__

    def _call(self, conv_res, page_batch):
        # Проверка ДО работы: ради этого всё и делается.
        check()
        for page in orig_call(self, conv_res, page_batch):
            check()
            yield page

    cls.__call__ = _call
    cls._abort_orig_call = orig_call
    cls._abort_patched = True
    return True


def install_abort_checks(classes=None) -> int:
    """Поставить проверки прерывания на стадии конвейера. Возвращает их число.

    Ставить ПОСЛЕ остальных патчей стадий (параллельный OCR, OSD, счётчик
    страниц): обёртка должна оказаться снаружи, иначе проверка попадёт внутрь
    чужой логики.
    """
    done = 0
    for cls in (classes if classes is not None else _classes()):
        if _wrap(cls):
            done += 1
    if done:
        log.info("abort: проверки прерывания на %d стадиях", done)
    return done


def uninstall_abort_checks(classes=None) -> int:
    """Снять проверки (используется тестами)."""
    done = 0
    for cls in (classes if classes is not None else _classes()):
        if getattr(cls, "_abort_patched", None) is None:
            continue
        cls.__call__ = cls._abort_orig_call
        del cls._abort_orig_call
        del cls._abort_patched
        done += 1
    return done
