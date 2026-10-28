"""
api.py
──────
HTTP-слой конвертера: FastAPI APIRouter.

Не запускает сервер сам по себе — только описывает эндпоинты. Подключается
в любой FastAPI-приложение строкой `app.include_router(router)`:
  • локально для разработки — из main.py (см. корень проекта);
  • в реальном использовании — из python-backend/main.py приложения sberAct.

Полностью локально, без сетевых вызовов. Логику конвертации не дублирует —
оборачивает существующие точки входа:
  • сканы          → docling_dev.convert_pdf (Docling + Tesseract + LLM)
  • нативные PDF    → convert_no_scan.PDFPipelineConverter.convert_single_pdf (pdf2docx)

Конвертация тяжёлая и блокирующая, поэтому выполняется в фоновом потоке, а
клиент опрашивает статус. Задачи сериализованы (один воркер) — приложение
однопользовательское локальное, параллельный прогон сканов только съел бы память.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import (APIRouter, BackgroundTasks, File, Form, HTTPException,
                     Response, UploadFile)
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/convert", tags=["convert"])

"""
── Хранилище задач
job_id → {status, stage, progress, mode, filename, docx_path, error}
  status:   queued | running | done | error
  progress: 0.0 … 1.0 (грубые отметки — convert_pdf одним блокирующим вызовом)
