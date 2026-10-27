"""Локальная LLM-доочистка подсвеченных (не исправленных детерминированно) слов.

ПОЛНОСТЬЮ ЛОКАЛЬНО, БЕЗ ОТДЕЛЬНЫХ ПРОГРАММ:
  • движок — pip-пакет `llama-cpp-python` (вендорится в git, как torch);
  • модель — файл `.gguf` в папке `models/` (тоже в git);
  • инференс идёт В САМОМ процессе Python — ни сети, ни службы, ни установки
    на целевом ПК (работает в закрытой корпоративной сети офлайн).
  • ОПЦИОНАЛЬНО: если пакета или файла модели нет — модуль ничего не делает
    (no-op), программа продолжает работать как обычно (автоочистка + подсветка).

Обрабатываются ТОЛЬКО уже подсвеченные жёлтым слова (остаток после
детерминированной автоочистки) — это ~10 коротких фрагментов на документ,
поэтому быстро даже на CPU. Жёсткие предохранители не дают модели менять
цифры/номера и «галлюцинировать» (подменять слово другим).
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil

from docx.enum.text import WD_COLOR_INDEX

from .highlight import _iter_paragraphs

log = logging.getLogger(__name__)

# Модель по умолчанию — вендоренный .gguf в папке models/ рядом с проектом.
_DEFAULT_MODEL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models", "qwen2.5-1.5b-instruct-q4_k_m.gguf",
)

# Настройки стоимости LLM-доочистки (см. handoff.md, правка п.1.3):
#   CONVERTER_LLM_THREADS — потоки префилла. Тяжёлый префилл идёт по
#     n_threads_batch, а llama-cpp по умолчанию берёт cpu_count() (все ядра,
#     включая логические). Замер: 16 → 5.1 s / 70 CPU-с, 8 → 7.3 s / 57,
#     4 → 13.0 s / 50. Дефолт оставлен прежним: это ручка для слабых ПК.
#   CONVERTER_LLM_UNLOAD — выгружать модель после документа. Освобождает ~2.2 ГБ
#     RSS, но возвращает ~70 CPU-с префилла префикса на каждый документ, поэтому
#     по умолчанию выключено (в сервере модель живёт долго).
#   CONVERTER_LLM_CTX — сколько символов контекста уходит в запрос.
def _env_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        log.warning("LLM: %s=%r — не целое число, беру значение по умолчанию", name, raw)
        return default
    return v if v > 0 else default


_THREADS_BATCH = _env_int("CONVERTER_LLM_THREADS", None)
_CTX_CHARS     = _env_int("CONVERTER_LLM_CTX", 600) or 600


def _unload_after() -> bool:
    """Выгружать ли модель после задачи.

    Модель 1.5B Q4 держит ~1.6 ГБ пика (замер фазы 0), поэтому на слабой машине
    её надо отдавать сразу, а на мощной — держать: повторная загрузка стоит
    секунды. Решает профиль (пункт 3.2), `CONVERTER_LLM_UNLOAD` перебивает.
    """
    raw = os.environ.get("CONVERTER_LLM_UNLOAD", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    try:
        from .profiles import resolve
        return resolve().llm_unload
    except Exception:                               # noqa: BLE001
        return False

_DIGIT_RE    = re.compile(r"\d")
_DIGIT_ONLY  = re.compile(r"^\d[\d\s.,\-/]*$")   # токен почти целиком из цифр
_HAS_CYR     = re.compile(r"[А-Яа-яЁё]")
_ROMAN_RE    = re.compile(r"^[IVXLCDM]{1,7}$", re.IGNORECASE)
_HAS_DIGIT   = re.compile(r"\d")

# Системный промпт: явно перечисляем типичные OCR-путаницы.
# Это работает для любого типа документа (не только юридического).
_SYSTEM = (
    "Ты исправляешь ошибки распознавания (OCR) в русском тексте любого типа. "
    "В ответе — ТОЛЬКО исправленный фрагмент (1–3 слова), без кавычек и пояснений. "
    "Типичные OCR-ошибки в русском: "
    "кириллица ↔ латиница (о↔o/O, е↔e, с↔c, а↔a, р↔p, н↔n, и↔u, х↔x); "
    "буквы ↔ цифры (I/l↔1, O/о↔0, Z↔2); "
    "потерянные или лишние буквы; перепутанный регистр. "
    "Не меняй цифры 0-9 и числовые значения. "
    "Если фрагмент не ошибка или не можешь исправить — повтори его без изменений."
)

# Few-shot: примеры охватывают разные типы OCR-ошибок для любых документов.
_FEWSHOT = [
    # Путаница регистра внутри слова
    ("Предложение: «двадцать три рубля, в тоM числе проценты»\n"
     "Искажённый фрагмент: «тоM»\nИсправление:", "том"),
    # Одиночная заглавная буква — предлог
    ("Предложение: «осуществляющему деятельность пО возврату просроченной задолженности»\n"
     "Искажённый фрагмент: «пО»\nИсправление:", "по"),
    # Кириллица/латиница: о↔u в слове
    ("Предложение: «действует в сuuтветствии со статьёй 309 ГК РФ»\n"
     "Искажённый фрагмент: «сuuтветствии»\nИсправление:", "соответствии"),
    # Кириллица/латиница: латинская r в начале слова (r ≈ т/г в сканах)
    ("Предложение: «нет rакого права у кредитора обжаловать»\n"
     "Искажённый фрагмент: «rакого»\nИсправление:", "такого"),
    # Кириллица/латиница: «р» — латинская r вместо «г.» (город)
    ("Предложение: «344002, r. Ростов-на-Дону, ул. Станиславского»\n"
     "Искажённый фрагмент: «r.»\nИсправление:", "г."),
    # OCR-мусор: бессмысленный токен оставляем как есть (guardrail-пример)
    ("Предложение: «включая услуги wlw и сбор информации»\n"
     "Искажённый фрагмент: «wlw»\nИсправление:", "wlw"),
    # Путаница знаков: N' / Ne → №
    ("Предложение: «Договором уступки права требованияNе 14-02-25 от 2025 года»\n"
     "Искажённый фрагмент: «требованияNе»\nИсправление:", "требования №"),
    # Пропущенная буква / перестановка в чисто-кириллическом слове
    ("Предложение: «сумма Крецитного долга составляет 873 306 руб.»\n"
     "Искажённый фрагмент: «Крецитного»\nИсправление:", "кредитного"),
    # Буква-цифра: лишняя прописная (не трогаем цифры — только букву)
    ("Предложение: «согласно положениям ст.ст. 432, 435 и 438 ГК РФ»\n"
     "Искажённый фрагмент: «ст.ст.»\nИсправление:", "ст.ст."),
    # Смешанные скрипты: заглавная латинская H вместо Н внутри слова
    ("Предложение: «Банк оH не подтвердил задолженность должника»\n"
     "Искажённый фрагмент: «оH»\nИсправление:", "он"),
]

_llm = None          # ленивый singleton модели (грузим один раз на весь батч)
_llm_failed = False


def _cleanup_llm() -> None:
    """Явно освобождает модель ПОКА интерпретатор жив. Без этого деструктор
    llama_cpp.Llama.__del__ срабатывает уже на завершении интерпретатора, когда
    ctypes-указатель free_model обнулён → «TypeError: 'NoneType' object is not
    callable» (безвредный, но пугающий трейсбек после «Готово»)."""
    global _llm
    if _llm is not None:
        try:
            _llm.close()
        except Exception:
            pass
        _llm = None


import atexit as _atexit
_atexit.register(_cleanup_llm)


def _resolve_model(model_path: str | None) -> str | None:
    """Путь к .gguf. Если собранного файла нет, но рядом лежат куски
    `<имя>.gguf.partNNN` (модель режут на <100 МБ ради лимита GitHub) —
    склеиваем их один раз в полный файл и далее используем его."""
    p = model_path or _DEFAULT_MODEL
    if os.path.isfile(p):
        return p
    parts = sorted(glob.glob(p + ".part*"))
    if parts:
        try:
            log.info("LLM: собираю модель из %d кусков…", len(parts))
            with open(p, "wb") as out:
                for part in parts:
                    with open(part, "rb") as f:
                        shutil.copyfileobj(f, out, 1024 * 1024)
            return p
        except Exception as exc:
            log.warning("LLM: не удалось собрать модель из кусков: %s", exc)
            try:
                if os.path.isfile(p):
                    os.remove(p)              # убираем частично записанный файл
            except OSError:
                pass
    return None


def llm_available(model_path: str | None = None) -> bool:
    """True если есть и пакет llama-cpp-python, и файл модели."""
    if _resolve_model(model_path) is None:
        return False
    try:
        import llama_cpp  # noqa: F401
        return True
    except Exception:
        return False


def _get_llm(model_path: str):
    global _llm, _llm_failed
    if _llm is None and not _llm_failed:
        try:
            from llama_cpp import Llama
            threads = _THREADS_BATCH or os.cpu_count() or 4
            _llm = Llama(model_path=model_path, n_ctx=2048,
                         n_threads=threads, n_threads_batch=threads,
                         verbose=False)
            log.info("LLM: модель загружена (%s)", os.path.basename(model_path))
        except Exception as exc:
            _llm_failed = True
            log.warning("LLM: не удалось загрузить модель: %s", exc)
    return _llm


def _ask(llm, word: str, ctx: str) -> str | None:
    try:
        msgs = [{"role": "system", "content": _SYSTEM}]
        for u, a in _FEWSHOT:                 # few-shot примеры
            msgs.append({"role": "user", "content": u})
            msgs.append({"role": "assistant", "content": a})
        msgs.append({"role": "user", "content":
                     f"Предложение: «{ctx}»\n"
                     f"Искажённый фрагмент: «{word}»\nИсправление:"})
        out = llm.create_chat_completion(messages=msgs, temperature=0.0,
                                         max_tokens=16)
        return (out["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        log.debug("LLM: запрос не удался: %s", exc)
        return None


def _digits(s: str) -> str:
    return "".join(_DIGIT_RE.findall(s))


def _edit_distance(a: str, b: str) -> int:
    """Расстояние Левенштейна (для сравнения исходного и исправленного слова)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_CYR_PART_RE = re.compile(r"[А-Яа-яЁё]{2,}")


