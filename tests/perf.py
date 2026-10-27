# -*- coding: utf-8 -*-
"""
tests/perf.py
─────────────
Снимок СКОРОСТИ и РЕСУРСОВ скан-движка (фаза 0 плана).

Меряет на каждый файл корпуса:
  • wall по стадиям Docling (page_parse / ocr / layout / table_structure / …)
    и по нашим шагам (build_docx, highlight, save);
  • внутренности OCR-стадии: OSD-субпроцесс, tesseract, наша чистка, рендеры —
    через обёртки над теми же точками, что патчит ocr_preprocess;
  • CPU-секунды дерева процессов (user+sys, включая детей) — реальный объём
    вычислений, он не зависит от распараллеливания;
  • пиковую RSS дерева процессов.

Снимок привязан к МАШИНЕ (имя + ядра + ОЗУ): абсолютные числа между машинами
несравнимы. Допуски `check`: wall ±25% (шум планировщика), CPU-секунды ±10%,
пиковая RSS ±10%. Дополнительно печатается профиль в процентах по стадиям —
именно он ловит регресс вида «ускорили OCR, но раздули таблицы».

Использование:
    python tests/perf.py save [--groups scans] [--llm]
    python tests/perf.py check [--groups scans]

LLM по умолчанию ВЫКЛЮЧЕН: он даёт 60% CPU-секунд и большой разброс по числу
правок, из-за чего снимок перестаёт быть воспроизводимым. Для отдельного
замера стоимости LLM есть флаг --llm (снимок пишется в другой файл).
"""
from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import corpus  # noqa: E402

STAGES = ("page_parse", "ocr", "layout", "table_structure", "page_assemble",
          "doc_assemble", "reading_order", "doc_enrich")


def machine_key() -> str:
    import psutil
    gb = round(psutil.virtual_memory().total / 2 ** 30)
    return f"{socket.gethostname()}-{psutil.cpu_count()}c-{gb}g-{platform.system()}"


class _Sampler:
    """Пиковая RSS дерева процессов и CPU-секунды (user+sys, включая детей)."""

    def __init__(self) -> None:
        import psutil
        self._ps = psutil
        self.proc = psutil.Process()
        self.peak_rss = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def cpu_seconds(self) -> float:
        t = self.proc.cpu_times()
        return (t.user + t.system + getattr(t, "children_user", 0.0)
                + getattr(t, "children_system", 0.0))

    def _run(self) -> None:
        while not self._stop.wait(0.25):
            try:
                rss = self.proc.memory_info().rss
                for ch in self.proc.children(recursive=True):
                    try:
                        rss += ch.memory_info().rss
                    except self._ps.Error:
                        pass
                self.peak_rss = max(self.peak_rss, rss)
            except self._ps.Error:
                pass

    def __enter__(self) -> "_Sampler":
        self.peak_rss = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)


def _instrument_ocr() -> dict:
    """Обёртки на OSD / tesseract / чистку / рендер страницы. Счётчики в dict."""
    from docling.backend.pypdfium2_backend import PyPdfiumPageBackend
    from docling.models.stages.ocr.tesseract_ocr_cli_model import TesseractOcrCliModel
    import docling_dev.ocr_preprocess as opre

    c = {k: 0.0 for k in ("osd_s", "tess_s", "clean_s", "render_s")}
    c.update({k: 0 for k in ("osd_n", "tess_n", "clean_n", "render_n")})
    # Со времён п.2.1 страницы батча распознаются в нескольких потоках, поэтому
    # `+=` по счётчикам нужен под замком: иначе часть прибавок теряется и замер
    # врёт в свою пользу. Суммы времён теперь складываются ПО ПОТОКАМ и больше
    # не равны wall — это нормально, wall считается отдельно.
    lock = threading.Lock()

    def wrap(obj, name, key):
        orig = getattr(obj, name)
        if getattr(orig, "_perf_wrapped", False):
            return

        def w(*a, **kw):
            t = time.perf_counter()
            try:
                return orig(*a, **kw)
            finally:
                dt = time.perf_counter() - t
                with lock:
                    c[key + "_n"] += 1
                    c[key + "_s"] += dt

        w._perf_wrapped = True           # type: ignore[attr-defined]
        setattr(obj, name, w)

    wrap(TesseractOcrCliModel, "_perform_osd", "osd")
    wrap(TesseractOcrCliModel, "_run_tesseract", "tess")   # вместе с предобработкой
    wrap(opre, "preprocess_for_ocr", "clean")
    wrap(PyPdfiumPageBackend, "get_page_image", "render")
    return c


