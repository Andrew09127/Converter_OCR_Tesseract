"""
errors.py
─────────
Типизированные ошибки конвертации (пункт 4.2).

ЗАЧЕМ. `convert_pdf` возвращал `bool`, и причина терялась: интерфейс показывал
«Конвертация не удалась» одинаково и на запароленном PDF, и на кончившемся
диске, и на не собравшихся моделях. Пользователь в офлайн-приложении не может
посмотреть лог сервера — значит, причина и действие должны доезжать до него.

ЧТО ЗДЕСЬ:
  • `ConvertError(code, message, hint)` — одна ошибка с машинным кодом, русским
    текстом «что случилось» и русским текстом «что делать»;
  • `check_pdf(path)` — дешёвые детерминированные проверки ДО постановки в
    очередь: не-PDF, пустой, запароленный, битый, не-ASCII путь;
  • `classify(exc)` — разбор произвольного исключения, вылетевшего из пайплайна,
    в один из кодов. Что не опознано — `INTERNAL` с исходным текстом.

Коды намеренно стабильны: по ним интерфейс (в том числе чужой, в sberAct)
решает, что показать, не разбирая русский текст.
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# code → (что случилось, что делать)
MESSAGES: dict[str, tuple[str, str]] = {
    "NOT_A_PDF": (
        "Файл не является PDF-документом.",
        "Проверьте, что выбран именно PDF: у файла может быть расширение .pdf, "
        "но внутри оказаться картинка или архив."),
    "PDF_ENCRYPTED": (
        "PDF защищён паролем.",
        "Снимите пароль в любой программе для работы с PDF и загрузите файл "
        "заново — конвертер не подбирает пароли."),
    "PDF_CORRUPTED": (
        "PDF повреждён и не читается.",
        "Попробуйте открыть файл в просмотрщике и пересохранить. Если файл "
        "скачан — скачайте ещё раз, он мог докачаться не полностью."),
    "TESSERACT_MISSING": (
        "Не найден Tesseract — без него распознавание сканов невозможно.",
        "Проверьте папку vendor/tesseract рядом с программой. Если её нет, "
        "переустановите приложение из офлайн-комплекта."),
    "MODELS_BROKEN_PARTS": (
        "Модели распознавания не готовы: файлы в models/ отсутствуют или "
        "собраны не полностью.",
        "Переустановите приложение из офлайн-комплекта. Крупные модели лежат "
        "кусками *.partNNN и собираются при первом запуске — куски должны быть "
        "скопированы все."),
    "OUT_OF_MEMORY": (
        "Не хватило оперативной памяти.",
        "Закройте другие программы и попробуйте снова. Для слабой машины "
        "задайте профиль CONVERTER_PROFILE=lean или разделите документ на "
        "части по 10-15 страниц."),
    "DISK_FULL": (
        "На диске закончилось место.",
        "Освободите место на системном диске и повторите конвертацию."),
    "PATH_NON_ASCII": (
        "Путь к временной папке содержит символы, которые не понимает "
        "Tesseract.",
        "Задайте переменную окружения TEMP на папку с латинским именем, "
        "например C:\\temp, и перезапустите программу."),
    "LLM_UNAVAILABLE": (
        "Доочистка текста недоступна: модель не загрузилась.",
        "Документ уже готов и скачивается — доочистка необязательна. Модель "
        "лежит в models/ кусками *.partNNN, проверьте, что скопированы все."),
    "INTERRUPTED": (
        "Конвертация прервана: программа была перезапущена.",
        "Загрузите файл и запустите конвертацию заново — незавершённая работа "
        "не продолжается после перезапуска."),
    "INVALID_SETTINGS": (
        "Такой настройки нет.",
        "Выберите профиль из списка: auto, lean, balanced или full."),
    "CANCELLED": (
        "Конвертация отменена.",
        "Файл не изменён. Запустите снова, когда будет удобно."),
    "TIMEOUT": (
        "Конвертация не уложилась в отведённое время.",
        "Попробуйте разделить документ на части по 10-15 страниц."),
    "INTERNAL": (
        "Внутренняя ошибка конвертера.",
        "Повторите попытку. Если ошибка повторяется, передайте разработчику "
        "текст ниже и имя файла."),
}


class ConvertError(Exception):
    """Ошибка конвертации с машинным кодом и человеческим объяснением."""

    def __init__(self, code: str, detail: str = "", *,
                 message: str = "", hint: str = "") -> None:
        base_msg, base_hint = MESSAGES.get(code, MESSAGES["INTERNAL"])
        self.code = code
        self.message = message or base_msg
        self.hint = hint or base_hint
        # Технический текст исходного исключения: пользователю не показываем
        # как главное сообщение, но в ответе и в логе он должен быть.
        self.detail = detail
        super().__init__(f"[{code}] {self.message}")

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "hint": self.hint, "detail": self.detail or None}

    def text(self) -> str:
        """Одной строкой — для поля `error`, которое интерфейс уже показывает."""
        return " ".join(p for p in (self.message, self.hint) if p)


class JobAborted(Exception):
    """Задачу надо прекратить: отмена пользователем или истёкший срок (п.4.6).

    Поток с конвертацией убить нельзя — Docling работает одним блокирующим
    вызовом. Поэтому прерывание кооперативное: проверка стоит на границе
    страницы (там, где считается прогресс), и отсюда летит это исключение.
    Наследуется от Exception, а не от ConvertError: по пути наружу его ловит и
    переводит в код только `convert_pdf_checked`, а промежуточные `except
    Exception` в Docling его просто пропускают дальше.
    """

    def __init__(self, code: str = "CANCELLED", detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else f"[{code}]")


def _looks_like(text: str, *needles: str) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


def check_pdf(pdf_path: Path) -> None:
    """Дешёвые проверки файла ДО постановки задачи в очередь.

    Стоят миллисекунды и дают точный код вместо часового прогона, который
    упадёт в середине. Что не ловится здесь (нет Tesseract, не собрались
    модели, кончилась память) — ловится уже по ходу через `classify`.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise ConvertError("NOT_A_PDF", f"файл не найден: {pdf_path.name}",
                           message="Файл не найден.")
    size = pdf_path.stat().st_size
    if size == 0:
        raise ConvertError("NOT_A_PDF", "файл пустой (0 байт)",
                           message="Файл пустой (0 байт).")
    # Заголовок %PDF может быть не в самом начале (мусорная преамбула), но в
    # первом килобайте он есть у любого читаемого PDF.
    with pdf_path.open("rb") as f:
        head = f.read(1024)
    if b"%PDF" not in head:
        raise ConvertError("NOT_A_PDF", f"нет заголовка %PDF: {head[:8]!r}")

    try:
        import fitz
    except Exception as exc:                         # noqa: BLE001
        log.debug("check_pdf: PyMuPDF недоступен (%s) — проверки пропущены", exc)
        return
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:                         # noqa: BLE001
        raise ConvertError("PDF_CORRUPTED", f"{type(exc).__name__}: {exc}") from exc
    try:
        # needs_pass: файл зашифрован и пароль не подошёл (пустой). Документы с
        # одними только ограничениями на печать открываются и конвертируются.
        if getattr(doc, "needs_pass", 0):
            raise ConvertError("PDF_ENCRYPTED", "needs_pass=1")
        if doc.page_count == 0:
            raise ConvertError("PDF_CORRUPTED", "в документе 0 страниц")
    finally:
        doc.close()


