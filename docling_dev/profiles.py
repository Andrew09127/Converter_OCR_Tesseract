# -*- coding: utf-8 -*-
"""
profiles.py — профили производительности под машину (пункт 3.1).

Критерий фазы 3: конвертер должен доходить до конца на машине с 4-8 ГБ ОЗУ и
2-4 ядрами, пусть медленнее. Поэтому ресурсные настройки собраны в одном месте
и выбираются по машине, а не задаются константами в коде.

ЗАМЕРЕНО НА КОРПУСЕ (8 сканов, без LLM, 28.10.2026):

| профиль    | воркеров OCR | потоков моделей | images_scale | wall    | пик RSS |
|------------|--------------|-----------------|--------------|---------|---------|
| lean       | 1            | 2               | 1.0          | 256.6 s | 1421 МБ |
| balanced   | 2            | 4               | 1.5          | 214.0 s | 1654 МБ |
| full       | 4            | 4               | 2.0          | ~215 s  | 1952 МБ |

Из этих чисел следуют два неочевидных вывода:

* `balanced` НЕ МЕДЛЕННЕЕ `full` на коротких файлах (214 против 215 s) при пике
  на 300 МБ ниже. Причина та же, что заставила поставить 4 воркера вместо 8 в
  п.2.1: `ThreadedQueue.get_batch` в Docling батч не накапливает, и на
  документе в 3-8 страниц двух воркеров уже почти достаточно. Разница в пользу
  `full` появляется на длинных документах, где очередь успевает наполняться.
* `lean` стоит +19% времени и даёт −531 МБ. Это и есть цена входа на слабую
  машину.

ЧЕГО В ПРОФИЛЯХ НЕТ, хотя план (plan_phases.md, 3.1) это предлагал:

* РАЗНЫЕ БАТЧИ СТАДИЙ (1/4/8). Пункт 2.2 замерил, что батч разметки и таблиц 1
  лучше четырёх ВЕЗДЕ — и по времени, и на 800 МБ по памяти. Делать его хуже на
  мощной машине незачем. Батч стадии OCR равен числу воркеров, иначе
  параллелить нечего.
* ОТКЛЮЧЕНИЕ КАРТИНОК СТРАНИЦ. Замер: 30 страниц, 2082 МБ без картинок против
  2159 МБ с ними — 77 МБ, то есть в пределах разброса. А содержимое терять
  нельзя: из картинок страниц вырезаются рукописные строки угловой шапки
  (`converter._render_corner_letterhead`). Поэтому масштаб понижаем, а сами
  картинки оставляем.
* РОДНОЙ DPI и ВТОРОЙ ПРОХОД OCR (1.Б.2 и 1.Б.3) — оба отвергнуты замером ещё
  в фазе 1, их просто нет в коде.

Выбор профиля: `CONVERTER_PROFILE` → по машине. Руками — флаг CLI `--profile`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Profile:
    """Ресурсные настройки одного профиля."""

    name: str
    ocr_workers: int          # страниц распознаётся одновременно (п.2.1)
    num_threads: int          # потоков torch для разметки и таблиц
    images_scale: float       # разрешение картинок страниц (72 × scale DPI)
    llm_default: bool         # включать ли LLM-доочистку по умолчанию
    llm_unload: bool          # выгружать ли модель LLM после задачи
    note: str

    @property
    def ocr_dpi(self) -> int:
        """Разрешение OCR не зависит от профиля: 216 DPI (scale=3 внутри
        TesseractOcrCliModel). Снижать его замер запретил (1.Б.2), повышать —
        тоже (−38 словарных слов при времени ×1.25)."""
        return 216


LEAN = Profile(
    name="lean", ocr_workers=1, num_threads=2, images_scale=1.0,
    llm_default=False, llm_unload=True,
    note="слабая машина: 4-8 ГБ ОЗУ или 2 ядра",
)
BALANCED = Profile(
    name="balanced", ocr_workers=2, num_threads=4, images_scale=1.5,
    llm_default=False, llm_unload=True,
    note="середина: до 12 ГБ ОЗУ или до 4 ядер",
)
FULL = Profile(
    name="full", ocr_workers=4, num_threads=4, images_scale=2.0,
    llm_default=True, llm_unload=False,
    note="рабочая станция: больше 12 ГБ ОЗУ и больше 4 ядер",
)

PROFILES = {p.name: p for p in (LEAN, BALANCED, FULL)}

# Пороги выбора. Берём ПОЛНУЮ память машины, а не свободную: свободная скачет от
# того, что у пользователя открыто, и профиль прыгал бы от запуска к запуску.
# Нехватку свободной памяти проверяем отдельно и предупреждением, а не выбором.
_LEAN_RAM_GB = 6.0
_LEAN_CORES = 2
_BALANCED_RAM_GB = 12.0
_BALANCED_CORES = 4


def machine() -> dict:
    """Что известно о машине: физические ядра и память в ГБ."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {
            "cores": psutil.cpu_count(logical=False) or psutil.cpu_count() or 1,
            "ram_gb": vm.total / 2 ** 30,
            "free_gb": vm.available / 2 ** 30,
        }
    except Exception as exc:                        # noqa: BLE001
        log.debug("profiles: psutil недоступен (%s) — считаю машину слабой", exc)
        return {"cores": 1, "ram_gb": 0.0, "free_gb": 0.0}


