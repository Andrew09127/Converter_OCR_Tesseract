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

import json
import logging
import os
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

# job_key → job_id. Ключ идемпотентности: сетевой сбой на POST не говорит
# клиенту, дошёл ли запрос, и честный повтор без ключа создал бы ВТОРУЮ задачу
# на тот же файл — двойное время и двойная память. С ключом повтор возвращает
# ту же задачу. Восстанавливается с диска вместе с задачами (п.4.5).
_JOB_KEYS: dict = {}

# job_id → Future. Нужен для отмены (п.4.6): задачу, которая ещё не начиналась,
# снимает сам исполнитель, и это единственный надёжный способ её не делать.
_FUTURES: dict = {}

# Срок на задачу: защита от зависания, а не ограничение по скорости. Файлы у
# нас до 20 страниц (~10 s на страницу в худшем случае), поэтому полчаса —
# это заведомо больше любой нормальной работы. 0 — без срока.
_DEFAULT_TIMEOUT_S = 1800.0


def _job_timeout_s() -> float:
    """Срок на задачу, CONVERTER_JOB_TIMEOUT_S в секундах (0 — выключить)."""
    raw = os.environ.get("CONVERTER_JOB_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        return max(float(raw), 0.0)
    except ValueError:
        log.warning("api: CONVERTER_JOB_TIMEOUT_S=%r — не число, беру %s s",
                    raw, _DEFAULT_TIMEOUT_S)
        return _DEFAULT_TIMEOUT_S

# Рабочая папка задач (вход PDF + выход DOCX) в системном temp. НЕ чистится:
# рядом с каждым PDF/DOCX лежит `job.json`, по которому задачи восстанавливаются
# после перезапуска сервера (п.4.5).
_WORK_DIR = Path(tempfile.gettempdir()) / "convert_api_jobs"
_WORK_DIR.mkdir(parents=True, exist_ok=True)

# Кэш Docling-конвертера: построение тяжёлое (1.8 s и ~230 МБ), поэтому
# держим готовый экземпляр. Слот РОВНО ОДИН: и флаг ocr_preprocess, и
# ресурсный профиль меняют pipeline, но кэшировать их комбинации нельзя —
# каждый экземпляр держит свой набор моделей, то есть вторая запись в кэше
# это +230 МБ впустую. Конвертации сериализованы одним воркером, так что
# пересборка при смене настроек стоит 1.8 s один раз.
_SCAN_CACHE: dict = {"key": None, "conv": None}
_SCAN_LOCK = threading.Lock()

# Кэш ответа /convert/health: проверка дешёвая (~0.1 s), но health-цикл
# обёртки опрашивает её в цикле, а внутри — два запуска tesseract.exe.
_HEALTH_CACHE: dict = {"at": 0.0, "report": None}
_HEALTH_TTL = 10.0

# Кэш OCR-reader (Tesseract): нужен только для флага word_order
# (переупорядочивание блоков). Строим лениво один раз.
_OCR_READER: list = []          # [reader] или [] ; None-маркер недоступности — [None]
_OCR_READER_LOCK = threading.Lock()

# Готовность движка. Импорт docling с torch занимает 15-40 с, и раньше он
# приходился на первую конвертацию — уже после того, как /health сказал «готов».
_ENGINE_READY = threading.Event()


# Задачи восстанавливаются лениво, при первом обращении к статусу или файлу.
# Событие старта не годится: в sberAct подключают только наш роутер, своё
# приложение они строят сами, и lifespan у нас не вызывается.
_RESTORED = False
_RESTORE_LOCK = threading.Lock()


def _job_file(job_id: str) -> Path:
    return _WORK_DIR / job_id / "job.json"


def _write_job(job_id: str, snapshot: dict) -> None:
    """Сохранить снимок задачи рядом с её файлами (п.4.5).

    Пишем через временный файл и `os.replace`: иначе выключение в момент записи
    оставит обрезанный JSON, и задача пропадёт вместе с готовым DOCX. Ошибки
    записи не роняют конвертацию — она важнее возможности узнать статус потом.
    """
    path = _job_file(job_id)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("api: не удалось сохранить задачу %s: %s", job_id, exc)


def _set(job_id: str, **changes) -> None:
    """Атомарно обновить запись задачи и сохранить её на диск."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(changes)
        snapshot = dict(job)
    # Файл пишем ВНЕ замка: `_JOBS_LOCK` не реентрантный, да и держать его на
    # время записи незачем.
    _write_job(job_id, snapshot)


def _reuse_by_key(job_key: str | None) -> dict | None:
    """Задача, уже созданная по этому ключу, если её ещё имеет смысл отдавать.

    Сорвавшуюся задачу НЕ переиспользуем: повтор после ошибки — это осознанная
    вторая попытка, а не дубликат запроса.
    """
    if not job_key:
        return None
    _ensure_restored()
    with _JOBS_LOCK:
        job_id = _JOB_KEYS.get(job_key)
        job = _JOBS.get(job_id) if job_id else None
        if job is None or job["status"] == "error":
            return None
        log.info("api: повтор по ключу %s — отдаю задачу %s", job_key, job_id)
        return {"job_id": job_id, "warning": job.get("warning"), "reused": True}


def _restore_jobs() -> int:
    """Поднять задачи прошлых запусков из `job.json`. Возвращает их число.

    `queued` и `running` продолжить нельзя: очередь живёт в памяти процесса, и
    после перезапуска никто эту работу не делает. Такие задачи помечаются
    ошибкой с кодом `INTERRUPTED` — иначе интерфейс ждал бы вечно.
    """
    from .errors import ConvertError
    restored = 0
    for job_dir in sorted(_WORK_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        path = job_dir / "job.json"
        if not path.is_file():
            continue                      # папка анализа или мусор — не задача
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("api: задача %s не читается: %s", job_dir.name, exc)
            continue
        if not isinstance(job, dict) or "status" not in job:
            continue
        job_id = job_dir.name
        if job["status"] in ("queued", "running"):
            err = ConvertError("INTERRUPTED",
                               f"статус на момент перезапуска: {job['status']}")
            job.update(status="error", stage="Прервано перезапуском",
                       error=err.text(), error_code=err.code,
                       error_hint=err.hint, error_detail=err.detail)
        with _JOBS_LOCK:
            if job_id in _JOBS:
                continue                  # задача этого запуска — она свежее
            _JOBS[job_id] = job
            if job.get("job_key"):
                _JOB_KEYS.setdefault(job["job_key"], job_id)
        restored += 1
        if job["stage"] == "Прервано перезапуском":
            _write_job(job_id, job)
    return restored


def _ensure_restored() -> None:
    """Один раз на процесс поднять задачи с диска (п.4.5)."""
    global _RESTORED
    if _RESTORED:
        return
    with _RESTORE_LOCK:
        if _RESTORED:
            return
        try:
            count = _restore_jobs()
            if count:
                log.info("api: восстановлено задач с диска: %d", count)
        except Exception as exc:                 # noqa: BLE001
            log.warning("api: не удалось восстановить задачи: %s", exc)
        _RESTORED = True


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


def _get_scan_converter(ocr_preprocess: bool, profile: str | None = None):
    """Лениво строит и кэширует Docling-конвертер под флаг предобработки и
    ресурсный профиль. При смене любого из них пересобирает (см. `_SCAN_CACHE`)."""
    from .profiles import resolve
    key = (bool(ocr_preprocess), resolve(profile).name)
    with _SCAN_LOCK:
        if _SCAN_CACHE["key"] != key or _SCAN_CACHE["conv"] is None:
            from . import build_converter
            log.info("api: строю Docling-конвертер (ocr_preprocess=%s, профиль %s)",
                     key[0], key[1])
            _SCAN_CACHE["conv"] = None      # старый отпускаем ДО построения нового
            import gc
            gc.collect()
            _SCAN_CACHE.update(key=key,
                               conv=build_converter(ocr_preprocess=key[0],
                                                    profile=key[1]))
        # Движок построен: на этот флаг смотрят `main.py` и `/health`.
        if not key[0]:
            _ENGINE_READY.set()
        return _SCAN_CACHE["conv"]


def engine_ready() -> bool:
    """Загружен ли docling. False — первая конвертация ещё подождёт импорт."""
    return _ENGINE_READY.is_set()


def start_warmup() -> None:
    """Греет движок в фоне, не задерживая ответ сервера.

    Зовётся из `main.py` при `CONVERTER_WARMUP=1`. Внутри — тот же прогрев, что
    и у `POST /convert/warmup` (`docling_dev/warmup.py`): он идемпотентен,
    поэтому прогрев при старте и прогрев по запросу не делают работу дважды.
    Вариант без предобработки: им идёт обычная конвертация скана, а
    ocr_preprocess=True включают редко и вручную.
    """
    from . import warmup as warm

    threading.Thread(target=warm.warmup, name="warmup", daemon=True).start()


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
    """Прогон скана под записью лога задачи (пункт 4.7).

    Лог пишется рядом с файлами задачи: на машине пользователя общий лог
    сервера недоступен (в sberAct stdout сайдкара уходит в console.log
    Electron), а разбирать сбои как-то надо.
    """
    from . import joblog
    with joblog.capture(pdf_path.parent, job_id):
        _run_scan_body(job_id, pdf_path, docx_path, flags)


def _run_scan_body(job_id: str, pdf_path: Path, docx_path: Path,
                   flags: dict) -> None:
    """Собственно прогон скана через Docling-пайплайн (медленный путь)."""
    from . import abort, progress as page_progress
    from .errors import ConvertError, classify
    aborted = None
    try:
        _set(job_id, status="running", stage="Анализ скана…", progress=0.1)
        converter = _get_scan_converter(flags["ocr_preprocess"],
                                        flags.get("profile"))

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
        # Пункт 4.6: отмена и срок. Проверки стоят на входе в каждую тяжёлую
        # стадию конвейера — иначе прерывание не экономит времени (замерено).
        abort.start(_job_timeout_s())
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
            # Обязательно: иначе следующая задача досчитает страницы в эту,
            # а запрос на отмену достанется ей в наследство.
            page_progress.finish()
            aborted = abort.requested()
            abort.finish()
        if docx_path.exists():
            _set(job_id, status="done", stage="Готово", progress=1.0)
        else:
            # Исключения не было, а файла нет — так быть не должно.
            _fail(job_id, ConvertError("INTERNAL",
                                       "DOCX не создан, хотя ошибки не было"))
    except Exception as exc:                     # noqa: BLE001 — отдаём текст клиенту
        # Если прерывание просили мы сами, причина известна точно: разбирать
        # текст ошибки Docling (он завершает прогон как FAILURE) незачем.
        if aborted:
            log.info("api: задача %s прекращена (%s)", job_id, aborted)
            _fail(job_id, ConvertError(aborted))
            _set(job_id, stage=("Отменено" if aborted == "CANCELLED"
                                else "Превышен срок"))
        else:
            log.exception("api: ошибка прогона скана %s", job_id)
            _fail(job_id, classify(exc))


def _run_native(job_id: str, pdf_path: Path, docx_path: Path) -> None:
    """Прогон нативного PDF под записью лога задачи (пункт 4.7)."""
    from . import joblog
    with joblog.capture(pdf_path.parent, job_id):
        _run_native_body(job_id, pdf_path, docx_path)


def _run_native_body(job_id: str, pdf_path: Path, docx_path: Path) -> None:
    """Собственно прогон нативного PDF через pdf2docx (без флагов)."""
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
    from . import settings
    from .profiles import machine, resolve
    prof = resolve()
    m = machine()
    return {
        "profile": prof.name,
        "note": prof.note,
        "ocr_workers": prof.ocr_workers,
        "num_threads": prof.num_threads,
        # Выбор пользователя старше профиля машины (диалог настроек).
        "llm_default": settings.llm_default(prof.llm_default),
        "source": settings.profile_source(),
        "cores": m["cores"],
        "ram_gb": round(m["ram_gb"], 1),
        "free_gb": round(m["free_gb"], 1),
    }


def _settings_view() -> dict:
    """Что показать в диалоге настроек: определённое по машине, выбранное
    пользователем и то, что реально применится."""
    from . import settings
    from .profiles import PROFILES, detect, machine, resolve
    auto = detect()
    eff = resolve()
    saved = settings.load()
    m = machine()
    return {
        "auto": {"profile": auto.name, "note": auto.note},
        "override": {"profile": saved.get("profile"),
                     "llm_default": saved.get("llm_default")},
        "effective": {"profile": eff.name, "note": eff.note,
                      "ocr_workers": eff.ocr_workers,
                      "num_threads": eff.num_threads,
                      "images_scale": eff.images_scale,
                      "llm_default": settings.llm_default(eff.llm_default)},
        "machine": {"cores": m["cores"], "ram_gb": round(m["ram_gb"], 1),
                    "free_gb": round(m["free_gb"], 1)},
        "choices": ["auto", *PROFILES],
        "source": settings.profile_source(),
        # Окружением гоняются замеры, и сохранённый выбор его не перебивает:
        # интерфейс должен не молча игнорировать выбор, а объяснить причину.
        "locked_by_env": settings.env_profile() is not None,
        "path": str(settings.path()),
    }


@router.get("/settings")
async def convert_settings_get():
    """Настройки, которые можно менять из интерфейса: профиль и дефолт LLM.

    Отдаёт сразу три вещи — что определило железо (`auto`), что выбрал
    пользователь (`override`) и что реально применится (`effective`), — чтобы
    диалогу настроек не приходилось повторять нашу логику старшинства.
    """
    return _settings_view()


@router.post("/settings")
async def convert_settings_post(
    profile: str | None = Form(None),
    llm_default: bool | None = Form(None),
    clear_llm_default: bool = Form(False),
):
    """Сохранить выбор пользователя. POST, а не PUT: прокси приложения
    пропускает только GET и POST.

    `profile` = auto | lean | balanced | full («auto» — решает машина);
    `llm_default` — предлагать ли доочистку; `clear_llm_default=true`
    возвращает значение «как решит профиль».
    """
    from . import settings
    from .errors import ConvertError
    changes: dict = {}
    if profile is not None:
        name = profile.strip().lower()
        if name not in settings.PROFILE_CHOICES:
            raise _http_error(ConvertError(
                "INVALID_SETTINGS",
                f"профиль {profile!r}; допустимы: "
                f"{', '.join(settings.PROFILE_CHOICES)}"))
        changes["profile"] = None if name == "auto" else name
    if clear_llm_default:
        changes["llm_default"] = None
    elif llm_default is not None:
        changes["llm_default"] = bool(llm_default)
    if changes:
        settings.save(**changes)
        log.info("api: настройки изменены: %s", changes)
    return _settings_view()


@router.get("/health")
async def convert_health(refresh: bool = False):
    """Готовность окружения одним ответом (пункт 4.3).

    Что проверяется: tesseract и языки rus/eng/osd, собранные модели Docling,
    gguf-модель LLM, словарь морфологии, свободные память и диск. Плюс
    выбранный ресурсный профиль и состояние прогрева.

    ОТВЕТ ВСЕГДА 200, даже когда `ready: false`. Код 503 выглядел бы
    естественнее, но health-цикл обёртки в sberAct по нему решит, что сайдкар
    не поднялся, и убьёт его — а нам надо наоборот: ответить и объяснить, чего
    не хватает. Критичность каждой строки — в поле `critical`.

    Результат кэшируется на 10 секунд (`refresh=true` — посчитать заново):
    внутри два запуска tesseract.exe, а опрашивают этот маршрут в цикле.
    """
    import time
    from .health import check_environment
    now = time.monotonic()
    report = _HEALTH_CACHE["report"]
    if refresh or report is None or now - _HEALTH_CACHE["at"] > _HEALTH_TTL:
        # deep=False обязательно: иначе проверка импортирует llama-cpp и может
        # СКЛЕИТЬ gguf-модель из кусков — ответ на запрос такого делать не должен.
        report = check_environment(deep=False)
        _HEALTH_CACHE.update(at=now, report=report)
        if not report["ready"]:
            log.warning("health: окружение не готово — %s",
                        ", ".join(report["problems"]))
    return report


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
    profile: str | None = Form(None),
    job_key: str | None = Form(None),
):
    """Скан-режим (Docling + Tesseract + LLM). Возвращает job_id для опроса статуса.

    `profile` (auto|lean|balanced|full) — разовое переопределение ресурсного
    профиля; без него действует настройка пользователя или выбор по машине.
    `job_key` — ключ идемпотентности: повтор запроса с тем же ключом вернёт ту
    же задачу вместо создания второй (нужен фронту, чтобы безопасно ретраить
    POST после сетевого сбоя).
    """
    from . import settings
    from .errors import ConvertError
    if profile is not None:
        name = profile.strip().lower()
        if name not in settings.PROFILE_CHOICES:
            raise _http_error(ConvertError(
                "INVALID_SETTINGS",
                f"профиль {profile!r}; допустимы: "
                f"{', '.join(settings.PROFILE_CHOICES)}"))
        profile = name
    reused = _reuse_by_key(job_key)
    if reused is not None:
        return reused
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
        "profile": profile,
    }
    job = {"status": "queued", "stage": "В очереди…", "progress": 0.0,
           "mode": "scan", "filename": docx_path.name,
           "docx_path": str(docx_path), "error": None, "warning": warning,
           "job_key": job_key}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        if job_key:
            _JOB_KEYS[job_key] = job_id
    _write_job(job_id, dict(job))
    _FUTURES[job_id] = _EXECUTOR.submit(_run_scan, job_id, pdf_path,
                                        docx_path, flags)
    return {"job_id": job_id, "warning": warning, "reused": False}


@router.post("/native")
async def convert_native(file: UploadFile = File(...),
                         job_key: str | None = Form(None)):
    """Нативный PDF (pdf2docx, без флагов). Возвращает job_id для опроса статуса.

    `job_key` — тот же ключ идемпотентности, что и у скана.
    """
    reused = _reuse_by_key(job_key)
    if reused is not None:
        return reused
    job_id, pdf_path, docx_path = _accept_upload(file)
    job = {"status": "queued", "stage": "В очереди…", "progress": 0.0,
           "mode": "native", "filename": docx_path.name,
           "docx_path": str(docx_path), "error": None, "job_key": job_key}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        if job_key:
            _JOB_KEYS[job_key] = job_id
    _write_job(job_id, dict(job))
    _FUTURES[job_id] = _EXECUTOR.submit(_run_native, job_id, pdf_path, docx_path)
    return {"job_id": job_id, "reused": False}


@router.post("/cancel/{job_id}")
async def convert_cancel(job_id: str, response: Response):
    """Отменить задачу (пункт 4.6).

    Два случая, и они ведут себя по-разному:
      • задача ещё НЕ НАЧИНАЛАСЬ — снимается с исполнителя, работы не будет
        вовсе, статус сразу `error` с кодом `CANCELLED`;
      • задача УЖЕ ИДЁТ — поток убить нельзя, поэтому прерывание кооперативное:
        проверки стоят на входе в каждую тяжёлую стадию конвейера. Отвечаем 202,
        итог приходит обычным `GET /convert/status`. Замер на 3-страничном
        скане: отмена на 3-й секунде — прогон закончился на 7.4 s вместо 22 s.

    Нативный режим (pdf2docx) и доочистка LLM прерываются ТОЛЬКО пока стоят в
    очереди: это внешний чёрный ящик и один вызов модели соответственно, точек
    проверки внутри них нет. На идущей такой задаче отвечаем 409 честно.
    """
    from . import abort
    from .errors import ConvertError
    _ensure_restored()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Задача не найдена.")
        if job["status"] in ("done", "error"):
            raise HTTPException(status_code=409,
                                detail="Задача уже завершена — отменять нечего.")
        mode = job.get("mode")
        stage = job.get("stage", "")

    future = _FUTURES.get(job_id)
    if future is not None and future.cancel():
        # Сняли из очереди: воркер её даже не начнёт, статус ставим здесь.
        _fail(job_id, ConvertError("CANCELLED", "снята из очереди до начала"))
        _set(job_id, stage="Отменено")
        log.info("api: задача %s снята из очереди", job_id)
        return {"job_id": job_id, "state": "cancelled"}

    polishing = stage.startswith("Доочистка")
    if mode != "scan" or polishing:
        raise HTTPException(
            status_code=409,
            detail=("Эту работу нельзя прервать на ходу: "
                    + ("доочистка — один вызов модели."
                       if polishing else
                       "нативный режим идёт внутри pdf2docx.")
                    + " Дождитесь завершения."))

    abort.request("CANCELLED", f"отмена задачи {job_id}")
    response.status_code = 202
    return {"job_id": job_id, "state": "cancelling"}


@router.get("/status/{job_id}")
async def convert_status(job_id: str):
    """Текущий статус задачи: queued | running | done | error (+ стадия и прогресс).

    Задача могла быть создана до перезапуска сервера — тогда она поднимается с
    диска (п.4.5): готовый DOCX лежит на месте, и терять его из-за 404 нельзя.
    """
    _ensure_restored()
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
    """Доочистка под записью лога задачи (пункт 4.7)."""
    from . import joblog
    with joblog.capture(docx_path.parent, job_id):
        _run_polish_body(job_id, docx_path)


def _run_polish_body(job_id: str, docx_path: Path) -> None:
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
    _ensure_restored()
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
    _FUTURES[job_id] = _EXECUTOR.submit(_run_polish, job_id, docx_path)
    return {"job_id": job_id, "status": "queued"}


@router.get("/log/{job_id}")
async def convert_log(job_id: str, tail: int = 64 * 1024):
    """Лог задачи (пункт 4.7): что именно происходило во время работы.

    Отдаётся ХВОСТ файла (по умолчанию последние 64 КБ) — интересно то, что
    было перед сбоем. Лог лежит рядом с файлами задачи, поэтому доступен и
    после перезапуска сервера.

    Это диагностика для разработчика, а не сообщение пользователю: что делать,
    говорят `error_code` и `error_hint` в статусе задачи.
    """
    from . import joblog
    _ensure_restored()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Задача не найдена.")
        docx_path = Path(job["docx_path"])
    text = joblog.read_tail(docx_path.parent, max(int(tail), 1024))
    if not text:
        raise HTTPException(status_code=404,
                            detail="Лог этой задачи недоступен: он мог быть "
                                   "отключён (CONVERTER_JOB_LOG_LEVEL=off) или "
                                   "удалён вместе с временными файлами.")
    return {"job_id": job_id, "text": text}


@router.get("/download/{job_id}")
async def convert_download(job_id: str):
    """Отдаёт готовый DOCX. Доступно только после status == done."""
    _ensure_restored()
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
