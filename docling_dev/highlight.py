"""Подсветка «подозрительных» (вероятно искажённых OCR) слов в DOCX.

Детерминированно, без LLM/GPU: помечаем то, что безопасно автоматически НЕ
исправить, чтобы человек-проверяющий сразу видел сомнительные места.

Эвристики «подозрительного» слова (низкий процент ложных срабатываний):
  1) смешанный регистр-внутри-слова: строчная, затем заглавная («должНЫ»,
     «тысЯЧ», «ПОЧтОВЫм») — типичный OCR-капс-шум;
  2) смешанные алфавиты в одном слове (кириллица + латиница: «Ng», «д0говор»);
  3) одиночный латинский токен (≤5 букв) среди кириллицы и без цифр
     («co», «CO», «CT», «Bank», «Ne», «Hi») — почти всегда гомоглиф;
  4) остаточные кавычки-гомоглифы, прилипшие к слову («Коллектэ», «банкэ»);
  5) если доступна pymorphy3/pymorphy2: слово ≥6 букв из чистой кириллицы, не
     распознанное морфологически («нанченования», «Крецитного» и т.п.).
Числа, ИНН/коды (с цифрами), email/URL не помечаются.

Часть помеченных слов чинится ДЕТЕРМИНИРОВАННО ещё до подсветки (_autofix_word):
латиница→кириллица, регистр, и словарный спелл-фикс кириллических OCR-путаниц
(_try_cyr_spell_fix: н↔и, с↔е, о↔а … — берём правку только при ЕДИНСТВЕННОМ
словарном кандидате, иначе слово уходит на подсветку/LLM).
"""
from __future__ import annotations

import copy
import logging
import re
from functools import lru_cache

from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

log = logging.getLogger(__name__)

# ── Опциональный морфологический словарь (pymorphy3, фолбэк pymorphy2) ────────
# Если пакет не установлен — продолжаем без него (только эвристики).
try:
    import pymorphy3 as _pm2
    _morph = _pm2.MorphAnalyzer()
    log.debug("highlight: pymorphy3 доступен — включена словарная проверка")
except Exception:
    try:
        import pymorphy2 as _pm2
        _morph = _pm2.MorphAnalyzer()
        log.debug("highlight: pymorphy2 доступен — включена словарная проверка")
    except Exception:
        _morph = None

_CYR = "А-Яа-яЁё"
_LAT = "A-Za-z"

# Токен = последовательность букв/цифр/дефисов/подчёркиваний/собак/точек внутри.
_TOKEN_RE = re.compile(r"[^\s]+")

_CAPS_MID_RE    = re.compile(rf"[{_CYR}]*[а-яё][А-ЯЁ][{_CYR}]*")   # строчная→заглавная
_HAS_CYR        = re.compile(rf"[{_CYR}]")
_HAS_LAT        = re.compile(rf"[{_LAT}]")
_HAS_DIGIT      = re.compile(r"\d")
# Был {1,3} — расширён до {1,5}: «Hi», «Ne», «Bank», «Name» тоже гомоглифы
_PURE_LAT_SHORT = re.compile(rf"^[{_LAT}]{{1,5}}$")
_URL_EMAIL_RE   = re.compile(r"[@./]|https?|www|\.ru|\.com", re.IGNORECASE)
# «слово» с прилипшим гомоглифом-кавычкой на конце (э/ж/х/» после строчной буквы)
_TRAIL_QUOTE_RE = re.compile(rf"[{_CYR}]{{3,}}[эжхъ]$")

_STRIP = " \t.,;:!?()«»\"'–—-"

# Аббревиатуры и ALL-CAPS токены ≤6 — ИНН, ОГРН, РФ, АО — не проверяем словарём.
_ABBREV_RE = re.compile(r"^[А-ЯЁ]{2,6}$")

# Доменные юр./фин. термины, которых НЕТ в словаре pymorphy, но они ВЕРНЫЕ.
# Без этого списка «взыскателя» чинилось бы на «изыскателя», «займодавец» на
# архаичное «заимодавец», а правильные «микрофинансовый»/«коллекторская»
# подсвечивались бы как ошибки. Сравниваем по началу слова (стем), без регистра.
_DOMAIN_OK_STEMS: tuple[str, ...] = (
    "взыскател", "займодав", "заимодав", "залогодержател", "залогодател",
    "цессион", "цедент", "цессионар", "микрофинанс", "коллекторск",
    "созаёмщик", "созаемщик", "поручительств", "неустойк", "потребительск",
    "правоустанавлива", "правопреемник", "правопреемств", "реструктуризац",
    "досудебн", "внесудебн", "подведомственн", "подсудн",
    # пополнение по разметке корпуса (п.1.4): верные слова, которых нет в pymorphy
    "самозанят", "госрег",
)