def check_temp_dir() -> None:
    """Временная папка должна быть пригодна для Tesseract.

    ВАЖНО, ЧТО ИМЕННО ПРОВЕРЯЕТСЯ. Tesseract НЕ видит ни загруженный PDF, ни
    результат: Docling сохраняет картинку каждой страницы во временный файл
    (`tempfile.NamedTemporaryFile` в `tesseract_ocr_cli_model`) и передаёт
    Tesseract только его. Значит, имя исходного файла может быть каким угодно,
    а вот системный TEMP обязан быть доступен по ASCII-пути: Windows-сборка
    Tesseract (mingw) читает не-ASCII пути как ANSI-байты и просто не находит
    файл. Обходной путь — короткие имена 8.3, но на томах с отключённой их
    генерацией его нет, и распознавание даёт пустые страницы без объяснения.
    """
    import tempfile
    from .pipeline import _ascii_safe_path
    tmp = tempfile.gettempdir()
    if tmp.isascii():
        return
    if _ascii_safe_path(tmp) is None:
        raise ConvertError("PATH_NON_ASCII", f"TEMP: {tmp}")


def classify(exc: BaseException) -> ConvertError:
    """Превратить исключение пайплайна в типизированную ошибку.

    Правила — по тексту и типу исключения: внутренности Docling, Tesseract и
    torch нам не принадлежат, стабильных классов исключений они не дают.
    """
    if isinstance(exc, ConvertError):
        return exc
    if isinstance(exc, JobAborted):
        return ConvertError(exc.code, exc.detail)

    text = f"{type(exc).__name__}: {exc}"

    if isinstance(exc, MemoryError) or _looks_like(
            text, "unable to allocate", "bad allocation", "out of memory",
            "cannot allocate memory", "not enough memory", "недостаточно памяти"):
        return ConvertError("OUT_OF_MEMORY", text)

    if isinstance(exc, OSError):
        # errno 28 — POSIX ENOSPC; WinError 112 — «недостаточно места на диске»
        if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
            return ConvertError("DISK_FULL", text)
    if _looks_like(text, "no space left", "недостаточно места на диске"):
        return ConvertError("DISK_FULL", text)

    if _looks_like(text, "tesseract"):
        if _looks_like(text, "not found", "no such file", "winerror 2",
                       "not installed", "is not available", "не найден"):
            return ConvertError("TESSERACT_MISSING", text)

    if _looks_like(text, "safetensors", "huggingface", "hf_hub", "artifacts_path",
                   "headertoolarge", "model file", "layout_predictor",
                   "tableformer", "max retries", "connectionerror",
                   "failed to resolve"):
        return ConvertError("MODELS_BROKEN_PARTS", text)

    if _looks_like(text, "llama", ".gguf"):
        return ConvertError("LLM_UNAVAILABLE", text)

    if isinstance(exc, TimeoutError) or _looks_like(text, "timed out", "timeout"):
        return ConvertError("TIMEOUT", text)

    if os.name == "nt" and _looks_like(text, "ascii", "codec can't encode"):
        return ConvertError("PATH_NON_ASCII", text)

    return ConvertError("INTERNAL", text)
