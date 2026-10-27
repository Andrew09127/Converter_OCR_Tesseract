# -*- coding: utf-8 -*-
"""
tests/highlights.py
───────────────────
Точность подсветки скан-движка по РУЧНОЙ РАЗМЕТКЕ (пункт 1.4 плана).

Снимки `quality.py` считают подсвеченные слова, но не знают, правильно ли они
подсвечены. Здесь считается то, что важно на деле:
  * сколько ЛОЖНЫХ подсветок (верное слово помечено как ошибка) — это шум, в
    котором тонет проверяющий, и зря сожжённые запросы к LLM;
  * сколько ПРОПУЩЕННЫХ искажений (ошибка OCR без подсветки) — это куда хуже
    шума: такую ошибку никто не увидит.

Разметка и документы НЕ в git: в них ФИО, адреса и СНИЛС реальных людей.
  * разметка   — `<корпус>/highlight_labels.json` (`ok` / `bad` на слово);
  * документы  — `<корпус>/highlight_docx/*.docx`, готовятся действием
                 `prepare` (конвертация сканов, ~5 минут).

Использование:
    python tests/highlights.py prepare          # сконвертировать сканы в docx
    python tests/highlights.py check            # сверить с разметкой
    python tests/highlights.py check --list     # плюс перечислить расхождения
    python tests/highlights.py labels           # шаблон разметки для новых слов
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import corpus  # noqa: E402

LABELS_NAME = "highlight_labels.json"
DOCX_DIRNAME = "highlight_docx"


def labels_path() -> Path:
    return corpus.corpus_dir() / LABELS_NAME


def docx_dir() -> Path:
    return corpus.corpus_dir() / DOCX_DIRNAME


class NoLabels(Exception):
    """Нет локальной разметки — проверку нечем вести."""


def _load_labels() -> dict:
    p = labels_path()
    if not p.is_file():
        raise NoLabels(str(p))
    return json.loads(p.read_text(encoding="utf-8"))


def _docx_for(stem: str, files: list[Path]) -> Path | None:
    """Ищет docx по ключу разметки. Совпадение должно быть ОДНОЗНАЧНЫМ: у части
    заявлений первые два десятка символов имени совпадают («Заявление о
    признании банкротом» и «…гражданина несостоятельным»), и приблизительное
    сопоставление подсовывало контекст чужого документа."""
    exact = [f for f in files if f.stem == stem]
    if exact:
        return exact[0]
    near = [f for f in files if f.stem.startswith(stem) or stem.startswith(f.stem)]
    if len(near) == 1:
        return near[0]
    if len(near) > 1:
        print(f"  пропуск: имя {stem!r} подходит сразу к {len(near)} документам")
    return None


def cmd_prepare(args) -> int:
    """Конвертирует сканы корпуса в docx с подсветкой (без LLM: он недетерминирован)."""
    from docling_dev.pipeline import build_converter
    from docling_dev.converter import build_docx
    from docling_dev.highlight import highlight_suspicious

    dst = docx_dir()
    dst.mkdir(parents=True, exist_ok=True)
    files = corpus.prepare(corpus.select(args.groups.split(",")), "highlights")
    if not files:
        print("корпус пуст — нечего готовить")
        return 1
    conv = build_converter(ocr_preprocess=False)
    for cf, pdf in files:
        res = conv.convert(str(pdf))
        dl = res.document
        sizes = {int(pn): (float(pg.size.width), float(pg.size.height))
                 for pn, pg in dl.pages.items() if getattr(pg, "size", None)}
        doc = build_docx(dl, sizes, ocr_reader=None, use_word_order=False,
                         ink_bold=False)
        n = highlight_suspicious(doc)
        out = dst / (cf.name.rsplit(".", 1)[0] + ".docx")
        doc.save(str(out))
        print(f"  {cf.name}: подсвечено {n} → {out.name}")
    return 0


def _collect(path: Path) -> tuple[list, list]:
    """(подсвеченные слова, тексты абзацев) из готового docx."""
    from docx import Document
    from docx.enum.text import WD_COLOR_INDEX
    from docling_dev.highlight import _iter_paragraphs

    doc = Document(str(path))
    paras = list(_iter_paragraphs(doc))
    words = []
    for p in paras:
        for r in p.runs:
            if r.font.highlight_color == WD_COLOR_INDEX.YELLOW and r.text.strip():
                words.append((r.text.strip(), p.text.strip()[:90]))
    return words, [p.text for p in paras]


def cmd_labels(args) -> int:
    """Собирает подсвеченные слова в шаблон разметки (label = "?" — заполнить)."""
    docs = sorted(docx_dir().glob("*.docx"))
    if not docs:
        print(f"нет документов в {docx_dir()} — сначала `prepare`")
        return 1
    try:
        old = _load_labels()
    except NoLabels:
        old = {"files": {}}
    out = {"_note": "ok = верное слово (подсвечивать нельзя); "
                    "bad = искажение или мусор OCR (подсветка обязательна)",
           "files": {}}
    added = 0
    for d in docs:
        words, _ = _collect(d)
        known = old.get("files", {}).get(d.stem, {})
        rows = {}
        for w, ctx in words:
            if w in rows:
                continue
            prev = known.get(w)
            rows[w] = prev if prev else {"label": "?", "ctx": ctx}
            if not prev:
                added += 1
        out["files"][d.stem] = rows
    labels_path().write_text(json.dumps(out, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print(f"разметка: {labels_path()} (новых слов без метки: {added})")
    return 0


def cmd_check(args) -> int:
    from docling_dev.highlight import build_doc_context, _is_suspicious

    # Разметка и документы лежат ВНЕ git (в них ФИО, адреса, СНИЛС), поэтому на
    # другой машине проверка ПРОПУСКАЕТСЯ, а не валит прогон фазы.
    try:
        labels = _load_labels()
    except NoLabels as exc:
        print(f"пропуск: нет разметки {exc}")
        print("  подготовить: python tests/highlights.py prepare, затем labels")
        return 0
    docs = sorted(docx_dir().glob("*.docx"))
    if not docs:
        print(f"пропуск: нет документов в {docx_dir()} — сначала `prepare`")
        return 0

    tot = {"bad": 0, "ok": 0, "false_pos": 0, "missed": 0, "unlabeled": 0}
    false_pos, missed = [], []
    for stem, rows in labels.get("files", {}).items():
        d = _docx_for(stem, docs)
        if d is None:
            print(f"  пропуск: нет docx для {stem}")
            continue
        _, texts = _collect(d)
        ctx = build_doc_context(texts)
        for word, info in rows.items():
            label = info.get("label")
            if label not in ("ok", "bad"):
                tot["unlabeled"] += 1
                continue
            tot[label] += 1
            flagged = _is_suspicious(word, ctx)
            if label == "ok" and flagged:
                tot["false_pos"] += 1
                false_pos.append((stem[:18], word))
            elif label == "bad" and not flagged:
                tot["missed"] += 1
                missed.append((stem[:18], word))

    n_ok, n_bad = tot["ok"], tot["bad"]
    print(f"\nразмечено слов: искажений {n_bad}, верных {n_ok}"
          f"{', без метки ' + str(tot['unlabeled']) if tot['unlabeled'] else ''}")
    print(f"ложных подсветок: {tot['false_pos']} из {n_ok} верных слов "
          f"({100 * tot['false_pos'] / max(n_ok, 1):.0f}%)")
    print(f"пропущенных искажений: {tot['missed']} из {n_bad} "
          f"({100 * tot['missed'] / max(n_bad, 1):.0f}%)")
    if args.list:
        for title, rows_ in (("ложные подсветки", false_pos),
                             ("ПРОПУЩЕННЫЕ искажения", missed)):
            if rows_:
                print(f"\n{title}:")
                for f, w in rows_:
                    print(f"  {f:18} {w}")
    # Пропуск искажения — провал: его никто не увидит. Ложные подсветки только
    # печатаем: это цель оптимизации, а не условие прохождения.
    if tot["missed"]:
        print("\nПРОВАЛ: часть искажений перестала подсвечиваться")
        return 1
    print("\nни одно искажение не потеряно")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Точность подсветки по разметке")
    p.add_argument("action", choices=("prepare", "check", "labels"))
    p.add_argument("--groups", default="scans,dirty")
    p.add_argument("--list", action="store_true", help="перечислить расхождения")
    args = p.parse_args()
    return {"prepare": cmd_prepare, "check": cmd_check,
            "labels": cmd_labels}[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
