# -*- coding: utf-8 -*-
"""
tests/run_phase.py
──────────────────
Прогон проверок для фазы плана (`plan_phases.md`) одной командой.

Состав прогона:
  1. pytest docling_dev/tests/   — юнит-тесты (быстро, всегда);
  2. tests/smoke_env.py          — готовность окружения;
  3. tests/quality.py check      — снимок качества (регресс текста/структуры);
  4. tests/perf.py check         — снимок скорости и ресурсов;
  5. tests/golden.py check       — эталоны вёрстки (информативно: эталоны
                                   устарели из-за дрейфа OCR с июля, их
                                   переснятие требует явного подтверждения).

Использование:
    python tests/run_phase.py --phase 1
    python tests/run_phase.py --phase 1 --quick      # без снимков качества/скорости
    python tests/run_phase.py --phase 1 --groups scans

Код возврата 0 — все обязательные проверки прошли.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
PY = sys.executable

# Какие проверки обязательны для какой фазы. Фазы — из plan_phases.md.
# golden везде информативен: его эталоны устарели (см. handoff.md).
PHASE_CHECKS: dict[int, tuple[str, ...]] = {
    0: ("pytest", "env"),
    1: ("pytest", "env", "quality", "perf"),
    2: ("pytest", "env", "quality", "perf"),
    3: ("pytest", "env", "perf", "quality"),
    4: ("pytest", "env", "quality"),
}


def run(title: str, cmd: list[str], required: bool) -> tuple[str, bool, float, str]:
    print(f"\n{'─' * 70}\n▶ {title}\n{'─' * 70}")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(_ROOT))
    dt = time.perf_counter() - t0
    ok = proc.returncode == 0
    verdict = "прошло" if ok else ("ПРОВАЛ" if required else "расхождения (информативно)")
    return title, ok or not required, dt, verdict


def main() -> int:
    p = argparse.ArgumentParser(description="Прогон проверок фазы плана")
    p.add_argument("--phase", type=int, required=True, choices=sorted(PHASE_CHECKS))
    p.add_argument("--groups", default="scans,dirty",
                   help="группы корпуса для снимков (по умолч. scans,dirty)")
    p.add_argument("--quick", action="store_true",
                   help="только тесты и окружение (без прогона корпуса)")
    args = p.parse_args()

    checks = PHASE_CHECKS[args.phase]
    if args.quick:
        checks = tuple(c for c in checks if c in ("pytest", "env"))

    plan: list[tuple[str, list[str], bool]] = []
    if "pytest" in checks:
        plan.append(("юнит-тесты (pytest)",
                     [PY, "-m", "pytest", "docling_dev/tests/", "-q"], True))
    if "env" in checks:
        plan.append(("окружение (smoke_env)", [PY, "tests/smoke_env.py"], True))
    if "quality" in checks:
        plan.append(("качество (quality check)",
                     [PY, "tests/quality.py", "check", "--groups", args.groups], True))
    if "perf" in checks:
        plan.append(("скорость и ресурсы (perf check)",
                     [PY, "tests/perf.py", "check", "--groups", args.groups], True))
    plan.append(("эталоны вёрстки (golden check)",
                 [PY, "tests/golden.py", "check"], False))

    results = [run(t, c, r) for t, c, r in plan]

    print(f"\n{'═' * 70}\nИТОГ фазы {args.phase}\n{'═' * 70}")
    failed = 0
    for title, ok, dt, verdict in results:
        print(f"  {title:38} {verdict:30} {dt:6.1f}s")
        if not ok:
            failed += 1
    print(f"\n{'всё в порядке' if not failed else f'провалов: {failed}'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
