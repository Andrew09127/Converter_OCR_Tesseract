# -*- coding: utf-8 -*-
"""
tests/smoke_env.py
──────────────────
Проверка ГОТОВНОСТИ ОКРУЖЕНИЯ конвертера — CLI-обёртка.

Сами проверки переехали в `docling_dev/health.py` (пункт 4.3): их же отдаёт
`GET /convert/health`. Здесь остались только печать таблицы и код возврата,
чтобы проверка годилась для `tests/run_phase.py` и для ручного запуска на
машине пользователя.

CLI зовёт `check_environment(deep=True)`: в отличие от эндпоинта, ему можно
честно импортировать llama-cpp и pymorphy и собрать gguf-модель из кусков —
это проверка, а не ответ на запрос.

Использование:
    python tests/smoke_env.py            # печать таблицы, код 1 если что-то критично
    python tests/smoke_env.py --json     # машинночитаемо
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from docling_dev.health import (                    # noqa: E402  (после sys.path)
    MIN_FREE_DISK_GB, MIN_FREE_RAM_GB, REQUIRED_LANGS, check_environment,
)

__all__ = ["check_environment", "REQUIRED_LANGS",
           "MIN_FREE_DISK_GB", "MIN_FREE_RAM_GB", "main"]


def main() -> int:
    p = argparse.ArgumentParser(description="Проверка окружения конвертера")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    rep = check_environment(deep=True)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["ready"] else 1
    for i in rep["items"]:
        flag = "OK  " if i.get("ok") else ("СБОЙ" if i.get("critical") else "нет ")
        print(f"  [{flag}] {i['name']:24} {i['detail']}")
    prof = rep.get("profile")
    if prof:
        print(f"\n  профиль: {prof['profile']} — {prof['note']}")
    print(f"\nготовность: {'ДА' if rep['ready'] else 'НЕТ (см. СБОЙ выше)'}")
    return 0 if rep["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