def detect() -> Profile:
    """Профиль по машине. При сомнении выбираем более экономный: лучше медленнее,
    чем уход в swap или MemoryError."""
    m = machine()
    if m["ram_gb"] < _LEAN_RAM_GB or m["cores"] <= _LEAN_CORES:
        return LEAN
    if m["ram_gb"] < _BALANCED_RAM_GB or m["cores"] <= _BALANCED_CORES:
        return BALANCED
    return FULL


def resolve(name: str | None = None) -> Profile:
    """Профиль: аргумент → `CONVERTER_PROFILE` → выбор пользователя → машина.

    Выбор пользователя (диалог настроек приложения) хранится в
    `docling_dev/settings.py`; окружение старше его, потому что им гоняются
    замеры и A/B — сохранённый файл не должен их перебивать.
    """
    raw = (name or "").strip().lower()
    if not raw:
        from .settings import profile_name
        raw = (profile_name() or "").strip().lower()
    if raw:
        if raw in PROFILES:
            return PROFILES[raw]
        if raw == "auto":
            return detect()
        log.warning("профиль %r не знаю (есть %s) — выбираю по машине",
                    raw, ", ".join(PROFILES))
    return detect()


def apply_threads(profile: Profile) -> None:
    """Ограничивает потоки torch по профилю.

    Зачем явно: torch по умолчанию берёт все ядра, и на 2-ядерной машине это
    душит интерфейс — пользователь видит зависшее приложение. Переменные
    окружения выставляем только если их не задали снаружи: чужую настройку
    перебивать нельзя.
    """
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, str(profile.num_threads))
    try:
        import torch
        torch.set_num_threads(profile.num_threads)
    except Exception as exc:                        # noqa: BLE001
        log.debug("profiles: не удалось ограничить потоки torch (%s)", exc)


def memory_warning(profile: Profile, pages: int = 0) -> str | None:
    """Предупреждение, если свободной памяти заведомо мало (пункт 3.4).

    Пики замерены по корпусу: lean 1.4 ГБ, balanced 1.7 ГБ, full 2.0 ГБ без
    LLM; LLM добавляет ~1.6 ГБ. Плюс замеренный рост ~13 МБ на страницу — он
    не зависит от профиля, это удерживаемые Docling результаты страниц.
    """
    need = {"lean": 1.5, "balanced": 1.8, "full": 2.1}[profile.name]
    if pages > 0:
        need += 0.013 * pages
    free = machine()["free_gb"]
    if free <= 0 or free >= need:
        return None
    return (f"Свободной памяти {free:.1f} ГБ, а на этот файл в режиме "
            f"«{profile.name}» нужно примерно {need:.1f} ГБ. Закройте лишние "
            f"программы или включите экономный режим — иначе конвертация "
            f"может не дойти до конца.")


def describe(profile: Profile) -> str:
    m = machine()
    return (f"профиль {profile.name} ({profile.note}): воркеров OCR "
            f"{profile.ocr_workers}, потоков моделей {profile.num_threads}, "
            f"images_scale {profile.images_scale}, LLM по умолчанию "
            f"{'вкл' if profile.llm_default else 'выкл'}; машина — "
            f"{m['cores']} физ. ядер, {m['ram_gb']:.0f} ГБ ОЗУ "
            f"({m['free_gb']:.1f} ГБ свободно)")