"""
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

# Один воркер — задачи идут по очереди (сериализация прогонов).
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="convert")

# Рабочая папка задач (вход PDF + выход DOCX). В системном temp, чистится при старте.
_WORK_DIR = Path(tempfile.gettempdir()) / "convert_api_jobs"
_WORK_DIR.mkdir(parents=True, exist_ok=True)

# Кэш Docling-конвертера: построение тяжёлое, поэтому держим
# по одному экземпляру на значение ocr_preprocess (оно меняет pipeline).
_SCAN_CONVERTERS: dict[bool, object] = {}
_SCAN_LOCK = threading.Lock()

# Кэш OCR-reader (Tesseract): нужен только для флага word_order
# (переупорядочивание блоков). Строим лениво один раз.
_OCR_READER: list = []          # [reader] или [] ; None-маркер недоступности — [None]
_OCR_READER_LOCK = threading.Lock()


def _set(job_id: str, **changes) -> None:
    """Атомарно обновить запись задачи."""
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(changes)


def _fail(job_id: str, err) -> None:
    """Записать типизированную ошибку в задачу (пункт 4.2).

    Поле `error` остаётся человеческой строкой — его интерфейс уже показывает;
    рядом появляются `error_code` и `error_hint`, по которым чужой интерфейс
    (sberAct) может решать сам, не разбирая русский текст.
    """
    _set(job_id, status="error", stage="Ошибка", error=err.text(),
         error_code=err.code, error_hint=err.hint,
         error_detail=err.detail or None)
    log.error("api: задача %s — [%s] %s", job_id, err.code,
              err.detail or err.message)


def _http_error(err, status_code: int = 400) -> HTTPException:
    """ConvertError → HTTPException со структурным detail.

    Обработчик исключений уровня приложения не поставить: в sberAct подключают
    только наш роутер, своё приложение они строят сами.
    """
    return HTTPException(status_code=status_code, detail=err.as_dict())


def _busy() -> bool:
    """True если есть задача в очереди или в работе. Воркер один, поэтому
    любая такая задача означает, что исполнитель занят."""
    with _JOBS_LOCK:
        return any(j.get("status") in ("queued", "running") for j in _JOBS.values())


def _get_scan_converter(ocr_preprocess: bool):
    """Лениво строит и кэширует Docling-конвертер под флаг предобработки."""
    with _SCAN_LOCK:
        conv = _SCAN_CONVERTERS.get(ocr_preprocess)
        if conv is None:
            from . import build_converter
            log.info("api: строю Docling-конвертер (ocr_preprocess=%s)", ocr_preprocess)
            conv = build_converter(ocr_preprocess=ocr_preprocess)
            _SCAN_CONVERTERS[ocr_preprocess] = conv
    return conv


def _get_ocr_reader():
    """Лениво строит Tesseract-reader для word_order. None — если недоступен."""
    with _OCR_READER_LOCK:
        if not _OCR_READER:
            try:
                from .word_order import TesseractWordReader
                log.info("api: инициализация Tesseract для word_order…")
                _OCR_READER.append(TesseractWordReader(["ru", "en"]))
                log.info("api: Tesseract готов.")
            except Exception as exc:                 # noqa: BLE001
                log.warning("api: Tesseract недоступен, word_order отключён: %s", exc)
                _OCR_READER.append(None)
        return _OCR_READER[0]
    
#Фоновые воркеры

#Прогресс по страницам (п.2.5)

# Распознавание занимает почти всё время задачи, поэтому страницы двигают
# полосу от 0.35 (файл принят, модели готовы) до 0.90 (страницы пройдены,
# остались сборка DOCX, подсветка и опционально LLM).
_PROGRESS_FROM = 0.35
_PROGRESS_TO = 0.90


def _page_progress(done: int, total: int) -> float:
    """Доля для полосы по числу пройденных страниц."""
    if total <= 0:
        return _PROGRESS_FROM
    share = min(max(done / total, 0.0), 1.0)
    return round(_PROGRESS_FROM + (_PROGRESS_TO - _PROGRESS_FROM) * share, 3)


def _page_count(pdf_path: Path) -> int:
    """Число страниц в PDF. 0 — если не прочитался: тогда полоса просто не
    двигается постранично, как было до п.2.5."""
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(str(pdf_path))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception as exc:                         # noqa: BLE001
        log.debug("api: не удалось узнать число страниц в %s (%s)",
                  pdf_path.name, exc)
        return 0


#Фоновые воркеры (продолжение)

def _run_scan(job_id: str, pdf_path: Path, docx_path: Path, flags: dict) -> None:
    """Прогон скана через Docling-пайплайн (медленный путь)."""
    from . import progress as page_progress
    from .errors import ConvertError, classify
    try:
        _set(job_id, status="running", stage="Анализ скана…", progress=0.1)
        converter = _get_scan_converter(flags["ocr_preprocess"])

        # OCR-reader нужен только для word_order — иначе не создаём.
        ocr_reader = _get_ocr_reader() if flags["word_order"] else None

        total = _page_count(pdf_path)

        def _on_page(done: int, pages: int) -> None:
            if done >= pages > 0:
                _set(job_id, stage="Сборка документа…",
                     progress=_page_progress(done, pages))
            else:
                # Фронт печатает это после слова «Обработка: », поэтому текст
                # короткий и без собственного двоеточия.
                _set(job_id, stage=f"Страница {done} из {pages}",
                     progress=_page_progress(done, pages))

        # Первая страница доходит до конца конвейера не сразу (замер: 11.8 s на
        # файле в 7 страниц), поэтому текст должен объяснять, чего ждём.
        _set(job_id, stage="Распознавание страниц…", progress=_PROGRESS_FROM)
        page_progress.start(total, _on_page)
        from . import convert_pdf_checked
        try:
            convert_pdf_checked(
                pdf_path, docx_path, converter,
                ocr_reader=ocr_reader,
                use_word_order=flags["word_order"],
                highlight=flags["highlight"],
                llm=flags["llm"],
                ink_bold=flags["ink_bold"],
            )
        finally:
            # Обязательно: иначе следующая задача досчитает страницы в эту.
            page_progress.finish()
        if docx_path.exists():
            _set(job_id, status="done", stage="Готово", progress=1.0)
        else:
            # Исключения не было, а файла нет — так быть не должно.
            _fail(job_id, ConvertError("INTERNAL",
                                       "DOCX не создан, хотя ошибки не было"))
    except Exception as exc:                     # noqa: BLE001 — отдаём текст клиенту
        log.exception("api: ошибка прогона скана %s", job_id)
        _fail(job_id, classify(exc))


def _run_native(job_id: str, pdf_path: Path, docx_path: Path) -> None:
    """Прогон нативного PDF через pdf2docx (быстрый путь, без флагов)."""
    from .errors import ConvertError, classify
    try:
        _set(job_id, status="running", stage="Конвертация PDF…", progress=0.2)
        # Берём только одиночную конвертацию — без батч-пайплайна, который удаляет исходники.
        from convert_no_scan import PDFPipelineConverter
        conv = PDFPipelineConverter(
            input_folder=str(pdf_path.parent),
            output_folder=str(docx_path.parent),
        )
        ok, msg = conv.convert_single_pdf(pdf_path, docx_path)
        if ok and docx_path.exists():
            _set(job_id, status="done", stage="Готово", progress=1.0)
        else:
            # pdf2docx — внешний чёрный ящик: кода он не даёт, но текст отдаёт.
            _fail(job_id, ConvertError("INTERNAL",
                                       str(msg or "pdf2docx вернул отказ")))
    except Exception as exc:                     # noqa: BLE001
        log.exception("api: ошибка прогона нативного PDF %s", job_id)
        _fail(job_id, classify(exc))
        
#Приём загруженного файла

def _accept_upload(file: UploadFile) -> tuple[str, Path, Path]:
    """Сохраняет загруженный PDF в папку задачи. Возвращает (job_id, pdf, docx)."""
    from .errors import ConvertError, check_pdf, check_temp_dir
    name = Path(file.filename or "document.pdf").name
    if not name.lower().endswith(".pdf"):
        raise _http_error(ConvertError("NOT_A_PDF", f"расширение файла: {name}"))

    job_id = uuid.uuid4().hex
    job_dir = _WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = job_dir / name
    with pdf_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    docx_path = job_dir / f"{Path(name).stem}.docx"
    # Пункт 4.2: запароленный, битый или не-PDF файл виден за миллисекунды —
    # отвечаем сразу и внятно, а не ставим в очередь задачу, которая упадёт
    # через час распознавания.
    try:
        check_pdf(pdf_path)
        check_temp_dir()
    except ConvertError as err:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise _http_error(err) from err
    return job_id, pdf_path, docx_path


#Эндпоинты

@router.get("/profile")
async def convert_profile():
    """Ресурсный профиль машины (пункт 3.1): по нему интерфейс решает, предлагать
    ли LLM-доочистку по умолчанию и чего ждать по времени.

    Профиль выбирается по ядрам и памяти; переключается переменной
    CONVERTER_PROFILE (lean | balanced | full | auto).
    """
    from .profiles import machine, resolve
    prof = resolve()
    m = machine()
    return {
        "profile": prof.name,
        "note": prof.note,
        "ocr_workers": prof.ocr_workers,
        "num_threads": prof.num_threads,
        "llm_default": prof.llm_default,
        "cores": m["cores"],
        "ram_gb": round(m["ram_gb"], 1),
        "free_gb": round(m["free_gb"], 1),
    }


@router.post("/warmup")
@router.get("/warmup")
async def convert_warmup(response: Response):
    """Прогрев моделей (пункт 4.0). Приложение sberAct дёргает этот маршрут при
    запуске convert-шага и до сих пор получало 404 — прогрева не происходило, и
    первый скан платил холодный старт внутри задачи.

    Fire-and-forget: отвечаем сразу, греем в фоне. GET принимается наравне с
    POST — так маршрут проверяется из браузера и из health-цикла обёртки.

    Поля ответа: `state` (cold|warming|ready|failed|skipped|busy), `scheduled` —
    поставили ли прогрев в этот раз, `seconds` и `steps` — замеры прошедшего
    прогрева, `error` — текст, если какой-то шаг не прошёл.
    """
    from . import warmup as warm

    def answer(state: str, scheduled: bool) -> dict:
        st = warm.state()
        return {"state": state, "scheduled": scheduled,
                "seconds": st["seconds"], "steps": st["steps"],
                "error": st["error"]}

    if warm.disabled():
        return answer("skipped", False)
    current = warm.state()["state"]
    if current in ("warming", "ready"):
        return answer(current, False)
    if _busy():
        # Воркер один и задачи сериализованы: позади реальной конвертации
        # прогрев бесполезен (она сама всё загрузит), а впереди его не
        # поставить. Пусть приложение дёрнет маршрут ещё раз позже.
        return answer("busy", False)
    _EXECUTOR.submit(warm.warmup)
    response.status_code = 202
    return answer(warm.state()["state"], True)


@router.post("/analyze")
async def analyze_pdf(file: UploadFile = File(...)):
    """
    Быстрое определение типа PDF (скан/нативный) для подсветки рекомендованного
    режима в интерфейсе. Синхронно, без OCR и без фоновых задач — только чтение
    текстового слоя. Временный файл удаляется сразу после анализа.
    """
    from .errors import ConvertError, check_pdf
    name = Path(file.filename or "document.pdf").name
    if not name.lower().endswith(".pdf"):
        raise _http_error(ConvertError("NOT_A_PDF", f"расширение файла: {name}"))

    tmp_dir = Path(tempfile.mkdtemp(prefix="analyze_", dir=_WORK_DIR))
    pdf_path = tmp_dir / name
    try:
        with pdf_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        # Анализ идёт при ВЫБОРЕ файла, то есть это самый ранний момент, когда
        # можно сказать про пароль или повреждение — до нажатия «Конвертировать».
        try:
            check_pdf(pdf_path)
        except ConvertError as err:
            raise _http_error(err) from err
        from .pdf_classifier import classify_pdf
        return classify_pdf(pdf_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/scan")
async def convert_scan(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    no_highlight: bool = Form(True),
    word_order: bool = Form(False),
    iim: bool = Form(True),
    ink_bold: bool = Form(False),
    ocr_preprocess: bool = Form(False),
):
    """Скан-режим (Docling + RapidOCR + LLM). Возвращает job_id для опроса статуса."""
    job_id, pdf_path, docx_path = _accept_upload(file)
    # Проверка текстового слоя стоит доли секунды (чтение PyMuPDF) и делается
    # до постановки в очередь, чтобы предупреждение было видно сразу.
    try:
        from .pdf_classifier import scan_mode_warning
        warning = scan_mode_warning(pdf_path)
    except Exception as exc:                         # noqa: BLE001
        log.debug("scan: не удалось проверить текстовый слой: %s", exc)
        warning = None
    # Пункт 3.4: не падать с MemoryError на слабой машине, а предупредить до
    # начала работы. Отказывать не будем — пользователь вправе попробовать.
    try:
        from .profiles import memory_warning, resolve
        mem = memory_warning(resolve(), _page_count(pdf_path))
    except Exception as exc:                         # noqa: BLE001
        log.debug("scan: не удалось оценить память: %s", exc)
        mem = None
    warning = " ".join(w for w in (warning, mem) if w) or None
    if warning:
        log.info("scan: %s (%s)", warning, pdf_path.name)
    flags = {
        "highlight": not no_highlight,   # фронтовый --no-highlight инвертирует подсветку
        "word_order": word_order,
        "llm": iim,                      # «iim» в интерфейсе = LLM-доочистка
        "ink_bold": ink_bold,
        "ocr_preprocess": ocr_preprocess,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "queued", "stage": "В очереди…", "progress": 0.0,
                         "mode": "scan", "filename": docx_path.name,
                         "docx_path": str(docx_path), "error": None,
                         "warning": warning}
    _EXECUTOR.submit(_run_scan, job_id, pdf_path, docx_path, flags)
    return {"job_id": job_id, "warning": warning}


@router.post("/native")
async def convert_native(file: UploadFile = File(...)):
    """Нативный PDF (pdf2docx, без флагов). Возвращает job_id для опроса статуса."""
    job_id, pdf_path, docx_path = _accept_upload(file)
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "queued", "stage": "В очереди…", "progress": 0.0,
                         "mode": "native", "filename": docx_path.name,
                         "docx_path": str(docx_path), "error": None}
    _EXECUTOR.submit(_run_native, job_id, pdf_path, docx_path)
    return {"job_id": job_id}


@router.get("/status/{job_id}")
async def convert_status(job_id: str):
    """Текущий статус задачи: queued | running | done | error (+ стадия и прогресс)."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Задача не найдена.")
        return {"job_id": job_id, "status": job["status"], "stage": job["stage"],
                "progress": job["progress"], "filename": job["filename"],
                "error": job["error"],
                # Пункт 4.2: машинный код и что делать — чтобы интерфейс не
                # разбирал русский текст и не показывал «Failed to fetch».
                "error_code": job.get("error_code"),
                "error_hint": job.get("error_hint"),
                "warning": job.get("warning"),
                # Для кнопки «Доочистить текст» (п.3.2): есть ли смысл её
                # показывать и не нажата ли она уже.
                "mode": job.get("mode"), "polished": bool(job.get("polished")),
                "polish_fixed": job.get("polish_fixed")}


