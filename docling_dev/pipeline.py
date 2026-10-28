"""Настройка Docling pipeline для отсканированных PDF (OCR — Tesseract)."""
from __future__ import annotations

import logging
import os

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
# Пути к вендоренным инструментам и моделям живут в `vendored.py`: там нет
# импорта docling, поэтому проверка окружения (`health`) и проверка TEMP
# (`errors`) не платят 11 секунд за torch. Имена re-export'им — на них
# ссылаются `word_order`, `health` и тесты.
from .vendored import (                            # noqa: F401
    _DOCLING_MODELS_DIR,
    _LLM_MODEL,
    _TESSERACT_FALLBACK,
    _TESSERACT_VENDORED,
    _ascii_safe_path,
    _ensure_docling_models,
    _REPO_ROOT,
    _TESS_LANG_MAP,
    resolve_tesseract_cmd,
    to_tesseract_langs,
)


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
    images_scale: float | None = None,
    ocr_preprocess: bool = False,
    ocr_workers: int | None = None,
    profile: str | None = None,
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

    ocr_workers — сколько страниц распознавать одновременно (None → из профиля,
    либо CONVERTER_OCR_WORKERS). Каждый воркер держит свою страницу в 216 DPI
    (~96 МБ) и свой процесс tesseract: пик памяти растёт примерно на 100 МБ за
    каждую сверхплановую страницу, поэтому на слабых машинах воркеров опускают.

    profile — ресурсный профиль (`lean` | `balanced` | `full` | `auto`); None →
    `CONVERTER_PROFILE` или выбор по машине. Профиль задаёт воркеров, потоки
    моделей и images_scale, см. docling_dev/profiles.py. Явные аргументы
    `images_scale` и `ocr_workers` профиль перебивают — это нужно замерам.
    """
    if langs is None:
        langs = ["ru", "en"]

    # Профиль под машину: на слабой лучше медленнее, чем MemoryError.
    from .profiles import apply_threads, describe, resolve
    prof = resolve(profile)
    apply_threads(prof)
    log.info("%s", describe(prof))
    if images_scale is None:
        images_scale = prof.images_scale
    if ocr_workers is None and not os.environ.get("CONVERTER_OCR_WORKERS", "").strip():
        ocr_workers = prof.ocr_workers

    # Предобработка изображения перед Tesseract.
    # despeckle (удаление пятен/грязи скана) — ВСЕГДА ВКЛ: строкоцентричный
    # алгоритм консервативен, на чистых сканах ему нечего удалять.
    # deskew + CLAHE-контраст — только под флагом --ocr-preprocess: помогают
    # плохим сканам (наклон, серый фон), но на хороших могут дать артефакты.
    try:
        from .ocr_preprocess import install_tesseract_preprocess
        install_tesseract_preprocess(
            deskew=ocr_preprocess,
            clahe=ocr_preprocess,
            despeckle=True,
        )
    except Exception as _exc:
        log.debug("ocr_preprocess недоступен: %s", _exc)

    # Страницы батча распознаются параллельно: Docling внутри стадии OCR идёт
    # по страницам последовательно, то есть один процесс tesseract на всю
    # машину. Замер: 8 страниц в 216 DPI — 28.5 s в один процесс против 4.9 s в
    # восемь. Ставится ДО кэша OSD (тот тоже патчит __call__ и должен остаться
    # снаружи) — порядок модуль соблюдает сам, см. ocr_parallel.
    workers = 1
    try:
        from .ocr_parallel import install_parallel_ocr, resolve_workers
        workers = resolve_workers(ocr_workers)
        install_parallel_ocr(workers)
    except Exception as _exc:
        log.debug("ocr_parallel недоступен: %s", _exc)

    # OSD (ориентация страницы) — один процесс tesseract на документ вместо
    # одного на страницу: 11% времени конвертации уходило на перепроверку одной
    # и той же ориентации. Повёрнутые страницы при этом не ломаются — см.
    # ocr_osd.install_osd_cache.
    try:
        from .ocr_osd import install_osd_cache
        install_osd_cache()
    except Exception as _exc:
        log.debug("ocr_osd недоступен: %s", _exc)

    # TableFormer на грязных сканах срывается в генерации структуры: таблице 4x6
    # вместо 28 токенов выдаёт 266 и считает её 19 секунд. Предел длины обрезает
    # только этот мусорный хвост — см. docling_dev/table_limits.py.
    try:
        from .table_limits import install_table_seq_limit
        install_table_seq_limit()
    except Exception as _exc:
        log.debug("table_limits недоступен: %s", _exc)

    # Счёт страниц для полосы прогресса в интерфейсе: на последней стадии
    # конвейера, где страница уже прошла и OCR, и разметку, и таблицы.
    # Без подписчика (CLI) — no-op, см. docling_dev/progress.py.
    try:
        from .progress import install_page_progress
        install_page_progress()
    except Exception as _exc:
        log.debug("progress недоступен: %s", _exc)

    # Прерывание задачи (отмена, истёкший срок) — пункт 4.6. Ставится ПОСЛЕДНИМ
    # из патчей стадий: обёртка с проверкой должна быть снаружи остальных.
    try:
        from .abort import install_abort_checks
        install_abort_checks()
    except Exception as _exc:
        log.debug("abort недоступен: %s", _exc)

    ocr_opts = _tesseract_options(langs)

    pipeline_opts = PdfPipelineOptions()
    # Потоки моделей: Docling по умолчанию берёт 4 независимо от машины, а на
    # двух ядрах это душит интерфейс.
    pipeline_opts.accelerator_options.num_threads = prof.num_threads
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
    # Параллельный OCR раздаёт страницы ВНУТРИ батча, поэтому батч стадии OCR
    # обязан быть не меньше числа воркеров — при батче 1 параллелить нечего.
    pipeline_opts.ocr_batch_size                           = max(workers, 1)
    # А вот разметке и таблицам батч только вредит: стадии конвейера работают
    # параллельно, и при батче 4 стадия ждёт накопления четырёх страниц
    # (batch_polling_interval_seconds), ломая перекрытие, да ещё держит четыре
    # страницы в памяти разом. Замер по корпусу: 219.2 s и пик 2778 МБ при
    # батче 4 против 207.0 s и 1977 МБ при батче 1 — −801 МБ пика при
    # идентичном тексте DOCX на всех 8 сканах. Весь эффект даёт РАЗМЕТКА:
    # с layout=1 и table=4 выходит 209.1 s / 1965 МБ, то есть то же самое
    # (у таблиц батч почти не наполняется — на странице их редко больше трёх).
    # table_batch_size=1 оставлен ради предсказуемости на документах, где
    # таблиц на страницу много.
    pipeline_opts.layout_batch_size                        = 1
    pipeline_opts.table_batch_size                         = 1

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_opts,
                backend=PyPdfiumDocumentBackend,
            )
        }
    )
