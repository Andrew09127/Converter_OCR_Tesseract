# -*- coding: utf-8 -*-
"""
tests/corpus.py
───────────────
Фиксированный корпус для снимков качества и скорости (фаза 0 плана).

Сами PDF в git не лежат (это рабочие документы пользователя) — здесь только
МАНИФЕСТ: имя файла, группа, ожидаемое число страниц и класс. Путь к папке с
файлами берётся из переменной окружения CONVERTER_CORPUS_DIR, по умолчанию —
датасет заявлений. Отсутствующий файл не роняет прогон: он помечается
`skipped`, а снимок сравнивается только по тем файлам, что есть и там, и тут.

Файлы КОПИРУЮТСЯ в рабочую папку перед прогоном: батч-режим конвертера
перемещает исходники в backup, и работать на оригиналах датасета нельзя.

Группы:
  scans   — истинные сканы (растр без текстового слоя), основной корпус;
  dirty   — скан с пятнами/штампами (проверка, что чистка всё ещё чистит);
  natives — цифровые PDF с текстовым слоем (рубеж для п.1.5 плана: сейчас они
            прогоняются скан-движком и получают OCR-ошибки на ровном месте).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_DIR = Path(r"C:\Диск D\Сбер\мусорные заявления\dataset")


def corpus_dir() -> Path:
    return Path(os.environ.get("CONVERTER_CORPUS_DIR", str(_DEFAULT_DIR)))


@dataclass(frozen=True)
class CorpusFile:
    name: str
    group: str
    pages: int
    note: str

    @property
    def stem(self) -> str:
        return Path(self.name).stem


# Состав выверен замером 25-26.10.2026 (см. handoff.md): растр сканов 150/200/
# 300/600 DPI, нативные — реальные шрифты Times/Arial/Calibri.
MANIFEST: tuple[CorpusFile, ...] = (
    # ── истинные сканы ───────────────────────────────────────────────────────
    CorpusFile("A53-9758-2026_20260318_Zajavlenie.pdf", "scans", 4,
               "растр 200 DPI; заголовок внизу стр.1 рядом с росчерками"),
    CorpusFile("Умерший ким клим.pdf", "scans", 3,
               "растр 200 DPI; стр.1 — кейс потери текста чисткой (п.1.1)"),
    CorpusFile("Заявление о включении в ртк  Артемов И.Г..pdf", "scans", 3,
               "растр 300 DPI; письмо ФНС с гербом, тяжёлые таблицы"),
    CorpusFile("Заявление о включении в ртк  Геворгян К.В..pdf", "scans", 3,
               "растр 300 DPI; письмо ФНС, левовыровненный адресат"),
    CorpusFile("Заявление о признании гражданина несостоятельным (банкротом).pdf",
               "scans", 4, "растр 600 DPI; OSD падает на стр.3"),
    CorpusFile("Зайцев В.В._заявление.pdf", "scans", 7,
               "растр 200 DPI; угловой штамп ФНС, 10 таблиц"),
    CorpusFile("Заявление на 6 л. в 1 экз.pdf", "scans", 6,
               "растр 150 DPI + OCR-подложка; шапка ФНС"),
    # ── грязный скан ─────────────────────────────────────────────────────────
    CorpusFile("Заявление о признании банкротом.pdf", "dirty", 8,
               "растр 200 DPI; пятна/штампы, заявление физлица (Мирский)"),
    # ── нативные (рубеж для п.1.5) ───────────────────────────────────────────
    CorpusFile("Заявление РТК Богачева.pdf", "natives", 2, "шапка ФНС, Times"),
    CorpusFile("Чернов Николай Николаевич_95.pdf", "natives", 5, "требование ФНС"),
    CorpusFile("ФНС Заявление (1).pdf", "natives", 4, "заявление уполн. органа"),
    CorpusFile("Заявление_Базов Георгий Николаевич_20.pdf", "natives", 4, "ФНС"),
    CorpusFile("Заявление РФЛ.pdf", "natives", 4, "заявление физлица"),
    CorpusFile("Заявление о включении в РТК А53-10433-2026  3 стр.pdf",
               "natives", 3, "Совкомбанк, LiberationSerif"),
)

GROUPS = ("scans", "dirty", "natives")


def select(groups: str | tuple[str, ...] = GROUPS) -> list[CorpusFile]:
    """Файлы манифеста выбранных групп (строка 'scans,dirty' тоже принимается)."""
    if isinstance(groups, str):
        groups = tuple(g.strip() for g in groups.split(",") if g.strip())
    unknown = set(groups) - set(GROUPS)
    if unknown:
        raise SystemExit(f"неизвестные группы: {', '.join(sorted(unknown))}; "
                         f"доступны: {', '.join(GROUPS)}")
    return [f for f in MANIFEST if f.group in groups]


def md5(path: Path, limit: int = 8 << 20) -> str:
    """MD5 первых `limit` байт — чтобы заметить подмену файла в корпусе."""
    h = hashlib.md5()
    with path.open("rb") as fh:
        h.update(fh.read(limit))
    return h.hexdigest()[:12]


def work_dir(tag: str) -> Path:
    """Рабочая папка прогона в системном temp (копии PDF + готовые DOCX)."""
    d = Path(tempfile.gettempdir()) / f"converter_tests_{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prepare(files: list[CorpusFile], tag: str) -> list[tuple[CorpusFile, Path]]:
    """Копирует существующие файлы корпуса в рабочую папку.

    Возвращает [(описание, путь к копии)]. Отсутствующие файлы пропускаются с
    сообщением — прогон не падает (у другого разработчика может не быть части
    документов).
    """
    dst_dir = work_dir(tag)
    out: list[tuple[CorpusFile, Path]] = []
    src_dir = corpus_dir()
    for f in files:
        src = src_dir / f.name
        if not src.is_file():
            print(f"  skipped: нет файла {f.name}")
            continue
        dst = dst_dir / f.name
        if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        out.append((f, dst))
    return out
