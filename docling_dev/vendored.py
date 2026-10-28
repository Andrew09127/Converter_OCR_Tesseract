"""
vendored.py
───────────
Пути к вендоренным инструментам и моделям: Tesseract, модели Docling, .gguf
для доочистки. Плюс обход Windows-сборки Tesseract по не-ASCII путям.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Всё это раньше жило в `pipeline.py`, а он на первой же
строке импортирует docling, то есть torch — 11 секунд и 400 МБ. После перехода
пакета на ленивые импорты (`docling_dev/__init__.py`) выяснилось, что
`GET /convert/health` всё равно платит эти 11 секунд: ему нужен лишь путь к
tesseract.exe и папка моделей, но он тянул их из `pipeline`. То же касалось
проверки TEMP в `errors.check_temp_dir`, то есть любой загрузки файла — включая
нативный режим, которому движок не нужен вовсе.

Здесь только стандартная библиотека. `pipeline` re-export'ит эти имена, чтобы
прежние импорты (`from .pipeline import resolve_tesseract_cmd`) продолжали
работать.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil

log = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tesseract >= 5 с языками rus, eng, osd (osd нужен Docling для определения
# ориентации страницы). Порядок поиска: вендоренная копия в vendor/tesseract/
# (полная автономность, без установки на машину) → PATH → стандартный путь
# установщика UB Mannheim (https://github.com/UB-Mannheim/tesseract/wiki).
_TESSERACT_VENDORED = os.path.join(
    _REPO_ROOT, "vendor", "tesseract", "tesseract.exe")
_TESSERACT_FALLBACK = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Вендоренные модели Docling (layout heron + TableFormer accurate) — чтобы
# первая конвертация скана не лезла на huggingface.co. Если папки нет, Docling
# скачает модели сам (нужен интернет).
_DOCLING_MODELS_DIR = os.path.join(_REPO_ROOT, "models", "docling")

# Модель доочистки: вендоренный .gguf в models/ рядом с проектом.
_LLM_MODEL = os.path.join(
    _REPO_ROOT, "models", "qwen2.5-1.5b-instruct-q4_k_m.gguf")

# Проект использует 2-буквенные коды языков, Tesseract — 3-буквенные (ISO 639-2)
_TESS_LANG_MAP = {"ru": "rus", "en": "eng"}


def _ensure_docling_models() -> None:
    """Склеивает *.safetensors из кусков `<имя>.partNNN`, если собранного файла
    нет (крупные модели режутся на <100 МБ ради лимита GitHub — тот же приём,
    что и для gguf-модели, см. llm_correct._resolve_model). Идемпотентно."""
    for first in glob.glob(os.path.join(_DOCLING_MODELS_DIR, "**", "*.part000"),
                           recursive=True):
        target = first[: -len(".part000")]
        if os.path.isfile(target):
            continue
        parts = sorted(glob.glob(target + ".part*"))
        try:
            log.info("Docling: собираю %s из %d кусков…",
                     os.path.basename(target), len(parts))
            with open(target, "wb") as out:
                for part in parts:
                    with open(part, "rb") as f:
                        shutil.copyfileobj(f, out, 1024 * 1024)
        except Exception as exc:
            log.warning("Docling: не удалось собрать %s: %s", target, exc)
            try:
                if os.path.isfile(target):
                    os.remove(target)         # убираем частично записанный файл
            except OSError:
                pass


def _ascii_safe_path(path: str) -> str | None:
    """Возвращает ASCII-вариант пути или None, если получить его нельзя.

    Windows-сборка Tesseract (mingw) не открывает файлы по путям с не-ASCII
    символами (кириллица и т.п.) — читает их как ANSI-байты. Обходим через
    короткие DOS-имена 8.3 (GetShortPathNameW), они всегда ASCII.
    """
    if path.isascii():
        return path
    if os.name != "nt":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
        if 0 < n < 1024 and buf.value.isascii():
            return buf.value
    except Exception as exc:
        log.debug("GetShortPathNameW не сработал: %s", exc)
    return None


def resolve_tesseract_cmd() -> str:
    """Путь к tesseract: vendor/tesseract → PATH → стандартная установка Windows.

    Вендоренная копия — приоритет: приложение работает автономно, без установки
    Tesseract на машину. Проверка PATH нужна и потому, что в уже запущенных
    процессах PATH мог не обновиться после установки.
    """
    if os.path.isfile(_TESSERACT_VENDORED):
        cmd = _ascii_safe_path(_TESSERACT_VENDORED)
        if cmd:
            log.info("Tesseract: вендоренная копия %s", cmd)
            return cmd
        log.warning(
            "Tesseract: vendor/tesseract найден, но путь содержит не-ASCII "
            "символы и короткие имена 8.3 недоступны — эта сборка Tesseract "
            "такие пути не открывает. Ищу системную установку.")
    if shutil.which("tesseract"):
        return "tesseract"
    if os.path.isfile(_TESSERACT_FALLBACK):
        return _TESSERACT_FALLBACK
    log.warning("Tesseract не найден ни в vendor/tesseract, ни в PATH, ни в %s",
                _TESSERACT_FALLBACK)
    return "tesseract"  # Docling выдаст явную RuntimeError при построении


def to_tesseract_langs(langs: list[str] | None) -> list[str]:
    """['ru', 'en'] → ['rus', 'eng']; уже 3-буквенные коды проходят как есть."""
    return [_TESS_LANG_MAP.get(l.lower(), l) for l in (langs or ["ru", "en"])]