def _domain_known(word: str) -> bool:
    """True если слово — известный доменный термин (нет в pymorphy, но верный)."""
    w = word.lower()
    return any(w.startswith(s) for s in _DOMAIN_OK_STEMS)

# Кластеры визуально похожих кириллических букв — OCR путает их между собой.
# Пары ДВУНАПРАВЛЕННЫЕ: «и↔н» порождает и подстановку и→н, и обратную н→и.
# Только высоко-визуальные путаницы (мин. ложных): вертикальные штрихи (и/н/п/й/м),
# открытые дуги (с/е/о), диакритика (е/ё, и/й), хвосты (ц/и), засечки (г/т).
_CYR_OCR_PAIRS: tuple[tuple[str, str], ...] = (
    ("и", "н"), ("н", "п"), ("и", "й"), ("н", "й"), ("н", "м"),
    ("с", "е"), ("о", "а"), ("о", "с"), ("е", "ё"),
    ("ц", "и"), ("л", "п"), ("в", "н"), ("г", "т"), ("ь", "ы"),
    # Пункт 1.Б.4: пары добавлены по замеру на 155 размеченных словах корпуса.
    # Каждая доказала пользу: «правонарущения»→«правонарушения» (ш/щ),
    # «гофударственной»→«государственной» и «изложенногф»→«изложенного» (ф/о),
    # «налогоплателыциков»→«налогоплательщиков» (ц/щ вместе с ь/ы),
    # «порялка»→«порядка» и «расхолы»→«расходы» (л/д).
    # Больше пар НЕ добавлять без замера: лишние двойники делают кандидатов
    # неоднозначными, и слово вместо починки уходит на подсветку. Так отвергнуты
    # р/в (ломала «васстановлен»→«восстановлен»), я/л, б/ь, з/э, д/ц, т/г, х/к
    # (пользы на корпусе не дали) и любые вставки/удаления/перестановки
    # (точность 58%: «авляющего»→«валяющего», «порялка»→«прялка»).
    ("ш", "щ"), ("ф", "о"), ("ц", "щ"), ("л", "д"),
)
# Разворачиваем пары в словарь: буква → кортеж её визуальных двойников.
_CYR_CONFUSIONS: dict[str, list[str]] = {}
for _a, _b in _CYR_OCR_PAIRS:
    _CYR_CONFUSIONS.setdefault(_a, []).append(_b)
    _CYR_CONFUSIONS.setdefault(_b, []).append(_a)

_CYR_ONLY      = re.compile(r"^[а-яё]+$")
_SPELL_MIN_LEN = 5       # слова короче — не трогаем (риск исказить «дом», «код»)
_SPELL_DEPTH   = 2       # максимум исправляемых ошибок на слово (1–2)
_SPELL_BUDGET  = 6000    # потолок перебора: слишком ветвистое слово → подсветка


@lru_cache(maxsize=20000)
def _pymorphy_known(word: str) -> bool:
    """True если pymorphy знает хотя бы одно осмысленное прочтение слова.

    Кэшируется: BFS-перебор спелл-фиксера проверяет одни и те же кандидаты
    многократно, а словарный разбор — самая дорогая операция в модуле.
    """
    if _morph is None:
        return True          # без словаря считаем все слова «известными»
    parses = _morph.parse(word.lower())
    # pymorphy3: is_known=True → слово в словаре
    # pymorphy2: UNKN POS → не в словаре (fallback)
    if hasattr(parses[0], "is_known"):
        return any(p.is_known for p in parses)
    return any(p.tag.POS not in (None, "UNKN") for p in parses)


