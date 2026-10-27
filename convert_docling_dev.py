"""
convert_docling_dev.py
Точка входа для Docling-конвертера (docling_dev/).

Использование:
  python convert_docling_dev.py                  # батч из pdf_to_convert/
  python convert_docling_dev.py file.pdf         # один файл
  python convert_docling_dev.py --no-word-order  # отключить автоопределение порядка

Папки по умолчанию:
  input:   ./pdf_to_convert/
  output:  ./converted_word_docling/
  backup:  ./pdf_backup/     (PDF перемещается сюда после конвертации)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

try:
    # reconfigure есть только у TextIOWrapper (не у абстрактного TextIO) —
    # type: ignore гасит статическую претензию, рантайм прикрыт try/except.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("docling_convert.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
# Подавляем DEBUG от внешних библиотек — только наш код
for _lib in ("docling", "httpx", "urllib3", "PIL", "torch", "pytesseract",
             "transformers", "httpcore", "hpack"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from docling_dev import DoclingBatchConverter, convert_pdf, build_converter


def _init_ocr_reader(langs: list[str]) -> object | None:
    """Инициализирует Tesseract reader для word_order. None если недоступен."""
    try:
        from docling_dev.word_order import TesseractWordReader
        logging.info("Инициализация Tesseract для word_order...")
        reader = TesseractWordReader(langs)
        logging.info("Tesseract готов.")
        return reader
    except Exception as e:
        logging.warning("Tesseract недоступен, word_order отключён: %s", e)
        return None


def _convert_single(pdf_path: Path, out_dir: Path, use_word_order: bool,
                    highlight: bool = True, llm: bool = False,
                    llm_model: str = "", ink_bold: bool = False,
                    ocr_preprocess: bool = True) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    docx_path = out_dir / f"{pdf_path.stem}.docx"
    logging.info("Один файл: %s", pdf_path.name)

    converter  = build_converter(ocr_preprocess=ocr_preprocess)
    ocr_reader = _init_ocr_reader(["ru", "en"]) if use_word_order else None

    ok = convert_pdf(pdf_path, docx_path, converter,
                     ocr_reader=ocr_reader, use_word_order=use_word_order,
                     highlight=highlight, llm=llm, llm_model=llm_model,
                     ink_bold=ink_bold)
    if ok:
        logging.info("Готово: %s", docx_path)
    else:
        logging.error("Ошибка конвертации: %s", pdf_path.name)
        sys.exit(1)


def _convert_batch(input_dir: Path, output_dir: Path, backup_dir: Path,
                   use_word_order: bool, highlight: bool = True,
                   llm: bool = False, llm_model: str = "",
                   ink_bold: bool = False, ocr_preprocess: bool = True) -> None:
    DoclingBatchConverter(
        input_folder=str(input_dir),
        output_folder=str(output_dir),
        backup_folder=str(backup_dir),
        use_word_order=use_word_order,
        highlight=highlight,
        llm=llm,
        llm_model=llm_model,
        ink_bold=ink_bold,
        ocr_preprocess=ocr_preprocess,
    ).process()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Конвертер отсканированных PDF → DOCX (Docling + Tesseract)"
    )
    parser.add_argument(
        "pdf", nargs="?", type=Path,
        help="Путь к одному PDF (если не указан — батч-режим из pdf_to_convert/)"
    )
    parser.add_argument(
        "--input", "-i", type=Path, default=Path("pdf_to_convert"),
        help="Папка с PDF (по умолч. ./pdf_to_convert/)"
    )
    parser.add_argument(
        "--output", "-o", type=Path, default=Path("converted_word_docling"),
        help="Папка для DOCX (по умолч. ./converted_word_docling/)"
    )
    parser.add_argument(
        "--backup", "-b", type=Path, default=Path("pdf_backup"),
        help="Куда перемещать PDF после конвертации (по умолч. ./pdf_backup/)"
    )
    parser.add_argument(
        "--word-order", action="store_true",
        help="Включить OCR-переупорядочивание блоков. По умолчанию ВЫКЛ: "
             "нативный порядок Docling точнее и не ломает вёрстку (штамп ЭП, "
             "реквизиты, колоночные блоки)."
    )
    parser.add_argument(
        "--no-highlight", action="store_true",
        help="Не подсвечивать жёлтым подозрительные (вероятно искажённые OCR) слова"
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Отключить локальную LLM-доочистку. По умолчанию ВКЛ: llama-cpp-python "
             "+ .gguf-модель добивает оставшиеся подсвеченные слова. Полностью "
             "локально, без отдельных программ; если пакета/модели нет — шаг молча "
             "пропускается (на остальную конвертацию не влияет)."
    )
    parser.add_argument(
        "--llm-model", default="",
        help="Путь к .gguf-модели для доочистки (по умолч. вендоренная в models/)"
    )
    parser.add_argument(
        "--ink-bold", action="store_true",
        help="ЭКСПЕРИМЕНТАЛЬНО: определять жирность по насыщенности штриха на "
             "сканах. По умолчанию ВЫКЛ — на сканах толщину штриха задаёт состав "
             "символов (заглавные имена толще), а не жирность, что даёт ложные "
             "срабатывания. Имеет смысл для сканов высокого DPI (≥300)."
    )
    parser.add_argument(
        "--ocr-preprocess", action="store_true",
        help="Предобработка изображения перед Tesseract (deskew + CLAHE-контраст) для "
             "ПЛОХИХ сканов. По умолчанию ВЫКЛ: помогает шумным/наклонным сканам, но "
             "может портить мелкий текст хороших (CLAHE-артефакты)."
    )
    parser.add_argument(
        "--profile", choices=("lean", "balanced", "full", "auto"), default=None,
        help="Ресурсный профиль (по умолчанию выбирается по машине): lean — "
             "слабый ПК (1 воркер OCR, 2 потока, без LLM: замер по корпусу "
             "256 s при пике 1.4 ГБ), balanced (214 s / 1.7 ГБ), full "
             "(215 s / 2.0 ГБ). Подробности в docling_dev/profiles.py."
    )
    parser.add_argument(
        "--ocr-workers", type=int, default=None, metavar="N",
        help="Сколько страниц распознавать ОДНОВРЕМЕННО (по умолчанию 4, но не "
             "больше числа ядер). Ускоряет сканы примерно на треть; каждая "
             "лишняя страница — это ещё ~100 МБ пика памяти и свой процесс "
             "tesseract. На слабой машине ставьте 1-2."
    )
    args = parser.parse_args()

    # Профиль и число воркеров передаются через окружение: их читают
    # profiles.resolve и ocr_parallel.resolve_workers, и так флаги не
    # приходится протаскивать через всю цепочку вызовов до build_converter.
    if args.profile:
        os.environ["CONVERTER_PROFILE"] = args.profile
    if args.ocr_workers is not None:
        os.environ["CONVERTER_OCR_WORKERS"] = str(args.ocr_workers)

    use_word_order = args.word_order
    highlight      = not args.no_highlight
    # LLM-доочистка по умолчанию зависит от профиля: на слабой машине она стоит
    # ~1.6 ГБ пика и её не включают молча. --no-llm выключает явно и всегда.
    from docling_dev.profiles import resolve as _resolve_profile
    llm            = (not args.no_llm) and _resolve_profile().llm_default
    ocr_preprocess = args.ocr_preprocess

    if args.pdf:
        if not args.pdf.is_file():
            logging.error("Файл не найден: %s", args.pdf)
            sys.exit(1)
        _convert_single(args.pdf, args.output, use_word_order, highlight,
                        llm, args.llm_model, args.ink_bold, ocr_preprocess)
    else:
        if not args.input.is_dir():
            logging.error(
                "Папка не найдена: %s\n"
                "Создайте её или укажите --input /путь/к/папке", args.input
            )
            sys.exit(1)
        _convert_batch(args.input, args.output, args.backup, use_word_order,
                       highlight, llm, args.llm_model, args.ink_bold,
                       ocr_preprocess)


if __name__ == "__main__":
    main()
