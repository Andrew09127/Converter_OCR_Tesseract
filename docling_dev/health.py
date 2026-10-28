"""
health.py
─────────
Готовность окружения одним ответом (пункт 4.3).

Отвечает на вопрос «почему не работает» ДО запуска конвертации: есть ли
Tesseract и языки rus/eng/osd, собраны ли модели Docling из кусков `*.partNNN`,
на месте ли gguf-модель LLM, есть ли словарь морфологии, хватает ли памяти и
диска, какой выбран ресурсный профиль и прогреты ли модели.

Код приехал сюда из `tests/smoke_env.py` (он написан ещё в фазе 0 и с самого
начала предназначался и для эндпоинта). Теперь проверки живут в пакете, а
`tests/smoke_env.py` остался CLI-обёрткой: печать таблицы и код возврата.
Одна реализация, два потребителя.

ПРО ДВА РЕЖИМА. `deep=True` честно импортирует пакеты (так делает CLI-проверка).
`deep=False` (по умолчанию, и так зовёт эндпоинт) обходится `find_spec` — потому
что `import llama_cpp` тянет DLL, а `llm_correct._resolve_model` при отсутствии
собранного файла СКЛЕИВАЕТ модель из кусков на пару гигабайт. Проверка
готовности не должна ничего собирать и грузить.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
from importlib.util import find_spec
from pathlib import Path

log = logging.getLogger(__name__)

# Языки, без которых скан-движок не работает: rus+eng для текста, osd для
# определения ориентации страницы (Docling вызывает его перед страницей).
REQUIRED_LANGS = ("rus", "eng", "osd")
MIN_FREE_DISK_GB = 2.0
MIN_FREE_RAM_GB = 1.5          # пол потребления скан-движка без LLM ≈ 1.25 ГБ

_ROOT = Path(__file__).resolve().parent.parent


def _tesseract() -> dict:
    from .pipeline import resolve_tesseract_cmd
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
    from .pipeline import _DOCLING_MODELS_DIR
    d = Path(_DOCLING_MODELS_DIR)
    item: dict = {"name": "модели Docling", "critical": True, "path": str(d)}
    if not d.is_dir():
        item["ok"] = False
        item["detail"] = ("папки нет — модели будут скачиваться из сети "
                          "(офлайн не сработает)")
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


def _llm_model(deep: bool) -> dict:
    from .llm_correct import _DEFAULT_MODEL
    item: dict = {"name": "LLM-модель (gguf)", "critical": False,
                  "path": _DEFAULT_MODEL}
    if deep:
        from .llm_correct import _resolve_model
        resolved = _resolve_model(None)
        have_pkg = find_spec("llama_cpp") is not None
    else:
        # Ни собирать модель из кусков, ни грузить DLL ради проверки готовности
        # нельзя: первое пишет гигабайты, второе остаётся в памяти навсегда.
        resolved = (_DEFAULT_MODEL if os.path.isfile(_DEFAULT_MODEL)
                    else (_DEFAULT_MODEL if glob.glob(_DEFAULT_MODEL + ".part*")
                          else None))
        have_pkg = find_spec("llama_cpp") is not None
    item["ok"] = bool(resolved) and have_pkg
    if not have_pkg:
        item["detail"] = "нет пакета llama-cpp-python — доочистка будет пропущена"
    elif not resolved:
        item["detail"] = "нет файла модели и кусков *.partNNN — доочистка пропустится"
    elif os.path.isfile(resolved):
        size = Path(resolved).stat().st_size / 2 ** 20
        item["detail"] = f"{Path(resolved).name}, {size:.0f} МБ"
    else:
        item["detail"] = (f"{Path(resolved).name}: лежит кусками *.partNNN, "
                          f"соберётся при первой доочистке")
    return item


def _morphology(deep: bool) -> dict:
    item: dict = {"name": "морфология (pymorphy)", "critical": False}
    try:
        if deep:
            from .highlight import _morph
            have = _morph is not None
        else:
            have = (find_spec("pymorphy3") or find_spec("pymorphy2")) is not None
        item["ok"] = have
        item["detail"] = ("словарь загружен" if have
                          else "нет pymorphy — подсветка и автоправки работают хуже")
    except Exception as exc:                        # noqa: BLE001
        item["ok"] = False
        item["detail"] = f"ошибка импорта: {exc}"
    return item


def _ocr_workers() -> int:
    """Сколько страниц скан-движок распознаёт одновременно (п.2.1): от этого
    зависит пик памяти — каждая держит свою страницу в 216 DPI."""
    try:
        from .ocr_parallel import resolve_workers
        return resolve_workers()
    except Exception:                               # noqa: BLE001
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


def check_environment(deep: bool = False) -> dict:
    """Структура готовности окружения.

    `ready` — не сломано ли что-то КРИТИЧНОЕ (tesseract, модели, память, диск).
    Некритичное (LLM, морфология) на готовность не влияет: без них конвертация
    работает, просто хуже чистит текст.
    """
    items = [_tesseract(), _docling_models(), _llm_model(deep), _morphology(deep)]
    items += _resources()
    critical_bad = [i for i in items if i.get("critical") and not i.get("ok")]

    report = {"ready": not critical_bad, "items": items,
              "problems": [i["name"] for i in critical_bad],
              "corpus_dir": os.environ.get("CONVERTER_CORPUS_DIR", "(по умолчанию)")}
    try:
        from .profiles import machine, resolve
        prof = resolve()
        m = machine()
        report["profile"] = {"profile": prof.name, "note": prof.note,
                             "ocr_workers": prof.ocr_workers,
                             "num_threads": prof.num_threads,
                             "llm_default": prof.llm_default,
                             "cores": m["cores"],
                             "ram_gb": round(m["ram_gb"], 1),
                             "free_gb": round(m["free_gb"], 1)}
    except Exception as exc:                        # noqa: BLE001
        log.debug("health: профиль недоступен (%s)", exc)
    try:
        from .warmup import state
        report["warmup"] = state()["state"]
    except Exception as exc:                        # noqa: BLE001
        log.debug("health: состояние прогрева недоступно (%s)", exc)
    return report
