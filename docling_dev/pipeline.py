"""Настройка Docling pipeline для отсканированных PDF (OCR — Tesseract)."""
from __future__ import annotations

import glob
import logging
import os
import shutil

from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

log = logging.getLogger(__name__)

# Tesseract >= 5 с языками rus, eng, osd (osd нужен Docling для определения
# ориентации страницы). Порядок поиска: вендоренная копия в vendor/tesseract/
# (полная автономность, без установки на машину) → PATH → стандартный путь
# установщика UB Mannheim (https://github.com/UB-Mannheim/tesseract/wiki).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TESSERACT_VENDORED = os.path.join(
    _REPO_ROOT, "vendor", "tesseract", "tesseract.exe")
_TESSERACT_FALLBACK = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Вендоренные модели Docling (layout heron + TableFormer accurate) — чтобы
# первая конвертация скана не лезла на huggingface.co. Если папки нет, Docling
# скачает модели сам (нужен интернет).
_DOCLING_MODELS_DIR = os.path.join(_REPO_ROOT, "models", "docling")


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

# Проект использует 2-буквенные коды языков, Tesseract — 3-буквенные (ISO 639-2)
_TESS_LANG_MAP = {"ru": "rus", "en": "eng"}


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


def _tesseract_options(langs: list[str]):
    from docling.datamodel.pipeline_options import TesseractCliOcrOptions
    return TesseractCliOcrOptions(
        lang=to_tesseract_langs(langs),          # ["rus", "eng"]
        tesseract_cmd=resolve_tesseract_cmd(),
        force_full_page_ocr=True,
        # psm=None → дефолт tesseract (3, полная авторазметка страницы)
    )


def build_converter(
    langs: list[str] | None = None,
    images_scale: float = 2.0,
    ocr_preprocess: bool = False,
) -> DocumentConverter:
    """
    Создаёт DocumentConverter для сканов:
      - Tesseract (CLI) с принудительным полностраничным OCR, языки rus+eng
      - TableFormer ACCURATE для таблиц
      - PyPdfium backend

    images_scale НЕ влияет на качество OCR: Docling-модель Tesseract сама рендерит
    страницу в 216 DPI (scale=3 внутри TesseractOcrCliModel). images_scale задаёт
    разрешение page images (generate_page_images), которые используют word_order
    и ink_bold — 2.0 → 144 DPI, достаточно и не раздувает память.
    """
    if langs is None:
        langs = ["ru", "en"]

    # Предобработка изображения перед Tesseract (deskew + CLAHE-контраст). ОПЦИЯ
    # (по умолчанию ВЫКЛ): помогает плохим сканам (наклон, серый фон, бледность),
    # но на хороших сканах может дать артефакты. Включать флагом --ocr-preprocess.
    if ocr_preprocess:
        try:
            from .ocr_preprocess import install_tesseract_preprocess
            install_tesseract_preprocess()
        except Exception as _exc:
            log.debug("ocr_preprocess недоступен: %s", _exc)

    ocr_opts = _tesseract_options(langs)

    pipeline_opts = PdfPipelineOptions()
    if os.path.isdir(_DOCLING_MODELS_DIR):
        _ensure_docling_models()
        pipeline_opts.artifacts_path = _DOCLING_MODELS_DIR
        log.info("Docling: вендоренные модели из %s", _DOCLING_MODELS_DIR)
    else:
        log.warning("Docling: %s не найден — модели возьмутся из кэша "
                    "HuggingFace или скачаются из сети", _DOCLING_MODELS_DIR)
    pipeline_opts.do_ocr                                   = True
    pipeline_opts.ocr_options                              = ocr_opts
    pipeline_opts.do_table_structure                       = True
    pipeline_opts.table_structure_options.do_cell_matching = True
    pipeline_opts.table_structure_options.mode             = TableFormerMode.ACCURATE
    pipeline_opts.generate_page_images                     = True
    pipeline_opts.images_scale                             = images_scale

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_opts,
                backend=PyPdfiumDocumentBackend,
            )
        }
    )