def _is_real_word(word: str) -> bool:
    """Есть ли слово в словаре (pymorphy) или в доменном списке.

    Без словаря всё считаем словом — иначе на машине без pymorphy доочистка
    молча перестала бы работать.
    """
    try:
        from .highlight import _domain_known, _pymorphy_known, _morph
    except Exception:                        # noqa: BLE001
        return True
    if _morph is None:
        return True
    low = word.lower()
    return _pymorphy_known(low) or _domain_known(low)


def _result_is_words(cand: str) -> bool:
    """Все кириллические части правки — существующие слова.

    Главный предохранитель от «правок», которые портят текст: замер показал, что
    модель 1.5B охотно выдаёт «Задолженпость» → «Задолжность», «пюридических» →
    «правических», «КАРАГЕЗОВОЙ» → «Карагеевой». Все такие ответы — не слова, и
    отличить их от верных правок («Крецитного» → «кредитного») можно словарём:
    на 19 верных правках замера условие проходит целиком.
    """
    parts = _CYR_PART_RE.findall(cand)
    if not parts:
        return True                          # правка без кириллицы («№») — не нам судить
    return all(_is_real_word(p) for p in parts)


def _prefilter(orig: str) -> bool:
    """Можно ли вообще получить принимаемую правку для этого токена.

    Те же запреты, что в `_accept`, но зависящие ТОЛЬКО от исходного слова —
    значит проверяются ДО запроса к модели. Запрос к 1.5B стоит 0.4-0.7 s и
    6-9 CPU-с, поэтому не спрашиваем про то, что отвергнем при любом ответе.
    """
    if not orig:
        return False
    if _ROMAN_RE.match(orig):
        return False                         # римские цифры - не трогаем
    if _DIGIT_ONLY.match(orig):
        return False                         # почти-цифровой токен (42, 3.5, 10-11)
    if not _HAS_CYR.search(orig):
        return False                         # нет кириллицы в исходнике - не правим
    return True