# ── Документные правила «это не ошибка» (п.1.4) ──────────────────────────────
# Разметка корпуса (155 различных подсвеченных слов: 110 искажений, 45 верных)
# показала, что ВСЕ ложные срабатывания дают только два правила — словарная
# проверка и короткая латиница. Поэтому исключения применяются лишь к ним;
# структурные признаки (смешанные алфавиты, капс в середине слова) безусловны.
#
# Исключения выводятся из самого документа, а не из рукописного списка компаний:
# в корпусе каждая организация либо в кавычках, либо после правовой формы,
# каждый топоним — рядом с «ул./город», каждая фамилия — рядом с отчеством или
# инициалами. Список из двадцати названий пришлось бы вечно пополнять, а эти
# правила переносятся на новые документы.
_LEGAL_FORMS = {
    "ооо", "оао", "зао", "пао", "ао", "нао", "ип", "мкк", "мфк", "мфо", "пко",
    "пкб", "сро", "аау", "нпф", "кб", "бки", "тсж", "гуп", "муп", "фгуп",
    "ано", "нко", "кпк", "скпк", "банк",
}
_GEO_MARKERS = {
    "ул", "улица", "пер", "переулок", "просп", "проспект", "пр-кт", "пл",
    "площадь", "б-р", "бульвар", "наб", "набережная", "ш", "шоссе", "мкр",
    "микрорайон", "р-н", "район", "ст-ца", "станица", "г", "гор", "город",
    "с", "село", "пос", "поселок", "посёлок", "х", "хутор", "д", "деревня",
    "обл", "область", "край", "тер",
}
# Отчество опознаём по окончанию — это надёжнее любого словаря имён.
_PATR_SUFFIXES = (
    "ович", "овича", "овичу", "овичем", "овиче",
    "евич", "евича", "евичу", "евичем", "евиче",
    "ьевич", "ьевича", "ьевичу",
    "овна", "овны", "овне", "овну", "овной",
    "евна", "евны", "евне", "евну", "евной",
    "ична", "ичны", "ичне", "ичну", "ичной",
)
_INITIALS_RE = re.compile(r"^[А-ЯЁ]\.(?:[А-ЯЁ]\.?)?$")
_QUOTED_RE = re.compile("[«\"„]([^»\"“]{2,80})[»\"“]")
_URL_FULL_RE = re.compile(
    r"[\w.-]+@[\w.-]+|https?://\S+"
    r"|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",      # nalog.gov.ru, www.nalog.gov.Tu
    re.IGNORECASE)
# В скобках чисто латинская фраза — название бренда: «(One Click Money)».
_LAT_PAREN_RE = re.compile(rf"\(\s*((?:[{_LAT}][{_LAT}0-9]*[ ,.&-]*){{2,6}})\)")
_WORDISH_RE = re.compile(rf"[{_CYR}{_LAT}]{{2,}}")
_NEAR_MAX_DIST = 2       # «росроченная» → «просроченная» = 1, «задолженнофть» = 1


# Внутренняя грязь: кавычка, запятая, апостроф ВНУТРИ слова — дефект склейки
# («РСФСР‚паспорт», «обл„Шахты»), никакое имя так не выглядит.
_INNER_JUNK_RE = re.compile(r"[\w][«»„“”‚’'\",;:!?()\[\]{}/\\|<>*%#$^&~`+=][\w]")
# «й» распознана как «и»: русских слов на «-ои»/«-еи» практически нет, а
# «Ташкентскои», «Азовскии», «Россиискои» — типичная ошибка OCR.
_II_TAIL_RE = re.compile(r"(?:ои|еи|ии)$", re.IGNORECASE)


def _is_patronymic(token: str) -> bool:
    low = token.lower()
    return len(low) >= 6 and low.endswith(_PATR_SUFFIXES)


@lru_cache(maxsize=20000)
def _lemma(word: str) -> str:
    """Начальная форма по pymorphy (для незнакомых слов — предсказанная).

    Нужна, чтобы «КАРАГЕЗОВА», «КАРАГЕЗОВОЙ» и «КАРАГЕЗОВУ» считались одним
    именем: иначе каждая словоформа выглядит одиночной и доказательств, что это
    имя, не набирается.
    """
    if _morph is None:
        return word.lower()
    parses = _morph.parse(word.lower())
    return parses[0].normal_form if parses else word.lower()


