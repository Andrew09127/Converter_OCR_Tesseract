# -*- coding: utf-8 -*-
"""
tests/frontend_api.py
─────────────────────
Запуск проверки сетевой логики фронта (пункт 4.4): компилирует
`frontend/src/api.ts` через локальный tsc и прогоняет `frontend_api.mjs` в node
с подменёнными `fetch` и `setTimeout`.

Зачем отдельно от pytest: проверяемый код — TypeScript, а тестового рантайма
(vitest/jest) в проекте нет. Добавлять его только ради этого файла не стали —
зависимость пришлось бы вендорить в офлайн-комплект, хотя на машине
пользователя Node не нужен вовсе (в git лежит собранный `frontend/dist`).

На машине без Node печатает «пропуск» и возвращает 0 — то же правило, что у
`tests/golden.py` и `tests/highlights.py`.

Использование:
    python tests/frontend_api.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

_ROOT = Path(__file__).resolve().parent.parent
_FRONT = _ROOT / "frontend"
_CHECK = Path(__file__).resolve().parent / "frontend_api.mjs"


def main() -> int:
    node = shutil.which("node")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not node or not npx:
        print("пропуск: Node не установлен (на машине пользователя он и не нужен)")
        return 0
    if not (_FRONT / "node_modules" / "typescript").is_dir():
        print("пропуск: нет frontend/node_modules — запустите npm install")
        return 0

    out = Path(tempfile.mkdtemp(prefix="front_api_"))
    try:
        # Компилируем ОДИН модуль: api.ts не тянет React, поэтому собирается
        # отдельно и запускается в node как есть.
        tsc = subprocess.run(
            [npx, "tsc", str(_FRONT / "src" / "api.ts"), "--outDir", str(out),
             "--module", "es2022", "--target", "es2022",
             "--moduleResolution", "bundler", "--lib", "es2022,dom",
             "--skipLibCheck"],
            cwd=str(_FRONT), capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        if tsc.returncode != 0:
            print("tsc не собрал api.ts:")
            print((tsc.stdout or "") + (tsc.stderr or ""))
            return 1
        run = subprocess.run([node, str(_CHECK), (out / "api.js").as_uri()],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace")
        print((run.stdout or "").rstrip())
        if run.stderr.strip():
            print(run.stderr.rstrip())
        return run.returncode
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