def collect(groups: str, use_llm: bool) -> dict:
    from docling.datamodel.settings import settings
    settings.debug.profile_pipeline_timings = True

    t_imp = time.perf_counter()
    from docling_dev import build_converter
    from docling_dev.converter import build_docx
    from docling_dev.highlight import highlight_suspicious
    import_s = time.perf_counter() - t_imp

    files = corpus.prepare(corpus.select(groups), "perf")
    if not files:
        raise SystemExit("корпус пуст: проверьте CONVERTER_CORPUS_DIR")

    sampler = _Sampler()
    t0 = time.perf_counter()
    conv = build_converter(ocr_preprocess=False)
    build_s = time.perf_counter() - t0
    counters = _instrument_ocr()

    out: dict[str, dict] = {"_meta": {
        "machine": machine_key(), "llm": use_llm,
        "import_s": round(import_s, 1), "build_converter_s": round(build_s, 2)}}

    for cf, pdf in files:
        for k in counters:
            counters[k] = 0 if k.endswith("_n") else 0.0
        docx = pdf.with_suffix(".perf.docx")
        with sampler as s:
            cpu0 = s.cpu_seconds()
            t = time.perf_counter()
            res = conv.convert(str(pdf))
            conv_s = time.perf_counter() - t
            dl = res.document
            sizes = {}
            for pn, pg in dl.pages.items():
                sz = getattr(pg, "size", None)
                if sz:
                    sizes[int(pn)] = (float(sz.width), float(sz.height))
            t = time.perf_counter()
            doc = build_docx(dl, sizes, ocr_reader=None, use_word_order=False,
                             ink_bold=False)
            docx_s = time.perf_counter() - t
            t = time.perf_counter()
            highlight_suspicious(doc)
            hl_s = time.perf_counter() - t
            llm_s, llm_fixed = 0.0, 0
            if use_llm:
                from docling_dev.llm_correct import correct_highlighted
                t = time.perf_counter()
                llm_fixed = correct_highlighted(doc, None)
                llm_s = time.perf_counter() - t
            t = time.perf_counter()
            doc.save(str(docx))
            save_s = time.perf_counter() - t
            cpu = s.cpu_seconds() - cpu0
            rss = s.peak_rss

        pages = max(len(sizes), 1)
        total = conv_s + docx_s + hl_s + llm_s + save_s
        row = {
            "group": cf.group, "pages": pages,
            "total_s": round(total, 1), "per_page_s": round(total / pages, 2),
            "docling_s": round(conv_s, 1), "build_docx_s": round(docx_s, 2),
            "highlight_s": round(hl_s, 2), "llm_s": round(llm_s, 1),
            "llm_fixed": llm_fixed, "save_s": round(save_s, 2),
            "cpu_s": round(cpu, 1), "peak_rss_mb": round(rss / 2 ** 20),
            "stages": {k: round(sum(v.times), 2)
                       for k, v in (res.timings or {}).items() if k in STAGES},
            "ocr_internals": {k: (round(v, 2) if k.endswith("_s") else int(v))
                              for k, v in counters.items()},
        }
        out[cf.stem] = row
        print(f"  {cf.stem[:40]:40} {pages:2}стр {total:6.1f}s "
              f"({row['per_page_s']:5.2f}s/стр)  CPU={cpu:6.1f}s  "
              f"RSS={row['peak_rss_mb']:5}МБ  ocr={row['stages'].get('ocr', 0):5.1f}s "
              f"табл={row['stages'].get('table_structure', 0):5.1f}s")
    return out