def _accept(orig: str, cand: str | None) -> str | None:
    """Предохранители: принимаем правку, только если она безопасна и МЕЛКАЯ."""
    if not cand:
        return None
    cand = cand.splitlines()[0].strip().strip("«»\"'.,;:()")
    if not cand or cand == orig:
        return None
    if not _prefilter(orig):
        return None                          # римские/цифровые/без кириллицы
    if "\t" in cand or len(cand.split()) > 3:  # допускаем 1–3 слова (требования №)
        return None
    if _digits(cand) != _digits(orig):       # цифры менять запрещено
        return None
    if not _result_is_words(cand):           # правка должна быть словом, а не новым мусором
        return None
    # Для слов, содержащих цифры, порог жёстче: только 1 замена (очепятка рядом с числом)
    # Пример: «8I72361» содержит цифры, «Крецитного» — нет.
    if _HAS_DIGIT.search(orig):
        if _edit_distance(orig.lower(), cand.lower()) > 1:
            return None
    elif _edit_distance(orig.lower(), cand.lower()) > max(2, len(orig) // 3):
        # Для чисто-буквенных слов — прежний порог (МЕЛКАЯ правка).
        return None
    return cand


def correct_highlighted(doc, model_path: str | None = None) -> int:
    """Прогоняет подсвеченные слова через локальную модель; что удалось безопасно
    исправить — заменяет и снимает подсветку. Возвращает число исправленных."""
    resolved = _resolve_model(model_path)
    if resolved is None:
        log.info("LLM-доочистка: модель не найдена (%s) — пропуск",
                 model_path or _DEFAULT_MODEL)
        return 0
    llm = _get_llm(resolved)
    if llm is None:
        return 0

    # Строим список параграфов с текстом для оконного контекста
    all_paras = list(_iter_paragraphs(doc))
    para_texts = [p.text for p in all_paras]

    fixed = 0
    asked = reused = prefiltered = 0
    answers: dict[str, str | None] = {}      # кэш ответов модели в пределах документа
    for idx, para in enumerate(all_paras):
        # «Оконный» контекст: текущий абзац + соседние (по 1 с каждой стороны).
        # Маленькая модель (1.5B) — ограничиваем длиной _CTX_CHARS (CONVERTER_LLM_CTX).
        prev_txt = para_texts[idx - 1][:150] if idx > 0 else ""
        next_txt = para_texts[idx + 1][:150] if idx + 1 < len(all_paras) else ""
        ctx_window = " … ".join(t for t in [prev_txt, para.text, next_txt] if t)
        ctx = ctx_window[:_CTX_CHARS]

        for run in para.runs:
            if run.font.highlight_color != WD_COLOR_INDEX.YELLOW:
                continue
            word = run.text.strip()
            if not word:
                continue
            if not _prefilter(word):
                prefiltered += 1
                continue
            if word in answers:
                # Один и тот же искажённый токен («пО», «оH») встречается в
                # документе многократно, а правка для него от контекста не
                # зависит: ответ модели переиспользуем, предохранители
                # `_accept` всё равно прогоняются на каждом вхождении.
                raw = answers[word]
                reused += 1
            else:
                raw = _ask(llm, word, ctx)
                answers[word] = raw
                asked += 1
            cand = _accept(word, raw)
            if cand:
                run.text = run.text.replace(word, cand)
                run.font.highlight_color = None
                fixed += 1
                log.info("LLM: %r → %r", word, cand)
    log.info("LLM-доочистка: исправлено %d фрагментов; запросов к модели %d "
             "(переиспользовано ответов %d, отсеяно до запроса %d)",
             fixed, asked, reused, prefiltered)
    if _unload_after():
        _cleanup_llm()
        log.info("LLM: модель выгружена — освободили ~1.6 ГБ")
    return fixed
