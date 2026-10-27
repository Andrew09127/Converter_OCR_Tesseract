# -*- coding: utf-8 -*-
"""
tests/smoke_env.py
──────────────────
Проверка ГОТОВНОСТИ ОКРУЖЕНИЯ конвертера (фаза 0 плана).

Отвечает на вопрос «почему не работает» до того, как пользователь запустит
конвертацию: есть ли Tesseract и нужные языки, собраны ли модели Docling из
кусков `*.partNNN`, на месте ли gguf-модель LLM, хватает ли памяти и диска.

Тот же код пойдёт в эндпоинт `GET /convert/health` (фаза 4 плана) — пишется
один раз, используется дважды: отсюда функция `check_environment()` возвращает
структуру, а печать и код возврата — только в CLI-обёртке.

Использование:
    python tests/smoke_env.py            # печать таблицы, код 1 если что-то критично
    python tests/smoke_env.py --json     # машинночитаемо
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Языки, без которых скан-движок не работает: rus+eng для текста, osd для
# определения ориентации страницы (Docling вызывает его перед каждой страницей).
REQUIRED_LANGS = ("rus", "eng", "osd")
MIN_FREE_DISK_GB = 2.0
MIN_FREE_RAM_GB = 1.5          # пол потребления скан-движка без LLM ≈ 1.25 ГБ


def _tesseract() -> dict:
    from docling_dev.pipeline import resolve_tesseract_cmd
    cmd = resolve_tesseract_cmd()
    item: dict = {"name": "tesseract", "critical": True, "cmd": cmd}
    try:
        ver = subprocess.run([cmd, "--version"], capture_output=True, timeout=20)
        line = (ver.stdout or ver.stderr).decode("utf-8", "replace").splitlines()
        item["version"] = line[0].strip() if line else "?"
        langs = subprocess.run([cmd, "--list-langs"], capture_output=True, timeout=20)
        have = {s.strip() for s in
                langs.stdout.decode("utf-8", "replace").splitlines()[1:] if s.strip()}
        missing = [l for l in REQUIRED_LANGS if l not in have]
        item["langs"] = sorted(have & set(REQUIRED_LANGS))
        item["ok"] = not missing
        item["detail"] = ("все языки на месте" if not missing
                          else f"НЕТ языков: {', '.join(missing)}")
    except Exception as exc:                        # noqa: BLE001
        item["ok"] = False
        item["detail"] = f"не запускается: {type(exc).__name__}: {exc}"
    return item


def _docling_models() -> dict:
    from docling_dev.pipeline import _DOCLING_MODELS_DIR
    d = Path(_DOCLING_MODELS_DIR)
    item: dict = {"name": "модели Docling", "critical": True, "path": str(d)}
    if not d.is_dir():
        item["ok"] = False
        item["detail"] = "папки нет — модели будут скачиваться из сети (офлайн не сработает)"
        return item
    parts = glob.glob(str(d / "**" / "*.part000"), recursive=True)
    unassembled = [p for p in parts if not Path(p[: -len(".part000")]).is_file()]
    # Папка модели вендорится с префиксом репозитория:
    # `docling-project--docling-layout-heron`, поэтому шаблон широкий.
    layout = [p for p in d.glob("**/*layout*") if p.is_dir()]
    tableformer = list(d.glob("**/tableformer/accurate"))
    item["ok"] = bool(layout) and bool(tableformer) and not unassembled
    bits = []
    bits.append("layout heron" if layout else "НЕТ layout-модели")
    bits.append("tableformer accurate" if tableformer else "НЕТ tableformer")
    if unassembled:
        bits.append(f"НЕ СОБРАНО из кусков: {len(unassembled)}")
    if not list(d.glob("**/tableformer/fast")):
        bits.append("fast-режим tableformer не вендорен (это нормально)")
    item["detail"] = "; ".join(bits)
    return item


def _llm_model() -> dict:
    from docling_dev.llm_correct import _DEFAULT_MODEL, _resolve_model
    item: dict = {"name": "LLM-модель (gguf)", "critical": False,
                  "path": _DEFAULT_MODEL}
    resolved = _resolve_model(None)
    try:
        import llama_cpp  # noqa: F401
        have_pkg = True
    except Exception:                               # noqa: BLE001
        have_pkg = False
    item["ok"] = bool(resolved) and have_pkg
    if not have_pkg:
        item["detail"] = "нет пакета llama-cpp-python — доочистка будет пропущена"
    elif not resolved:
        item["detail"] = "нет файла модели и кусков *.partNNN — доочистка пропустится"
    else:
        size = Path(resolved).stat().st_size / 2 ** 20
        item["detail"] = f"{Path(resolved).name}, {size:.0f} МБ"
    return item


def _morphology() -> dict:
    item: dict = {"name": "морфология (pymorphy)", "critical": False}
    try:
        from docling_dev.highlight import _morph
        item["ok"] = _morph is not None
        item["detail"] = ("словарь загружен" if _morph is not None
                          else "нет pymorphy — подсветка и автоправки работают хуже")
    except Exception as exc:                        # noqa: BLE001
        item["ok"] = False
        item["detail"] = f"ошибка импорта: {exc}"
    return item


def _ocr_workers() -> int:
    """Сколько страниц скан-движок распознаёт одновременно (п.2.1): от этого
    зависит пик памяти — каждая держит свою страницу в 216 DPI."""
    try:
        from docling_dev.ocr_parallel import resolve_workers
        return resolve_workers()
    except Exception:
        return 1


def _resources() -> list[dict]:
    import psutil
    vm = psutil.virtual_memory()
    free_ram = vm.available / 2 ** 30
    disk = shutil.disk_usage(str(_ROOT))
    free_disk = disk.free / 2 ** 30
    cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 1
    return [
        {"name": "свободная память", "critical": True,
         "ok": free_ram >= MIN_FREE_RAM_GB,
         "detail": f"{free_ram:.1f} ГБ свободно из {vm.total / 2 ** 30:.0f} ГБ "
                   f"(минимум {MIN_FREE_RAM_GB} ГБ без LLM, ~3 ГБ с LLM; "
                   f"параллельный OCR — {_ocr_workers()} страниц(ы) разом, "
                   f"это ещё ~0.1 ГБ на каждую сверх первой)"},
        {"name": "свободный диск", "critical": True,
         "ok": free_disk >= MIN_FREE_DISK_GB,
         "detail": f"{free_disk:.1f} ГБ свободно (минимум {MIN_FREE_DISK_GB} ГБ "
                   f"на временные PNG и DOCX)"},
        {"name": "процессор", "critical": False, "ok": True,
         "detail": f"{cores} физических ядер, {psutil.cpu_count()} логических"},
    ]


def check_environment() -> dict:
    """Структура готовности окружения (её же отдаст /convert/health в фазе 4)."""
    items = [_tesseract(), _docling_models(), _llm_model(), _morphology()]
    items += _resources()
    critical_bad = [i for i in items if i.get("critical") and not i.get("ok")]
    return {"ready": not critical_bad, "items": items,
            "corpus_dir": os.environ.get("CONVERTER_CORPUS_DIR", "(по умолчанию)")}


def main() -> int:
    p = argparse.ArgumentParser(description="Проверка окружения конвертера")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    rep = check_environment()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["ready"] else 1
    for i in rep["items"]:
        flag = "OK  " if i.get("ok") else ("СБОЙ" if i.get("critical") else "нет ")
        print(f"  [{flag}] {i['name']:24} {i['detail']}")
    print(f"\nготовность: {'ДА' if rep['ready'] else 'НЕТ (см. СБОЙ выше)'}")
    return 0 if rep["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