def _profile(rows: dict) -> str:
    """Профиль в процентах по стадиям — ловит перекос нагрузки."""
    agg: dict[str, float] = {}
    total = 0.0
    for stem, r in rows.items():
        if stem.startswith("_"):
            continue
        total += r["total_s"]
        for k, v in r["stages"].items():
            agg[k] = agg.get(k, 0.0) + v
        for k in ("build_docx_s", "highlight_s", "llm_s"):
            agg[k] = agg.get(k, 0.0) + r[k]
    if not total:
        return ""
    parts = sorted(agg.items(), key=lambda kv: -kv[1])[:6]
    return "  ".join(f"{k}={v / total * 100:.0f}%" for k, v in parts if v > 0)


def snapshot_path(args) -> Path:
    if args.snapshot:
        return Path(args.snapshot)
    tag = "llm" if args.llm else "nollm"
    return _HERE / "snapshots" / f"perf-{machine_key()}-{tag}.json"


def cmd_save(args) -> int:
    data = collect(args.groups, args.llm)
    snap = snapshot_path(args)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nпрофиль: {_profile(data)}")
    print(f"снимок записан: {snap}")
    return 0


def cmd_check(args) -> int:
    snap = snapshot_path(args)
    if not snap.is_file():
        raise SystemExit(f"снимка нет: {snap}. Снимите его: python tests/perf.py save")
    было = json.loads(snap.read_text(encoding="utf-8"))
    стало = collect(args.groups, args.llm)

    fails: list[str] = []
    print(f"\n{'файл':40}{'wall':>18}{'CPU-сек':>18}{'пик RSS, МБ':>18}")
    for stem, now in стало.items():
        if stem.startswith("_"):
            continue
        old = было.get(stem)
        if old is None:
            print(f"  {stem[:38]:38} нет в снимке — пропуск")
            continue
        ok_wall = now["total_s"] <= old["total_s"] * 1.25
        ok_cpu = now["cpu_s"] <= old["cpu_s"] * 1.10
        ok_rss = now["peak_rss_mb"] <= old["peak_rss_mb"] * 1.10

        def mark(ok):
            return "" if ok else " ✗"

        print(f"  {stem[:38]:38}{old['total_s']:7.1f}→{now['total_s']:6.1f}{mark(ok_wall):2}"
              f"{old['cpu_s']:8.1f}→{now['cpu_s']:6.1f}{mark(ok_cpu):2}"
              f"{old['peak_rss_mb']:8}→{now['peak_rss_mb']:6}{mark(ok_rss)}")
        if not ok_wall:
            fails.append(f"{stem}: время {old['total_s']:.1f}s → {now['total_s']:.1f}s "
                         f"(рост >25%)")
        if not ok_cpu:
            fails.append(f"{stem}: CPU-секунды {old['cpu_s']:.1f} → {now['cpu_s']:.1f} "
                         f"(рост >10%)")
        if not ok_rss:
            fails.append(f"{stem}: пиковая RSS {old['peak_rss_mb']} → "
                         f"{now['peak_rss_mb']} МБ (рост >10%)")
    print(f"\nпрофиль было: {_profile(было)}")
    print(f"профиль стало: {_profile(стало)}")
    if было.get("_meta", {}).get("machine") != стало["_meta"]["machine"]:
        print(f"  ВНИМАНИЕ: снимок снят на другой машине "
              f"({было.get('_meta', {}).get('machine')}) — числа несравнимы")
    if fails:
        print("\nРЕГРЕСС:")
        for f in fails:
            print(f"  ✗ {f}")
        return 1
    print("\nскорость и ресурсы в допусках")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Снимок скорости и ресурсов (фаза 0)")
    p.add_argument("action", choices=("save", "check"))
    p.add_argument("--groups", default="scans,dirty")
    p.add_argument("--llm", action="store_true",
                   help="включить LLM-доочистку (отдельный снимок, большой разброс)")
    p.add_argument("--snapshot", default="")
    args = p.parse_args()
    return cmd_save(args) if args.action == "save" else cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