def _run_polish(job_id: str, docx_path: Path) -> None:
    """LLM-доочистка уже готового DOCX (пункт 3.2).

    Распознавание и разметка не повторяются: подсветка сохранена в самом файле,
    а `correct_highlighted` работает именно по подсвеченным прогонам. Поэтому
    шаг стоит только времени LLM, без OCR и без моделей Docling.
    """
    try:
        _set(job_id, status="running", stage="Доочистка текста…", progress=0.3)
        from docx import Document
        doc = Document(str(docx_path))
        from .llm_correct import correct_highlighted
        fixed = correct_highlighted(doc, None)
        doc.save(str(docx_path))
        _set(job_id, status="done", stage=f"Доочистка: исправлено {fixed}",
             progress=1.0, polished=True, polish_fixed=fixed)
        log.info("polish: задача %s, исправлено %d фрагментов", job_id, fixed)
    except Exception as exc:                         # noqa: BLE001
        log.exception("api: ошибка доочистки %s", job_id)
        from .errors import ConvertError, classify
        err = classify(exc)
        if err.code == "INTERNAL":
            # Доочистка целиком про LLM: что бы ни сломалось внутри неё,
            # сказать надо именно это, а не «внутренняя ошибка конвертера».
            err = ConvertError("LLM_UNAVAILABLE", err.detail)
        # Файл остаётся прежним и его по-прежнему можно скачать, поэтому
        # задача снова `done`, а не `error`: сломалась доочистка, не результат.
        _set(job_id, status="done", stage="Доочистка не удалась", progress=1.0,
             error=err.text(), error_code=err.code, error_hint=err.hint,
             error_detail=err.detail or None)