def _edit_distance_le(a: str, b: str, limit: int) -> bool:
    """Расстояние Левенштейна ≤ limit, с ранним выходом (вызывается часто)."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def _hyphen_parts_known(core: str) -> bool:
    """Составное слово, все части которого словарные: «купли-продажи».
    pymorphy таких склеек не знает, хотя каждую половину знает."""
    if "-" not in core:
        return False
    parts = [p for p in core.split("-") if len(p) >= 2]
    if len(parts) < 2:
        return False
    return all(_pymorphy_known(p) or _ABBREV_RE.match(p) or _domain_known(p)
               for p in parts)


class DocContext:
    """Сведения обо ВСЁМ документе: что в нём повторяется и что названо именем.

    Нужен потому, что по одному слову фамилию от искажения не отличить: pymorphy
    предсказывает метку `Surn` и для «Геворгян», и для «Россйн» (замерено). А вот
    «встречается 6 раз и ни на одно словарное слово документа не похоже» —
    признак имени, а не ошибки OCR, потому что ошибки OCR случайны.
    """

    __slots__ = ("counts", "excused", "vetoed")

    def __init__(self, counts: dict, excused: dict, vetoed: dict | None = None):
        self.counts = counts
        self.excused = excused       # токен → имя правила, снявшего подозрение
        self.vetoed = vetoed or {}   # токен → почему исключение не применилось

    def is_excused(self, core: str) -> bool:
        return core in self.excused

    def reason(self, core: str) -> str:
        return self.excused.get(core, "")


def _doc_tokens(text: str) -> list:
    return [t.strip(_STRIP) for t in text.split()]


def build_doc_context(texts: list) -> DocContext:
    """Собирает контекст документа по текстам абзацев (до правок подсветки)."""
    counts: dict = {}
    for text in texts:
        for tok in _doc_tokens(text):
            if tok:
                counts[tok] = counts.get(tok, 0) + 1

    # Токены, похожие друг на друга: «Коллекши» при наличии «Коллекшн»,
    # «Руснитерфинанс» при «Русинтерфинанс», «Сберданк» при «Сбербанк» — это
    # искажение имени, а не имя. Такое исключать нельзя, иначе проверяющий
    # никогда не увидит, что фамилия кредитора написана с ошибкой.
    def _twin_of(tok: str) -> str:
        """Почти такой же токен документа с ДРУГОЙ леммой, если он есть.

        Направление ошибки определить нельзя: «Коллекшн»/«Коллекши» и
        «Русинтерфинанс»/«Руснитерфинанс» встречаются по одному разу, и ни одного
        из них нет в словаре. Поэтому подсвечиваем ОБА — пусть проверяющий
        сравнит сам: пропустить искажённое имя кредитора хуже, чем подсветить
        верное.
        """
        low = tok.lower()
        for other in counts:
            if other == tok or len(other) < 4:
                continue
            ol = other.lower()
            if ol == low or not _edit_distance_le(low, ol, _NEAR_MAX_DIST):
                continue
            if _lemma(ol) == _lemma(low):
                continue                     # словоформы одного слова, не ошибка
            # Двойник должен начинаться так же: иначе в двойники попадает любое
            # случайно похожее слово («Займер» ↔ «размер» — расстояние 2).
            if low[:3] != ol[:3]:
                continue
            # Если двойник ЕСТЬ В СЛОВАРЕ, он, скорее всего, обычное слово, а не
            # искажение имени («Мирский» ↔ «мирской»): такой паре вето не нужно.
            if _pymorphy_known(ol):
                continue
            return other
        return ""

    def _veto(token: str) -> str:
        """Причина, по которой исключения не действуют ни по какому правилу."""
        if _INNER_JUNK_RE.search(token):
            return "склейка через мусорный символ"
        if (_HAS_CYR.search(token) and not _pymorphy_known(token.lower())
                and _II_TAIL_RE.search(token)):
            return "окончание «-ои/-еи/-ии» (й прочитана как и)"
        twin = _twin_of(token)
        return f"есть почти такой же токен «{twin}»" if twin else ""

    # Сколько раз документ называет это имя во ВСЕХ словоформах. Маркер рядом
    # («ул.», «ООО», отчество) сам по себе ничего не доказывает: «ул. Барданиул»
    # и «ул. Неглинная» для программы выглядят одинаково. А вот имя, названное
    # в документе хотя бы дважды, — это имя: ошибки OCR не повторяются.
    lemma_counts: dict = {}
    for tok, n in counts.items():
        if tok:
            lem = _lemma(tok)
            lemma_counts[lem] = lemma_counts.get(lem, 0) + n

    def _named_twice(token: str) -> bool:
        return lemma_counts.get(_lemma(token), 0) >= 2

    excused: dict = {}
    vetoed: dict = {}

    def mark(token: str, rule: str) -> None:
        if not token or token in excused:
            return
        why = _veto(token)
        if why:
            vetoed.setdefault(token, why)
            return
        excused[token] = rule

    for text in texts:
        # 1. Всё в кавычках — название: «Быстроденьги», «ДЗП-Единый», «Займер».
        for m in _QUOTED_RE.finditer(text):
            for t in _WORDISH_RE.findall(m.group(1)):
                mark(t, "кавычки")
        # 2. Куски URL и e-mail: nalog.gov.ru даёт токены nalog / gov / ru.
        for m in _URL_FULL_RE.finditer(text):
            for t in _WORDISH_RE.findall(m.group(0)):
                mark(t, "url")
        # 2а. Латинское название в скобках: «ООО МКК «УФ» (One Click Money)».
        for m in _LAT_PAREN_RE.finditer(text):
            for t in _WORDISH_RE.findall(m.group(1)):
                mark(t, "латинское название")
        # 3. Соседи маркеров: «ул. Неглинная», «АО МКК Займ-Экспресс»,
        #    «КАРАГЕЗОВА ЛИЛИЯ АРКАДЬЕВНА» (у отчества и инициалов окно шире,
        #    потому что фамилия стоит через имя).
        toks = _doc_tokens(text)
        # «www nalog gov ru» — точки потеряны при распознавании; всё латинское
        # после «www» до первого кириллического слова принадлежит адресу.
        for i, tok in enumerate(toks):
            if tok.lower().strip(".") != "www":
                continue
            for nb in toks[i + 1:i + 5]:
                if not nb or not _HAS_LAT.search(nb) or _HAS_CYR.search(nb):
                    break
                mark(nb, "url")
        for i, tok in enumerate(toks):
            low = tok.lower().rstrip(".")
            if low in _GEO_MARKERS:
                window, rule = 1, "топоним"
            elif low in _LEGAL_FORMS:
                window, rule = 1, "организация"
            elif _is_patronymic(tok) or _INITIALS_RE.match(tok):
                window, rule = 2, "фио"
            else:
                continue
            for j in range(max(0, i - window), min(len(toks), i + window + 1)):
                nb = toks[j]
                # Только кириллица: латинский обрывок рядом с инициалами
                # («MHI», «Kum») — мусор OCR, а не имя.
                if (j != i and nb[:1].isupper() and _HAS_CYR.search(nb)
                        and not _HAS_LAT.search(nb) and _named_twice(nb)):
                    mark(nb, rule)

    # 4. Повторяющийся токен — скорее имя, чем ошибка. Оговорка ОБЯЗАТЕЛЬНА:
    #    если в документе есть ПОХОЖЕЕ словарное слово, это систематическая
    #    ошибка OCR («росроченная» при наличии «просроченной», «задолженнофть»
    #    при «задолженности»), и подсветку снимать нельзя.
    known_doc = [t.lower() for t in counts
                 if len(t) >= 5 and _pymorphy_known(t.lower())]
    for tok, n in counts.items():
        if n < 2 or len(tok) < 5 or tok in excused:
            continue
        low = tok.lower()
        if _pymorphy_known(low):
            continue
        if not any(_edit_distance_le(low, kw, _NEAR_MAX_DIST) for kw in known_doc):
            mark(tok, "повтор")
    return DocContext(counts, excused, vetoed)


def _is_suspicious(core: str, ctx=None) -> bool:
    if not core:
        return False
    if _HAS_DIGIT.search(core):
        return False                      # коды/числа/ИНН — не трогаем
    if _URL_EMAIL_RE.search(core):
        return False                      # email/URL — не трогаем
    has_cyr = bool(_HAS_CYR.search(core))
    has_lat = bool(_HAS_LAT.search(core))
    # 2) смешанные алфавиты
    if has_cyr and has_lat:
        return True
    # 3) короткий (≤5 букв) чисто-латинский токен (гомоглиф среди кириллицы)
    if has_lat and not has_cyr and _PURE_LAT_SHORT.match(core):
        # кусок URL или латинское название («One Click Money») — не ошибка
        return not (ctx and ctx.is_excused(core))
    # 1) капс в середине слова
    if has_cyr and _CAPS_MID_RE.fullmatch(core):
        return True
    # 5) словарная проверка: только кириллица, ≥6 букв, не аббревиатура
    if has_cyr and not has_lat and len(core) >= 6 and not _ABBREV_RE.match(core):
        if _domain_known(core):
            return False                  # верный доменный термин — не шумим
        if _hyphen_parts_known(core):
            return False                  # «купли-продажи», «гражданина-должника»
        if ctx is not None and ctx.is_excused(core):
            return False                  # имя, организация, топоним или повтор
        if not _pymorphy_known(core):
            return True
    return False


def suspicious_spans(text: str, ctx=None) -> list[tuple[int, int]]:
    """Возвращает список (start, end) символьных диапазонов подозрительных слов."""
    spans: list[tuple[int, int]] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        # обрезаем обрамляющую пунктуацию/кавычки для анализа «ядра»
        core = tok.strip(_STRIP)
        if not core:
            continue
        if _is_suspicious(core, ctx):
            off = m.start() + tok.find(core)
            spans.append((off, off + len(core)))
    return spans


_CYR_WORD_RE = re.compile(rf"[{_CYR}]{{3,}}")
# Настоящий URL/email (строго): «@» между словами, www., схема, доменная зона.
# НЕ используем _URL_EMAIL_RE — он ловит любую точку («hype.») как URL.
_REAL_URL_RE = re.compile(r"\w@\w|https?://|www\.|\.(?:ru|com|org|net|gov)\b",
                          re.IGNORECASE)
# Валидные ОДНОбуквенные русские слова (предлоги/союзы/частицы): одиночная такая
# буква — не мусор («в лице»), прочие одиночные кир-буквы («ч», «м», «г») — шум.
_CYR_1LETTER_WORDS = set("аиоувкся")


def is_latin_homoglyph_word(core: str) -> str | None:
    """Если core — латиница, ПОЛНОСТЬЮ конвертируемая гомоглифами в известное
    русское слово (OCR-мисрид «Ha»→«На», «He»→«Не», «co»→«со»), вернуть кириллицу;
    иначе None. Защищает реальные слова от удаления как «латиница-мусор»."""
    if not core or _HAS_CYR.search(core) or not _HAS_LAT.search(core):
        return None
    conv = "".join(_LAT2CYR.get(c, "\0") for c in core)
    if "\0" in conv:                     # какая-то буква — не гомоглиф-двойник
        return None
    return conv if _pymorphy_known(conv.lower()) else None


def is_junk_text(text: str, max_len: int = 40) -> bool:
    """True если КОРОТКИЙ блок — целиком OCR-мусор (обрывок пятна/штампа): «t»,
    «ч», «theme cow», «19/2 t aad Ger», «aad Ger», «rN».

    Консервативно (удаление ДЕСТРУКТИВНО):
    - длинные блоки (> max_len) НЕ судим — там мусор вкраплён в текст, его
      снимают точечные ocr_fixes/подсветка, а НЕ удаление целого абзаца;
    - email/URL, знак «№» (поле/номер), чистое число/сумма/номер — содержательны;
    - если есть кириллица: мусор ТОЛЬКО одиночный символ («ч», «В») — обрывок
      пятна; двух+буквенные кир-фрагменты (в, из, на, об) НЕ трогаем, чтобы не
      потерять слово фрагментированной OCR-строки;
    - кириллицы нет: латиница/цифро-символьный мусор — удаляем."""
    s = text.strip()
    if not s:
        return True
    if len(s) > max_len:
        return False
    if _REAL_URL_RE.search(s):
        return False
    if "№" in s:
        return False
    core = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", s)   # буквенно-цифровое ядро
    if not core:
        return True                                  # только пунктуация — мусор
    # Чисто числовой/пунктуационный блок без букв («500 000», «19/2», «231/4»).
    if _HAS_DIGIT.search(s) and not _HAS_CYR.search(s) and not _HAS_LAT.search(s):
        return False
    if _HAS_CYR.search(s):
        cyr = re.findall(rf"[{_CYR}]", s)
        if len(cyr) <= 1 and len(core) <= 1:         # одиночная кир-буква
            return core.lower() not in _CYR_1LETTER_WORDS  # «ч»/«м» мусор, «в» нет
        return False                                  # 2+ кир-букв — не трогаем
    return True                                       # нет кириллицы = мусор


# Частые короткие предлоги/союзы — для снятия капс-шума (пО→по, cO→со).
# НЕ включаем «кв» и т.п. единицы, чтобы не сломать «кВ» (киловольт).
_COMMON_SHORT = {"по", "со", "из", "от", "на", "не", "до", "за", "об", "во",
                 "ко", "но", "да", "и", "в", "с", "к", "у", "о", "а"}


# Латиница → кириллица (визуальные двойники) — для очистки гомоглифов
_LAT2CYR = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
}


def _apply_case_pattern(fixed: str, original: str) -> str:
    """Переносит регистровый рисунок оригинала на исправленное слово:
    ВЕСЬ ВЕРХНИЙ → ВЕРХНИЙ, Первая-заглавная → Первая-заглавная, иначе строчный."""
    if len(original) > 1 and original.isupper():
        return fixed.upper()
    if original[:1].isupper():
        return fixed[:1].upper() + fixed[1:]
    return fixed


def _try_cyr_spell_fix(word: str) -> str | None:
    """Исправляет чисто-кириллические OCR-ошибки словарным перебором (BFS).

    На каждом шаге глубины подставляем визуальные двойники букв (_CYR_CONFUSIONS),
    pymorphy выступает оракулом «это настоящее слово». Возвращаем правку ТОЛЬКО
    если на минимальной глубине нашёлся РОВНО ОДИН словарный кандидат — иначе
    (0 или ≥2) оставляем слово на подсветку/LLM, чтобы не выдумать неверное
    написание. Примеры: нмущества→имущества, абластн→области, Федсрацин→Федерации.
    """
    if _morph is None or _HAS_LAT.search(word):
        return None
    # Имена собственные/фамилии (Первая-Заглавная, но не ВЕСЬ-ВЕРХНИЙ) словарным
    # перебором НЕ трогаем: «Заречнев»→«Заречней», «Аракслян»→«Аракелян» —
    # недопустимый риск. ALL-CAPS (заголовки) и строчные слова — разрешены.
    if word[:1].isupper() and not word.isupper():
        return None
    core = word.lower()
    if len(core) < _SPELL_MIN_LEN or not _CYR_ONLY.match(core):
        return None
    if _pymorphy_known(core) or _domain_known(core):
        return None          # уже нормальное/доменное слово — не выдумываем правку

    seen: set[str] = {core}
    frontier: list[str] = [core]
    budget = _SPELL_BUDGET

    for _ in range(_SPELL_DEPTH):
        found: set[str] = set()
        nxt: list[str] = []
        for w in frontier:
            for i, ch in enumerate(w):
                for repl in _CYR_CONFUSIONS.get(ch, ()):
                    cand = w[:i] + repl + w[i + 1:]
                    if cand in seen:
                        continue
                    seen.add(cand)
                    budget -= 1
                    if budget <= 0:
                        return None          # слишком ветвисто — безопаснее подсветить
                    if _pymorphy_known(cand):
                        found.add(cand)
                    else:
                        nxt.append(cand)
        if found:
            # единственный словарный кандидат на этой глубине → принимаем;
            # несколько (неоднозначность) → отдаём LLM/человеку с контекстом.
            return _apply_case_pattern(next(iter(found)), word) if len(found) == 1 else None
        frontier = nxt
        if not frontier:
            break

    return None


def _autofix_word(core: str) -> str | None:
    """Пытается ДЕТЕРМИНИРОВАННО починить подозрительное слово.

    1) случайные заглавные OCR (если строчных больше заглавных) → строчим;
    2) латинские буквы-двойники → кириллица (co→со, B→В, OOО→ООО, КB→КВ).
    Возвращает исправленное слово, если оно стало «чистым» (не подозрительным),
    иначе None (оставляем подсветку).
    """
    w = core
    # 1) латинские буквы-двойники → кириллица (регистр сохраняем): co→со, тоM→тоМ
    if _HAS_CYR.search(w) or _PURE_LAT_SHORT.match(w):
        w2 = "".join(_LAT2CYR.get(ch, ch) for ch in w)
        if not _HAS_LAT.search(w2):     # после замены латиницы не осталось
            w = w2
    # 2) случайные заглавные OCR (строчных больше заглавных) → строчим; но
    #    сохраняем возможную легитимную ПЕРВУЮ заглавную (Сбер, Москва, тоМ→том)
    n_low = sum(1 for c in w if c.islower())
    n_up  = sum(1 for c in w if c.isupper())
    if n_up and n_low > n_up:
        w = (w[0] + w[1:].lower()) if w[0].isupper() else w.lower()
    # 3) короткое слово со «скачущим» регистром, дающее частый предлог/союз:
    #    пО→по, cO→сО→со. Белый список защищает единицы (кВ→кв НЕ трогаем).
    if len(w) <= 3 and w.lower() in _COMMON_SHORT:
        w = w.lower()
    if w != core and not _is_suspicious(w):
        return w
    # 4) кириллические OCR-путаницы (н↔и, а↔о, с↔е, ё↔е) — словарный BFS
    spell = _try_cyr_spell_fix(w)
    if spell is not None and not _is_suspicious(spell):
        return spell
    return None


_ROMAN_RE = re.compile(r"^[IVXLCDM]{1,7}$", re.IGNORECASE)


def _is_garbled_cyrillic(core: str) -> bool:
    """Стоит ли подсвечивать слово, которое автофикс НЕ починил.

    Да — если это искажённое РУССКОЕ слово (есть кириллица): человек поправит.
    Нет — если чисто-латинский токен (URL-обрывок «ru»/«pro», код), римская
    цифра (IV, VII) или мусор: это не «слово с опечаткой», подсветка только шумит
    (и LLM на таком только галлюцинирует, напр. VII→VIII).

    Чисто-латинные токены ≤5 букв — подсвечиваем (гомоглифы в русском тексте).
    Длинные латинские слова (>5) — скорее код/URL/имя — не трогаем.
    """
    if _ROMAN_RE.match(core):
        return False                         # римские цифры (IV, VII) — валидны
    if _PURE_LAT_SHORT.match(core):
        return True                          # короткая/средняя латиница → ошибка
    n_cyr = len(re.findall(rf"[{_CYR}]", core))
    n_lat = len(re.findall(rf"[{_LAT}]", core))
    # для смешанных: подсвечиваем, если слово ПРЕИМУЩЕСТВЕННО кириллическое;
    # латиницы больше → обрывок URL/латинского слова (лnalog) — не подсветка
    return n_cyr > 0 and n_cyr >= n_lat


def _iter_paragraphs(parent):
    """Все абзацы документа, включая вложенные в ячейки таблиц (рекурсивно)."""
    body = parent.element.body if hasattr(parent, "element") else parent._element
    yield from _iter_in(body, parent)


def _iter_in(element, doc):
    for child in element.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            tbl = Table(child, doc)
            for row in tbl.rows:
                for cell in row.cells:
                    yield from _iter_in(cell._tc, doc)


def _has_complex_content(r) -> bool:
    """True если run содержит не только текст (переносы, картинки и т.п.)."""
    for ch in r.iterchildren():
        t = ch.tag.split("}")[-1]
        if t not in ("rPr", "t"):
            return True
    return False


def _process_run(run, ctx=None) -> tuple[int, int]:
    """Для каждого подозрительного слова: либо ДЕТЕРМИНИРОВАННО чиним (без
    подсветки), либо подсвечиваем жёлтым (если автопочинить нельзя).
    Возвращает (подсвечено, исправлено)."""
    spans = suspicious_spans(run.text, ctx)
    if not spans or _has_complex_content(run._r):
        return (0, 0)
    text = run.text
    segs: list[tuple[str, bool]] = []   # (текст, подсвечивать?)
    pos, n_hl, n_fix = 0, 0, 0
    for s, e in spans:
        if s > pos:
            segs.append((text[pos:s], False))
        word  = text[s:e]
        fixed = _autofix_word(word)
        if fixed is not None:
            segs.append((fixed, False)); n_fix += 1     # очищено — без подсветки
        elif _is_garbled_cyrillic(word):
            segs.append((word, True));  n_hl += 1       # искажённое рус. слово — подсветка
        else:
            segs.append((word, False))                  # URL-обрывок/римское/код — не трогаем
        pos = e
    if pos < len(text):
        segs.append((text[pos:], False))

    orig_r = copy.deepcopy(run._r)          # шаблон форматирования
    run.text = segs[0][0]
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW if segs[0][1] else None
    anchor = run._r
    for seg_text, hl in segs[1:]:
        new_r = copy.deepcopy(orig_r)
        anchor.addnext(new_r)
        nr = Run(new_r, run._parent)
        nr.text = seg_text
        nr.font.highlight_color = WD_COLOR_INDEX.YELLOW if hl else None
        anchor = new_r
    return (n_hl, n_fix)


def highlight_suspicious(doc) -> int:
    """Чистит детерминируемые OCR-ошибки в помеченных словах и подсвечивает
    остаток жёлтым. Возвращает число подсвеченных (оставшихся) фрагментов."""
    n_hl = n_fix = 0
    paras = list(_iter_paragraphs(doc))
    # Контекст собираем ДО правок: тексты абзацев ещё исходные.
    ctx = build_doc_context([p.text for p in paras])
    for para in paras:
        for run in list(para.runs):
            try:
                hl, fix = _process_run(run, ctx)
                n_hl += hl; n_fix += fix
            except Exception as exc:               # один run не должен ломать документ
                log.debug("highlight: пропуск run: %s", exc)
    log.info("highlight: исправлено %d, подсвечено %d (осталось) фрагментов; "
             "снято с подозрения документными правилами: %d токенов",
             n_fix, n_hl, len(ctx.excused))
    return n_hl
