"""
joblog.py
─────────
Лог на задачу (пункт 4.7).

ЗАЧЕМ. Код ошибки и подсказка (п.4.2) говорят пользователю, что делать, но не
объясняют РАЗРАБОТЧИКУ, что именно произошло. Общий лог сервера для этого
плохо годится: в sberAct stdout сайдкара уходит в console.log Electron, и
достать его с машины пользователя нельзя. Поэтому рядом с файлами задачи
(`%TEMP%/convert_api_jobs/<job_id>/`) пишется свой `job.log` — он переживает
перезапуск вместе с `job.json` (п.4.5) и отдаётся по `GET /convert/log/{id}`.

ЧТО ВНУТРИ. Записи логгеров за время работы задачи: наши стадии и
предупреждения, ошибки Docling, трассировка падения. Обработчик вешается на
КОРНЕВОЙ логгер — иначе часть записей (Docling, PIL, torch) не попадёт, а
именно они обычно и объясняют сбой. `uvicorn.access` отфильтрован: запросы
соседних вкладок к делу не относятся.

ПРО УРОВЕНЬ. Приложение настраивает логирование само (в sberAct — своё), и его
корневой уровень может быть WARNING. Тогда наши INFO-строки до обработчика не
дойдут вообще: логгер отсекает запись раньше обработчиков. Поэтому на время
задачи уровень логгера `docling_dev` поднимается до нужного и возвращается
обратно в `finally`.

Задачи сериализованы одним воркером, поэтому одновременно пишется не больше
одного лога.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)

LOG_NAME = "job.log"
# Docling на грязном скане пишет много; 2 МБ хватает на разбор, а диск не ест.
_MAX_BYTES = 2 * 1024 * 1024
_FORMAT = "%(asctime)s  %(levelname)-7s %(name)s  %(message)s"


def log_path(job_dir: Path) -> Path:
    return Path(job_dir) / LOG_NAME


def _level() -> int:
    """CONVERTER_JOB_LOG_LEVEL=DEBUG|INFO|WARNING|off (по умолчанию INFO)."""
    raw = os.environ.get("CONVERTER_JOB_LOG_LEVEL", "").strip().upper()
    if raw in ("OFF", "NONE", "0", "FALSE"):
        return 0
    return getattr(logging, raw, logging.INFO) if raw else logging.INFO


# Логгеры, чьи записи к задаче не относятся: обращения веб-сервера и
# HTTP-клиента (опрос статуса идёт раз в секунду и забивает лог).
_NOISE = ("uvicorn.access", "httpx", "httpcore")


class _NoAccessLog(logging.Filter):
    """Отбрасывает записи про HTTP-обмен: они только мешают читать лог."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_NOISE)


@contextmanager
def capture(job_dir: Path, job_id: str = ""):
    """Писать лог задачи в `<job_dir>/job.log` на время блока.

    Любая неудача с файлом НЕ ломает конвертацию: лог — диагностика, а не
    результат. В этом случае блок просто выполняется без записи.
    """
    level = _level()
    if not level:
        yield None
        return
    handler = None
    root = logging.getLogger()
    ours = logging.getLogger("docling_dev")
    saved_level = ours.level
    try:
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(log_path(job_dir)), maxBytes=_MAX_BYTES,
                                      backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.setLevel(level)
        handler.addFilter(_NoAccessLog())
        root.addHandler(handler)
        if saved_level > level or saved_level == logging.NOTSET:
            ours.setLevel(level)
        logging.getLogger(__name__).info("задача %s: лог начат", job_id)
    except Exception as exc:                        # noqa: BLE001
        log.debug("joblog: лог задачи %s не открылся: %s", job_id, exc)
        if handler is not None:
            root.removeHandler(handler)
            handler = None
    try:
        yield handler
    finally:
        if handler is not None:
            logging.getLogger(__name__).info("задача %s: лог закрыт", job_id)
            root.removeHandler(handler)
            handler.close()
        ours.setLevel(saved_level)


def read_tail(job_dir: Path, limit: int = 64 * 1024) -> str:
    """Последние `limit` байт лога. Пустая строка — лога нет.

    Отдаём хвост, а не начало: интересует то, что было перед сбоем. Обрезанный
    первый символ не страшен — читаем с заменой нечитаемых байт.
    """
    path = log_path(job_dir)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > limit:
                f.seek(size - limit)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    if size > limit:
        # Первая строка почти наверняка обрезана посередине — не показываем её.
        text = text.split("\n", 1)[-1]
        text = f"…(показаны последние {limit // 1024} КБ)\n{text}"
    return text