@router.post("/polish/{job_id}")
async def convert_polish(job_id: str):
    """Догнать готовый DOCX LLM-доочисткой по кнопке (пункт 3.2).

    Смысл в том, что модель 1.5B стоит ~1.6 ГБ пика и заметного времени. На
    слабой машине её не включают заранее (профиль `lean`/`balanced`), поэтому
    пользователь сначала получает документ, смотрит жёлтые подсветки и сам
    решает, платить ли за доочистку.

    Идёт в ту же однопоточную очередь, что и конвертации. Прогресс и результат —
    через обычный `GET /convert/status/{job_id}`.
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Задача не найдена.")
        if job["status"] != "done":
            raise HTTPException(status_code=409,
                                detail="Доочистка возможна только после "
                                       "завершения конвертации.")
        if job.get("mode") != "scan":
            raise HTTPException(status_code=400,
                                detail="Доочистка есть только для режима "
                                       "распознавания скана.")
        if job.get("polished"):
            raise HTTPException(status_code=409,
                                detail="Этот документ уже доочищен.")
        docx_path = Path(job["docx_path"])
        job.update(status="queued", stage="Доочистка в очереди…", progress=0.0,
                   error=None)
    if not docx_path.exists():
        raise HTTPException(status_code=410, detail="Результат недоступен.")
    _EXECUTOR.submit(_run_polish, job_id, docx_path)
    return {"job_id": job_id, "status": "queued"}


@router.get("/download/{job_id}")
async def convert_download(job_id: str):
    """Отдаёт готовый DOCX. Доступно только после status == done."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="Конвертация ещё не завершена.")
    docx_path = Path(job["docx_path"])
    if not docx_path.exists():
        raise HTTPException(status_code=410, detail="Результат недоступен.")
    return FileResponse(
        docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=job["filename"],
    )
