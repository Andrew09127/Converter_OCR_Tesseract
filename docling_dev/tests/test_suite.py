"""
test_suite.py — полный набор unit-тестов для пакета docling_dev.
Запуск: python -m pytest docling_dev/tests/ -v
"""
from __future__ import annotations
import re
import sys
import types
from pathlib import Path

import pytest

# Добавляем корень проекта в sys.path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#  HELPERS

def make_bbox(t, b, l=0, r=100):
    return types.SimpleNamespace(t=t, b=b, l=l, r=r)


def make_pts(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def make_ocr(x0, y0, x1, y1, text, conf=0.9):
    return (make_pts(x0, y0, x1, y1), text, conf)

#  OCR FIXES

from docling_dev.ocr_fixes import postprocess, fix_quotes

@pytest.mark.parametrize("inp,expected", [
    #Кавычки
    ("<Центр-инвест>",          "«Центр-инвест»"),
    ("<Центр-инвестэ текст",    "«Центр-инвест» текст"),
    ("<Авангардж текст",        "«Авангард» текст"),
    ("<Центр-инвест» текст",    "«Центр-инвест» текст"),
    ("@Центр-инвестэ текст",    "«Центр-инвест» текст"),
    ("@Центр-инвестж текст",    "«Центр-инвест» текст"),
    ("@Центр-инвест» текст",    "«Центр-инвест» текст"),
    ("@Центр-инвест> текст",    "«Центр-инвест» текст"),
    ("<<Центр-инвест>>",        "«Центр-инвест»"),
    ('"Центр-инвест"',          "«Центр-инвест»"),
    ("«Центр-инвест»",          "«Центр-инвест»"),   
    ("«ентр-инвест»",           "«Центр-инвест»"), 

    # ── Пунктуация ────────────────────────────────────────────────────────────
    ("ИНН 6163011391,; ОГРН",   "ИНН 6163011391, ОГРН"),
    ("р-н; х Ленинакан",        "р-н, х Ленинакан"),
    ("; пр. Соколова",          ", пр. Соколова"),

    # ── г. / пр. ─────────────────────────────────────────────────────────────
    ("&. Ростов",               "г. Ростов"),
    ("; &. Ростов",             "; г. Ростов"),
    # Latin P вместо Cyrillic Р — OCR путает («Pocmов» вместо «Ростов»);
    # правило нормализует латиницу обратно в «Ростов».
    ("&. Pocmов-на-Дону",       "г. Ростов-на-Дону"),
    ("ир; Соколова",            "пр. Соколова"),
    ("ир: Соколова",            "пр. Соколова"),
    # Latin p вместо Cyrillic р — «иp:» вместо «ир:»
    ("иp; Соколова",            "пр. Соколова"),
    ("иp: Соколова",            "пр. Соколова"),
    ("mp. Соколова",            "пр. Соколова"),

    #Цифры
    ("]1234",                   "11234"),
    ("1234]",                   "12341"),
    ("1]23",                    "1123"),
    ("3+44000",                 "344000"),

    #Латинская B 
    # B, затем капслок-правило «ВКЛЮЧИТЬ»→«включить» (OCR-капс в середине)
    ("ВКЛЮЧИТЬ B реестр",       "включить в реестр"),
    ("лимитом B размере",       "лимитом в размере"),
    # B в начале строки без предшествующей кириллицы - строчная в (контекст неизвестен)
    ("B реестр требований",     "в реестр требований"),
    ("включить B реестр",       "включить в реестр"),

    #Email 
    ("welcome@centrinvest пu n;", "welcome@centrinvest.ru"),
    ("welcome@centrinvest пи н;", "welcome@centrinvest.ru"),
    ("welcome@centrinvest ru",    "welcome@centrinvest.ru"),

    # ── Знак №
    ("No 123",                  "№ 123"),
    ("Ng 123",                  "№ 123"),
    ("No. 123",                 "№ 123"),
    # Слитный Ne после кириллицы (OCR потерял пробел и №)
    ("требованияNе 14-02-25",   "требования № 14-02-25"),
    ("договораNe 60190309",     "договора № 60190309"),

    #К/с 
    ("Klс",                     "К/с"),   # OCR: К/с - Klс (l заменяет /)
    ("K/с",                     "К/с"),   # OCR: К - K
    ("Klc",                     "К/с"),   # OCR: с - c

    #Двойной пробел
    ("слово  слово",            "слово слово"),

    #Дефисный перенос
    ("несосто- ятельным",       "несостоятельным"),

    #Госпошлина
    ("Госпошлина : 100",        "Госпошлина: 100"),
    ("100 руб:",                "100 руб."),

    #Восстановление тел./факс Центр-инвест 
    # EasyOCR стабильно пропускает эту строку; восстанавливаем по паттерну
    ("Россия, 344000, welcome@centrinvest.ru",
     "Россия, 344000, тел./факс: (863) 2-000-000, www.centrinvest.ru, welcome@centrinvest.ru"),
    # Уже есть — не дублировать
    ("344000, тел./факс: (863) 2-000-000, www.centrinvest.ru, welcome@centrinvest.ru",
     "344000, тел./факс: (863) 2-000-000, www.centrinvest.ru, welcome@centrinvest.ru"),

    #Доменные OCR-слова (раунд spell) 
    ("действует в соответствин с", "действует в соответствии с"),
    ("на основании выеизложенного", "на основании вышеизложенного"),
    ("договор с Заемшиком",        "договор с Заёмщиком"),
    ("права заемщика защищены",    "права заёмщика защищены"),

    #Идентичность
    ("по делу № А53-3675/2025", "по делу № А53-3675/2025"),
    ("г. Ростов-на-Дону",       "г. Ростов-на-Дону"),
    ("ИНН: 6163011391",         "ИНН: 6163011391"),
])
def test_postprocess(inp: str, expected: str) -> None:
    assert postprocess(inp) == expected, (
        f"\n  вход:    {inp!r}\n  ожидал:  {expected!r}\n  получил: {postprocess(inp)!r}"
    )


def test_postprocess_empty_string() -> None:
    assert postprocess("") == ""


def test_postprocess_idempotent() -> None:
    """Второй вызов не должен менять уже обработанный текст."""
    samples = [
        "«Центр-инвест»",
        "г. Ростов-на-Дону, пр. Соколова, 62",
        "ИНН 6163011391, КПП 615250001",
    ]
    for s in samples:
        assert postprocess(postprocess(s)) == postprocess(s), (
            f"Не идемпотентно: {s!r}"
        )


#  GEOMETRY

from docling_dev.geometry import (
    bbox_h, bbox_mid_y, bbox_x0, bbox_x1, coplanar,
    detect_pdf_native, reading_order_key,
)


def test_bbox_h_normal():
    assert bbox_h(make_bbox(100, 80)) == pytest.approx(20.0)


def test_bbox_h_inverted():
    """Высота всегда положительная, независимо от порядка t/b."""
    assert bbox_h(make_bbox(80, 100)) == pytest.approx(20.0)


def test_bbox_h_zero():
    assert bbox_h(make_bbox(50, 50)) == pytest.approx(0.0)


def test_bbox_mid_y():
    assert bbox_mid_y(make_bbox(100, 80)) == pytest.approx(90.0)


def test_bbox_x0():
    assert bbox_x0(make_bbox(100, 80, l=30, r=200)) == pytest.approx(30.0)


def test_bbox_x1():
    assert bbox_x1(make_bbox(100, 80, l=30, r=200)) == pytest.approx(200.0)


def test_coplanar_overlapping():
    a = make_bbox(100, 60)
    b = make_bbox(80, 40)
    assert coplanar(a, b, tolerance=0)


def test_coplanar_not_overlapping():
    a = make_bbox(100, 80)   # spans 80–100
    b = make_bbox(60, 40)    # spans 40–60, gap=20
    assert not coplanar(a, b, tolerance=10)


def test_coplanar_within_tolerance():
    a = make_bbox(100, 80)
    b = make_bbox(60, 40)    # gap=20
    assert coplanar(a, b, tolerance=25)


def test_coplanar_none_args():
    assert coplanar(None, make_bbox(100, 80)) is False
    assert coplanar(make_bbox(100, 80), None) is False
    assert coplanar(None, None) is False

#  WORD ORDER — структуры данных

from docling_dev.word_order import (
    Word, VisualLine, TextBlock, reconstruct_blocks,
)


def test_word_properties():
    w = Word(text="тест", x0=10.0, y0=20.0, x1=110.0, y1=40.0)
    assert w.mid_y  == pytest.approx(30.0)
    assert w.mid_x  == pytest.approx(60.0)
    assert w.height == pytest.approx(20.0)


def test_word_zero_size():
    w = Word(text="x", x0=0.0, y0=5.0, x1=10.0, y1=5.0)
    assert w.height == pytest.approx(0.0)


def test_visual_line_empty():
    line = VisualLine()
    assert line.text    == ""
    assert line.mid_y   == pytest.approx(0.0)
    assert line.x0      == pytest.approx(0.0)
    assert line.x1      == pytest.approx(0.0)
    assert line.median_height == pytest.approx(10.0)   # default fallback


def test_visual_line_with_words():
    w1 = Word("первое", x0=0,  y0=10, x1=60,  y1=30)
    w2 = Word("второе", x0=70, y0=10, x1=130, y1=30)
    line = VisualLine(words=[w1, w2])
    assert line.text  == "первое второе"
    assert line.mid_y == pytest.approx(20.0)
    assert line.x0    == pytest.approx(0.0)
    assert line.x1    == pytest.approx(130.0)


def test_text_block_empty():
    block = TextBlock()
    assert block.line_count == 0
    assert block.text       == ""
    assert block.mid_y      == pytest.approx(0.0)


def test_text_block_with_lines():
    w = Word("слово", x0=0, y0=10, x1=100, y1=30)
    line = VisualLine(words=[w])
    block = TextBlock(lines=[line])
    assert block.line_count == 1
    assert block.text       == "слово"
    assert block.mid_y      == pytest.approx(20.0)
    assert block.font_height > 0


#  WORD ORDER — reconstruct_blocks

def test_reconstruct_empty():
    assert reconstruct_blocks([]) == []


def test_reconstruct_low_confidence_filtered():
    r = make_ocr(0, 10, 100, 30, "слово", conf=0.1)
    assert reconstruct_blocks([r], pdf_native=False) == []


def test_reconstruct_single_word():
    r = make_ocr(0, 10, 100, 30, "слово")
    blocks = reconstruct_blocks([r], pdf_native=False)
    assert len(blocks) == 1
    assert blocks[0].text == "слово"
    assert blocks[0].line_count == 1


def test_reconstruct_one_line_correct_x_order():
    """Слова на одной строке сортируются слева направо."""
    results = [
        make_ocr(200, 10, 300, 30, "третье"),
        make_ocr(0,   10, 100, 30, "первое"),
        make_ocr(100, 10, 200, 30, "второе"),
    ]
    blocks = reconstruct_blocks(results, pdf_native=False)
    assert len(blocks) == 1
    assert blocks[0].text == "первое второе третье"


def test_reconstruct_two_lines_correct_y_order():
    """Строки сортируются сверху вниз (screen coords: меньший y = выше)."""
    results = [
        make_ocr(0, 60, 100, 80, "вторая"),  # y=60–80 → ниже
        make_ocr(0, 10, 100, 30, "первая"),  # y=10–30 → выше
    ]
    blocks = reconstruct_blocks(results, pdf_native=False)
    combined = " ".join(b.text for b in blocks)
    assert combined.index("первая") < combined.index("вторая")


def test_reconstruct_two_paragraphs():
    """Большой Y-разрыв создаёт два отдельных параграфа."""
    results = [
        make_ocr(0, 10,  100, 30,  "параграф1"),
        make_ocr(0, 400, 100, 420, "параграф2"),  # разрыв ~370px >> межстрочный
    ]
    blocks = reconstruct_blocks(results, pdf_native=False)
    assert len(blocks) == 2


def test_reconstruct_whitespace_only_filtered():
    r = make_ocr(0, 10, 100, 30, "   ", conf=0.9)
    assert reconstruct_blocks([r], pdf_native=False) == []


def test_reconstruct_all_words_zero_height_filtered():
    """Слова с нулевой высотой (height=0) не влияют на median_h и фильтруются."""
    results = [make_ocr(0, 10, 100, 10, "слово")]  # y0=y1=10, height=0
    blocks = reconstruct_blocks(results, pdf_native=False)
    # Может вернуть [] (нет heights > 2) — не должен падать
    assert isinstance(blocks, list)


#  CONVERTER — _reorder_by_word_order

from docling_dev.converter import _reorder_by_word_order


def _fake_item(label: str, page: int, mid_y_from_top_pct: float,
               ph: float = 842.0, pw: float = 595.0, x0: float = 50.0):
    """
    Создаёт фейковый Docling-элемент.
    mid_y_from_top_pct: 0.0 = самый верх страницы, 1.0 = самый низ.
    В Docling (PDF coords, y=0 снизу):
      mid_y = ph * (1 - mid_y_from_top_pct)
    """
    mid_y = ph * (1.0 - mid_y_from_top_pct)
    t = mid_y + 10
    b = mid_y - 10
    bbox = types.SimpleNamespace(t=t, b=b, l=x0, r=x0 + 100)
    prov = types.SimpleNamespace(page_no=page, bbox=bbox)
    return types.SimpleNamespace(
        label=types.SimpleNamespace(value=label),
        prov=[prov],
        text=f"{label}@{mid_y_from_top_pct:.2f}",
    )


def _word_block(img_y_pct: float, img_h: float = 1000.0):
    """Фейковый TextBlock с mid_y в пиксельных экранных координатах."""
    return types.SimpleNamespace(mid_y=img_y_pct * img_h)


def test_reorder_picture_untouched():
    """Picture всегда остаётся на своей позиции."""
    items = [
        (_fake_item("picture",   1, 0.05), 0),
        (_fake_item("text",      1, 0.08), 0),   # шапка (< 15%)
        (_fake_item("paragraph", 1, 0.50), 0),   # тело A
        (_fake_item("paragraph", 1, 0.70), 0),   # тело B
    ]
    # word_order: B (70%) - block 0, A (50%) - block 1 (B выше чем A в px)
    blocks = [_word_block(0.30), _word_block(0.60)]
    word_blocks_map = {1: (blocks, 1000.0)}
    page_sizes = {1: (595.0, 842.0)}

    result = _reorder_by_word_order(items, word_blocks_map, page_sizes)

    assert result[0][0].label.value == "picture",  "картинка сдвинулась!"
    assert result[1][0].label.value == "text",     "текст шапки сдвинулся!"


def test_reorder_header_zone_excluded():
    """Элементы в топ 15% страницы не переставляются."""
    items = [
        (_fake_item("paragraph", 1, 0.05), 0),   # < 15% - шапка, не трогать
        (_fake_item("paragraph", 1, 0.10), 0),   # < 15% - шапка, не трогать
        (_fake_item("paragraph", 1, 0.50), 0),   # тело
        (_fake_item("paragraph", 1, 0.70), 0),   # тело
    ]
    labels_before = [x[0].text for x in items]
    blocks = [_word_block(0.05), _word_block(0.10), _word_block(0.60), _word_block(0.40)]
    word_blocks_map = {1: (blocks, 1000.0)}
    page_sizes = {1: (595.0, 842.0)}

    result = _reorder_by_word_order(items, word_blocks_map, page_sizes)

    # Элементы шапки не сдвинулись
    assert result[0][0].text == labels_before[0]
    assert result[1][0].text == labels_before[1]


def test_reorder_body_corrected():
    """Body-параграфы переставляются по word_order-порядку."""
    items = [
        (_fake_item("paragraph", 1, 0.20), 0),
        (_fake_item("paragraph", 1, 0.22), 0),
        (_fake_item("paragraph", 1, 0.24), 0),
        (_fake_item("paragraph", 1, 0.80), 0),
    ]
    blocks = [_word_block(0.23), _word_block(0.19), _word_block(0.21), _word_block(0.78)]
    word_blocks_map = {1: (blocks, 1000.0)}
    page_sizes = {1: (595.0, 842.0)}

    result = _reorder_by_word_order(items, word_blocks_map, page_sizes)

    texts = [r[0].text for r in result]
    original = ["paragraph@0.20", "paragraph@0.22", "paragraph@0.24", "paragraph@0.80"]
    # Порядок должен измениться (word_order переставил 3 элемента)
    assert texts != original, f"Порядок не изменился: {texts}"
    # item3 (0.80) должен остаться на последнем месте
    assert "0.80" in texts[-1], f"item3 сдвинулся: {texts}"


def test_reorder_no_change_when_order_correct():
    """Если Docling-порядок уже верный — ничего не меняется."""
    items = [
        (_fake_item("paragraph", 1, 0.20), 0),
        (_fake_item("paragraph", 1, 0.50), 0),
        (_fake_item("paragraph", 1, 0.80), 0),
    ]
    original_texts = [x[0].text for x in items]

    # word_order подтверждает тот же порядок
    blocks = [_word_block(0.20), _word_block(0.50), _word_block(0.80)]
    word_blocks_map = {1: (blocks, 1000.0)}
    page_sizes = {1: (595.0, 842.0)}

    result = _reorder_by_word_order(items, word_blocks_map, page_sizes)
    assert [r[0].text for r in result] == original_texts


def test_reorder_empty_items():
    result = _reorder_by_word_order([], {}, {})
    assert result == []


def test_reorder_no_word_blocks():
    """Без word_blocks_map — возвращаем оригинал без изменений."""
    items = [(_fake_item("paragraph", 1, 0.5), 0)]
    result = _reorder_by_word_order(items, {}, {1: (595.0, 842.0)})
    assert result == items


def test_reorder_single_body_item():
    """Один body-элемент — нечего переставлять."""
    items = [(_fake_item("paragraph", 1, 0.5), 0)]
    blocks = [_word_block(0.5)]
    result = _reorder_by_word_order(
        items, {1: (blocks, 1000.0)}, {1: (595.0, 842.0)}
    )
    assert result == items


def test_reorder_min_move_threshold():
    """Если порядок уже верный — ничего не переставляем (moved=0 < MIN_MOVE=2)."""
    items = [
        (_fake_item("paragraph", 1, 0.30), 0),
        (_fake_item("paragraph", 1, 0.50), 0),
        (_fake_item("paragraph", 1, 0.70), 0),
    ]
    original_texts = [x[0].text for x in items]

    # word_order меняет только первые два (один реально сдвигается)
    # но оба получают одинаковый block_idx - нет реального смещения
    blocks = [_word_block(0.30), _word_block(0.50), _word_block(0.70)]
    word_blocks_map = {1: (blocks, 1000.0)}
    page_sizes = {1: (595.0, 842.0)}

    result = _reorder_by_word_order(items, word_blocks_map, page_sizes)
    assert [r[0].text for r in result] == original_texts

#  CONVERTER — _bbox_top_bottom / _is_letterhead_stop

from docling_dev.converter import _bbox_top_bottom, _is_letterhead_stop


def test_bbox_top_bottom_pdf_native():
    """PDF-native (y=0 снизу): t=750, b=700 → screen top=92, bottom=142."""
    bbox = make_bbox(t=750, b=700, l=10, r=100)
    top, bottom = _bbox_top_bottom(bbox, page_height=842.0, pdf_native=True)
    assert abs(top - 92.0) < 1.0    # 842 - 750 = 92
    assert abs(bottom - 142.0) < 1.0  # 842 - 700 = 142


def test_bbox_top_bottom_screen():
    """Screen (y=0 сверху): t=92, b=142 → top=92, bottom=142."""
    bbox = make_bbox(t=92, b=142, l=10, r=100)
    top, bottom = _bbox_top_bottom(bbox, page_height=842.0, pdf_native=False)
    assert abs(top - 92.0) < 1.0
    assert abs(bottom - 142.0) < 1.0


def test_is_letterhead_stop_matches():
    for text in ["КРЕДИТОР: ПАО КБ", "ДОЛЖНИК: Иванов", "Арбитражный суд",
                 "ЗАЯВЛЕНИЕ", "Госпошлина", "по делу № А53"]:
        assert _is_letterhead_stop(text), f"Должен быть стоп-маркером: {text!r}"


def test_is_letterhead_stop_no_match():
    for text in ["ИНН 6163011391", "тел./факс: (863)", "К/с 30101810",
                 "www.centrinvest.ru", "344000, г. Ростов-на-Дону"]:
        assert not _is_letterhead_stop(text), f"Не должен быть стоп-маркером: {text!r}"

#  _fix_reading_order

from docling_dev.converter import _fix_reading_order


def _sec_header(text: str, page: int = 1):
    """Fake section_header item."""
    bbox = make_bbox(t=800, b=780, l=50, r=400)
    prov = types.SimpleNamespace(page_no=page, bbox=bbox)
    return (types.SimpleNamespace(
        label=types.SimpleNamespace(value="section_header"),
        prov=[prov], text=text,
    ), 0)


def _list_item(text: str, page: int = 1):
    """Fake list_item."""
    bbox = make_bbox(t=500, b=480, l=50, r=400)
    prov = types.SimpleNamespace(page_no=page, bbox=bbox)
    return (types.SimpleNamespace(
        label=types.SimpleNamespace(value="list_item"),
        prov=[prov], text=text,
    ), 0)


def test_fix_order_caps_before_subtitle():
    """ALL-CAPS section_header выводится перед строчным subtitle."""
    items = [
        _sec_header("о включении в реестр"),      # строчный — должен стать вторым
        _sec_header("ЗАЯВЛЕНИЕ"),                  # ALL-CAPS — должен стать первым
        _sec_header("требований кредиторов"),      # строчный — остаётся третьим
    ]
    result, _ = _fix_reading_order(items)
    assert result[0][0].text == "ЗАЯВЛЕНИЕ", f"Got: {[r[0].text for r in result]}"
    assert result[1][0].text == "о включении в реестр"


def test_fix_order_caps_no_change_when_correct():
    """Если ALL-CAPS уже первый — ничего не меняется."""
    items = [
        _sec_header("ЗАЯВЛЕНИЕ"),
        _sec_header("о включении в реестр"),
    ]
    result, _ = _fix_reading_order(items)
    assert result[0][0].text == "ЗАЯВЛЕНИЕ"


def test_fix_order_numbered_list_sorted():
    """Numbered list_items сортируются по ведущей цифре."""
    items = [
        _list_item("2 Копия платежного поручения"),
        _list_item("1 Документы подтверждающие"),
        _list_item("3 Расчет задолженности"),
        _list_item("5 Копия доп. соглашения"),
        _list_item("4 Копия кредитного договора"),
    ]
    result, _ = _fix_reading_order(items)
    texts = [r[0].text for r in result]
    assert texts[0].startswith("1 "), f"Got: {texts}"
    assert texts[1].startswith("2 ")
    assert texts[2].startswith("3 ")
    assert texts[3].startswith("4 ")
    assert texts[4].startswith("5 ")


def test_fix_order_numbered_already_sorted():
    """Если список уже в порядке — ничего не меняется."""
    items = [
        _list_item("1 Документы"),
        _list_item("2 Копия"),
        _list_item("3 Расчет"),
    ]
    result, _ = _fix_reading_order(items)
    assert result[0][0].text == "1 Документы"


def test_fix_order_unnumbered_at_end():
    """Ненумерованный пункт в конце получает автонумерацию-продолжение:
    OCR часто теряет номер, поэтому к 1–3 добавляется «4 …»."""
    items = [
        _list_item("2 Копия поручения"),
        _list_item("1 Документы"),
        _list_item("3 Расчет"),
        _list_item("Копия доверенности"),   # без номера → станет «4 …»
    ]
    result, _ = _fix_reading_order(items)
    texts = [r[0].text for r in result]
    assert texts[0].startswith("1 ")
    assert texts[1].startswith("2 ")
    assert texts[2].startswith("3 ")
    assert texts[-1] == "4 Копия доверенности", f"Got: {texts}"

#  HIGHLIGHT — кириллический спелл-фиксер (_autofix_word)

from docling_dev.highlight import _autofix_word, _morph

# Спелл-фиксер требует словаря pymorphy; без него тесты пропускаем.
_need_morph = pytest.mark.skipif(_morph is None, reason="pymorphy не установлен")


@_need_morph
@pytest.mark.parametrize("garbled,fixed", [
    ("нмущества",       "имущества"),
    ("абластн",         "области"),
    ("падтверждено",    "подтверждено"),
    ("васстановлен",    "восстановлен"),
    ("частнасти",       "частности"),
    ("атсутстние",      "отсутствие"),
    ("прнменяеная",     "применяемая"),
    ("данньми",         "данными"),         # пара ь↔ы
    ("несостоятельньм", "несостоятельным"), # пара ь↔ы
    ("деятсльности",    "деятельности"),
])
def test_spell_fix_lower(garbled, fixed):
    """Строчные чисто-кириллические OCR-опечатки с ЕДИНСТВЕННЫМ кандидатом."""
    assert _autofix_word(garbled) == fixed


@_need_morph
def test_spell_fix_allcaps_preserves_case():
    """ALL-CAPS заголовок чинится с сохранением регистра."""
    assert _autofix_word("МЕЖРАПОННАЯ") == "МЕЖРАЙОННАЯ"


@_need_morph
@pytest.mark.parametrize("proper", ["Заречнев", "Аракслян", "Краснянскову"])
def test_spell_fix_skips_titlecase(proper):
    """Имена/фамилии (Первая-Заглавная) не «чиним» — только подсветка."""
    assert _autofix_word(proper) is None


@_need_morph
@pytest.mark.parametrize("domain", [
    "взыскателя", "займодавцу", "микрофинансовой", "коллекторская",
])
def test_spell_fix_skips_domain(domain):
    """Доменные юр./фин. термины (нет в pymorphy, но верные) не трогаем."""
    assert _autofix_word(domain) is None


@_need_morph
@pytest.mark.parametrize("ambiguous", ["арганом", "налоговон"])
def test_spell_fix_skips_ambiguous(ambiguous):
    """Несколько словарных кандидатов → не угадываем (отдаём на подсветку/LLM)."""
    assert _autofix_word(ambiguous) is None


@_need_morph
@pytest.mark.parametrize("valid", ["имущества", "области", "требований", "договоров"])
def test_spell_fix_keeps_valid(valid):
    """Уже корректные слова не меняем."""
    assert _autofix_word(valid) is None

#  INK — насыщенность штриха (жирность по изображению на сканах)

from docling_dev.ink import block_ink_stats

try:
    import numpy as _np
    from PIL import Image as _PILImage
    _HAS_NP = True
except Exception:
    _HAS_NP = False

_need_np = pytest.mark.skipif(not _HAS_NP, reason="numpy/PIL не установлены")


def _synthetic_text(stroke_px: int):
    """Белый холст с тремя «текстовыми» полосами из вертикальных штрихов
    заданной толщины — имитация тонкого/жирного шрифта."""
    arr = _np.full((50, 200), 255, dtype=_np.uint8)
    for row0 in (10, 25, 40):
        for col in range(5, 195, 8):
            arr[row0:row0 + 6, col:col + stroke_px] = 0
    return _PILImage.fromarray(arr)


@_need_np
def test_ink_none_for_empty():
    assert block_ink_stats(None) is None


@_need_np
def test_ink_none_for_tiny():
    assert block_ink_stats(_PILImage.fromarray(_np.zeros((2, 2), dtype=_np.uint8))) is None


@_need_np
def test_ink_stroke_w_grows_with_thickness():
    """Главное свойство: чем толще штрих, тем выше stroke_w/mean_run (жирнее)."""
    thin = block_ink_stats(_synthetic_text(1))
    mid  = block_ink_stats(_synthetic_text(2))
    bold = block_ink_stats(_synthetic_text(3))
    assert thin is not None and mid is not None and bold is not None
    assert thin["stroke_w"] < mid["stroke_w"] < bold["stroke_w"]
    assert thin["mean_run"] < mid["mean_run"] < bold["mean_run"]


@_need_np
def test_ink_robust_to_underline():
    """Подчёркивание (длинная линия) НЕ должно раздувать stroke_w/mean_run:
    толщина штриха букв сохраняется (линии-выбросы отсекаются max_run_px)."""
    plain = block_ink_stats(_synthetic_text(1))
    arr = _np.full((50, 200), 255, dtype=_np.uint8)
    for row0 in (10, 25, 40):
        for col in range(5, 195, 8):
            arr[row0:row0 + 6, col:col + 1] = 0
    arr[46:48, 5:195] = 0                       # длинная линия-подчёркивание
    underlined = block_ink_stats(_PILImage.fromarray(arr))
    assert plain is not None and underlined is not None
    assert abs(underlined["stroke_w"] - plain["stroke_w"]) < 0.6
    assert abs(underlined["mean_run"] - plain["mean_run"]) < 0.6


@_need_np
def test_ink_dense_thin_not_bold():
    """«Плотный, но тонкий» (имитация цифр) НЕ должен выглядеть жирным:
    stroke_w остаётся как у тонкого, хотя stroke_density высокая."""
    thin   = block_ink_stats(_synthetic_text(1))   # редкие тонкие штрихи
    arr = _np.full((50, 200), 255, dtype=_np.uint8)
    for row0 in (10, 25, 40):
        for col in range(5, 195, 3):
            arr[row0:row0 + 6, col:col + 1] = 0
    dense = block_ink_stats(_PILImage.fromarray(arr))
    assert thin is not None and dense is not None
    assert abs(dense["stroke_w"] - thin["stroke_w"]) < 0.6
    assert dense["stroke_density"] > thin["stroke_density"]  

@_need_np
def test_ink_values_in_range():
    st = block_ink_stats(_synthetic_text(2))
    assert st is not None
    assert 0.0 <= st["stroke_density"] <= 1.0
    assert 0.0 <= st["ink_ratio"] <= 1.0
    assert st["stroke_w"] >= 0.0
    assert st["mean_run"] >= 0.0


@_need_np
def test_ink_blank_is_zero():
    """Чистый белый холст — нулевая насыщенность."""
    blank = _PILImage.fromarray(_np.full((40, 120), 255, dtype=_np.uint8))
    st = block_ink_stats(blank)
    assert st is not None
    assert st["ink_ratio"] == 0.0
    assert st["stroke_density"] == 0.0

#  OCR PREPROCESS — предобработка изображения перед EasyOCR

from docling_dev.ocr_preprocess import preprocess_for_ocr

try:
    import cv2 as _cv2
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False

_need_cv2 = pytest.mark.skipif(not (_HAS_NP and _HAS_CV2),
                               reason="cv2/numpy не установлены")


@_need_cv2
def test_preprocess_returns_rgb_same_size():
    """Возвращает 3-канальный RGB того же размера (EasyOCR ждёт RGB)."""
    img = _np.full((120, 200, 3), 255, dtype=_np.uint8)
    img[50:60, 20:180] = 30
    out = preprocess_for_ocr(img)
    assert out.shape == (120, 200, 3)


@_need_cv2
def test_preprocess_noop_on_non_array():
    """Не-numpy вход (путь/None) возвращается как есть (no-op)."""
    assert preprocess_for_ocr("page.png") == "page.png"
    assert preprocess_for_ocr(None) is None


@_need_cv2
def test_preprocess_handles_grayscale():
    """Серый вход (2D) не падает и даёт RGB."""
    gray = _np.full((80, 120), 240, dtype=_np.uint8)
    gray[30:36, 10:110] = 20
    out = preprocess_for_ocr(gray)
    assert out.ndim == 3 and out.shape[2] == 3


@_need_cv2
def test_preprocess_denoise_off_by_default():
    """denoise по умолчанию выкл (он размывал текст и снижал confidence)."""
    import inspect
    sig = inspect.signature(preprocess_for_ocr)
    assert sig.parameters["denoise"].default is False


@_need_cv2
def test_preprocess_no_crash_on_rotated():
    """Повёрнутый текст (deskew) не вызывает падения."""
    img = _np.full((150, 300, 3), 255, dtype=_np.uint8)
    for r in range(40, 110, 20):
        img[r:r + 4, 30:270] = 0
    rot = _cv2.warpAffine(
        img, _cv2.getRotationMatrix2D((150, 75), 3.0, 1.0), (300, 150),
        borderValue=(255, 255, 255))
    out = preprocess_for_ocr(rot)
    assert out.shape == (150, 300, 3)


#  PAGE ANALYSER — уровни отступов и модель колонок

from docling_dev.page_analyser import (
    analyse_pages as analyse_page_infos,
    build_indent_levels, snap_indent, _detect_columns,
)


def _make_body_item(x0, x1, t, b, page=1, label="text"):
    """Мини-имитация Docling body-элемента с bbox и prov."""
    bbox = types.SimpleNamespace(l=x0, r=x1, t=t, b=b)
    prov = types.SimpleNamespace(bbox=bbox, page_no=page, page_w=595.0, page_h=842.0)
    return (types.SimpleNamespace(label=label, prov=[prov], text="x" * 50), 0)


def test_indent_levels_merge_ocr_jitter():
    """Дрожащие x0 одного отступа (шум OCR 1-4pt) сливаются в ОДИН уровень."""
    items = [_make_body_item(x0, 500, 700 - i * 20, 690 - i * 20)
             for i, x0 in enumerate([70, 71.5, 73, 72, 106, 107.5])]
    levels = build_indent_levels(items, {1: 70.0})
    # два уровня: ~0-3pt (шум левого поля, отбрасывается порогом 2pt частично)
    # и ~36-37pt (красная строка). Проверяем что близкие значения не размножились.
    assert len(levels) <= 2
    assert any(abs(lv - 36.5) <= 2 for lv in levels)


def test_indent_levels_keep_distinct():
    """Далёкие отступы (красная строка 35pt vs колонка 150pt) — РАЗНЫЕ уровни."""
    items = ([_make_body_item(105, 500, 700 - i * 20, 690 - i * 20) for i in range(3)]
             + [_make_body_item(220, 500, 400 - i * 20, 390 - i * 20) for i in range(3)])
    levels = build_indent_levels(items, {1: 70.0})
    assert len(levels) == 2
    assert any(abs(lv - 35) <= 2 for lv in levels)
    assert any(abs(lv - 150) <= 2 for lv in levels)


def test_indent_singleton_no_level():
    """Одиночный выброс (min_support=2) уровня не образует."""
    items = ([_make_body_item(105, 500, 700 - i * 20, 690 - i * 20) for i in range(3)]
             + [_make_body_item(300, 500, 100, 90)])
    levels = build_indent_levels(items, {1: 70.0})
    assert len(levels) == 1


def test_snap_indent_pulls_to_level():
    """Сырой отступ в пределах допуска прищёлкивается к уровню."""
    assert snap_indent(33.0, [35.4, 150.0]) == 35.4
    assert snap_indent(154.0, [35.4, 150.0]) == 150.0


def test_snap_indent_noise_is_zero():
    """Отступ < 4pt — шум сегментации, отступа нет."""
    assert snap_indent(3.0, [35.4]) == 0.0
    assert snap_indent(0.0, []) == 0.0


def test_snap_indent_far_raw_kept():
    """Отступ дальше допуска от всех уровней остаётся сырым."""
    assert snap_indent(80.0, [35.4, 150.0]) == 80.0
    assert snap_indent(80.0, []) == 80.0


def test_detect_columns_two_column_header():
    """Шапка заявления: левая колонка x0≈70, правая x0≈300 → 2 колонки."""
    x0s = [70, 72, 71, 74, 70, 300, 302, 305, 301, 300]
    cols = _detect_columns(x0s, 595.0)
    assert len(cols) == 2
    assert cols[0].x0 < 100 and cols[1].x0 > 250


def test_detect_columns_single_column():
    """Обычная страница: все x0 у левого поля → колонки не детектируются."""
    x0s = [70, 71, 74, 105, 72, 70, 73, 71]
    cols = _detect_columns(x0s, 595.0)
    assert len(cols) < 2


def test_page_info_multicolumn_wiring():
    """analyse_pages: двухколоночная страница получает page_type=multicolumn
    и column_for_x правильно относит блок к правой колонке."""
    items = ([_make_body_item(70, 260, 700 - i * 20, 690 - i * 20) for i in range(5)]
             + [_make_body_item(300, 520, 700 - i * 20, 690 - i * 20) for i in range(5)])
    infos = analyse_page_infos(items)
    info = infos[1]
    assert info.is_multicolumn
    assert info.column_for_x(300) == 1
    assert info.column_for_x(70) == 0


#  GEOMETRY — sort_reading_order (построчная кластеризация вместо бинов 40pt)

from docling_dev.geometry import sort_reading_order


def _line_frag(x0, x1, top, h=12.0, page=1, text="w"):
    """Фрагмент строки в screen-координатах (t < b, y растёт вниз)."""
    bbox = types.SimpleNamespace(l=x0, r=x1, t=top, b=top + h)
    prov = types.SimpleNamespace(bbox=bbox, page_no=page, page_w=595.0, page_h=842.0)
    return (types.SimpleNamespace(label="text", prov=[prov], text=text), 0)


def test_sort_no_line_interleave():
    """Три строки с шагом 14pt, каждая порезана на фрагменты: порядок строк
    сохраняется, фрагменты НЕ перемешиваются по X между строками.
    (Бин 40pt клал все три строки в один бин → интерливинг слов.)"""
    items = [
        _line_frag(70, 200, 100, text="во вторую очередь"),
        _line_frag(210, 400, 100, text="3 131 130,79 руб."),
        _line_frag(70, 250, 114, text="в третью очередь"),
        _line_frag(260, 450, 114, text="4 270 589,08 руб."),
        _line_frag(70, 180, 128, text="2 118 547,65 руб."),
        _line_frag(190, 380, 128, text="штрафы 105 555,43"),
    ]
    # Перемешиваем вход
    shuffled = [items[4], items[1], items[5], items[0], items[3], items[2]]
    out = sort_reading_order(shuffled, pdf_native=False)
    texts = [it.text for it, _ in out]
    assert texts == ["во вторую очередь", "3 131 130,79 руб.",
                     "в третью очередь", "4 270 589,08 руб.",
                     "2 118 547,65 руб.", "штрафы 105 555,43"]


def test_sort_full_lines_keep_order():
    """Однострочные полноширинные блоки с шагом 14pt не свапаются.
    (Бин 40pt сортировал их по X → пункты списка менялись местами.)"""
    items = [
        _line_frag(75, 500, 100, text="- задолженность по кредиту"),
        _line_frag(70, 510, 114, text="- задолженность по процентам"),
        _line_frag(72, 505, 128, text="- задолженность по пени"),
    ]
    out = sort_reading_order(list(items), pdf_native=False)
    texts = [it.text for it, _ in out]
    assert texts == ["- задолженность по кредиту",
                     "- задолженность по процентам",
                     "- задолженность по пени"]


def test_sort_same_line_left_to_right():
    """Блоки одной визуальной строки идут слева направо."""
    items = [
        _line_frag(300, 500, 100, text="право"),
        _line_frag(70, 250, 102, text="лево"),
    ]
    out = sort_reading_order(list(items), pdf_native=False)
    assert [it.text for it, _ in out] == ["лево", "право"]


def test_sort_pdf_native_coords():
    """pdf_native (y растёт вверх, t > b): верхняя строка первой."""
    def native(x0, x1, bottom, h=12.0, text="w"):
        bbox = types.SimpleNamespace(l=x0, r=x1, t=bottom + h, b=bottom)
        prov = types.SimpleNamespace(bbox=bbox, page_no=1, page_w=595.0, page_h=842.0)
        return (types.SimpleNamespace(label="text", prov=[prov], text=text), 0)
    items = [
        native(70, 500, 700, text="нижняя"),
        native(70, 500, 760, text="верхняя"),
    ]
    out = sort_reading_order(list(items), pdf_native=True)
    assert [it.text for it, _ in out] == ["верхняя", "нижняя"]


def test_sort_pages_and_no_prov():
    """Страницы по возрастанию; элементы без prov — в конец."""
    no_prov = (types.SimpleNamespace(label="text", prov=[], text="без prov"), 0)
    items = [
        _line_frag(70, 500, 100, page=2, text="стр2"),
        no_prov,
        _line_frag(70, 500, 100, page=1, text="стр1"),
    ]
    out = sort_reading_order(items, pdf_native=False)
    assert [it.text for it, _ in out] == ["стр1", "стр2", "без prov"]


#  CONVERTER — расклейка дефиса подпунктов («-задолженность» без пробела)

def test_dash_strip_glued():
    """Дефис, приклеенный к слову, срезается (не задваивается рендером)."""
    pat = re.compile(r'^\s*[-–—•·]\s*(?=[^\W\d])')
    assert pat.sub('', "-задолженность по уплате") == "задолженность по уплате"
    assert pat.sub('', "- задолженность по уплате") == "задолженность по уплате"
    assert pat.sub('', "–в третью очередь") == "в третью очередь"


def test_dash_negative_number_kept():
    """Дефис перед цифрой — минус числа, не буллет: не срезается."""
    pat = re.compile(r'^\s*[-–—•·]\s*(?=[^\W\d])')
    assert pat.sub('', "-5 000 руб.") == "-5 000 руб."


#  OCR PREPROCESS — remove_stains (удаление пятен со скана)

from docling_dev.ocr_preprocess import remove_stains as _remove_stains


def _synth_page():
    """Синтетический скан 900x700: 4 строки «текста» из букв-прямоугольников."""
    img = _np.full((700, 900), 255, dtype=_np.uint8)
    for row_y in (100, 150, 200, 250):
        x = 150
        for word in range(8):
            for letter in range(5):
                img[row_y:row_y + 20, x:x + 12] = 0
                x += 16
            x += 14                      # межсловный пробел
    return img


@_need_cv2
def test_stains_text_kept():
    """Чистый синтетический текст не изменяется."""
    img = _synth_page()
    out = _remove_stains(img)
    assert int((img != out).sum()) == 0


@_need_cv2
def test_stains_blob_removed():
    """Клякса в левом поле (вне строк) удаляется, текст цел."""
    img = _synth_page()
    img[120:240, 30:80] = 0              # вертикальная грязевая полоса в поле
    out = _remove_stains(img)
    assert (out[120:240, 30:80] == 255).all()          # полоса стёрта
    assert (out[100:120, 150:162] == 0).any()          # буквы на месте


@_need_cv2
def test_stains_specks_removed():
    """Россыпь мелких точек («соль-перец») удаляется."""
    img = _synth_page()
    for y, x in ((60, 300), (300, 500), (400, 100), (55, 700)):
        img[y:y + 2, x:x + 2] = 0
    out = _remove_stains(img)
    for y, x in ((60, 300), (300, 500), (400, 100), (55, 700)):
        assert (out[y:y + 2, x:x + 2] == 255).all()


@_need_cv2
def test_stains_thin_line_kept():
    """Тонкая длинная линия (рамка таблицы/подчёркивание) сохраняется."""
    img = _synth_page()
    img[300:302, 150:750] = 0            # горизонтальная линия
    out = _remove_stains(img)
    assert (out[300:302, 200:700] == 0).any()


@_need_cv2
def test_stains_short_token_near_row_kept():
    """Короткий токен («1.»), отделённый пробелом от строки, не удаляется."""
    img = _synth_page()
    img[100:120, 100:112] = 0            # «цифра» слева от строки (зазор 38px)
    out = _remove_stains(img)
    assert (out[100:120, 100:112] == 0).any()


@_need_cv2
def test_stains_isolated_mark_removed():
    """Одиночная метка вдали от текста (грязь) удаляется."""
    img = _synth_page()
    img[500:512, 400:412] = 0            # клякса 12x12 в пустой зоне
    out = _remove_stains(img)
    assert (out[500:512, 400:412] == 255).all()


#  OCR PREPROCESS — штампы и фильтр мусорных картинок

from docling_dev.ocr_preprocess import is_junk_image as _is_junk_image


def _add_stamp(img, x0=600, y0=400, w=300, h=120):
    """Рисует рамку штампа с «текстом» и «росписью» внутри."""
    img[y0:y0 + 2, x0:x0 + w] = 0            # верх
    img[y0 + h - 2:y0 + h, x0:x0 + w] = 0    # низ
    img[y0:y0 + h, x0:x0 + 2] = 0            # лево
    img[y0:y0 + h, x0 + w - 2:x0 + w] = 0    # право
    for lx in range(x0 + 20, x0 + 200, 16):  # строка «текста» внутри
        img[y0 + 30:y0 + 45, lx:lx + 10] = 0
    return img


@_need_cv2
def test_stamp_removed_with_contents():
    """Рамка штампа и содержимое внутри неё удаляются, текст документа цел."""
    img = _synth_page()
    _add_stamp(img)
    out = _remove_stains(img)
    assert (out[400:520, 600:900] == 255).all()        # штамп стёрт целиком
    assert (out[100:120, 150:162] == 0).any()          # текст на месте


@_need_cv2
def test_junk_image_speckle():
    """Кроп-россыпь крапинок (грязевая полоса) — мусор."""
    from PIL import Image as _PILImage
    rng = _np.random.default_rng(7)
    img = _np.full((400, 80), 255, dtype=_np.uint8)   # узкая полоса, как у сшивки
    for _ in range(60):
        y, x = int(rng.integers(0, 390)), int(rng.integers(0, 70))
        img[y:y + int(rng.integers(2, 8)), x:x + int(rng.integers(2, 8))] = 0
    assert _is_junk_image(_PILImage.fromarray(img)) is True


@_need_cv2
def test_junk_image_text_not_junk():
    """Кроп с обычными текстовыми строками — НЕ мусор."""
    from PIL import Image as _PILImage
    img = _synth_page()[80:280, 100:800]
    assert _is_junk_image(_PILImage.fromarray(img)) is False


@_need_cv2
def test_junk_image_stamp():
    """Кроп со штампом (рамка на весь кроп) — мусор."""
    from PIL import Image as _PILImage
    img = _np.full((160, 340), 255, dtype=_np.uint8)
    _add_stamp(img, x0=10, y0=10, w=320, h=140)
    assert _is_junk_image(_PILImage.fromarray(img)) is True


@_need_cv2
def test_junk_image_solid_logo_kept():
    """Сплошная эмблема (мало компонент, без строк) — НЕ мусор."""
    from PIL import Image as _PILImage
    img = _np.full((200, 200), 255, dtype=_np.uint8)
    _cv2.circle(img, (100, 100), 70, 0, -1)
    assert _is_junk_image(_PILImage.fromarray(img)) is False


#  HIGHLIGHT — фильтр коротких блоков-мусора (is_junk_text)

from docling_dev.highlight import is_junk_text as _is_junk_text


def test_junk_text_drops_noise():
    """Короткие блоки-обрывки OCR — мусор."""
    for s in ["t", "ч", "м", "щ", "theme cow", "hype.", "19/2 t aad Ger",
              "aad Ger", "AWAWNE", "rN", "", "  ", "|}{"]:
        assert _is_junk_text(s) is True, s


def test_junk_text_keeps_content():
    """Содержательные блоки не трогаем: кир-слово, аббревиатура, число,
    реквизит, email/URL, «№»-поле, имя собственное без словаря, длинный абзац."""
    for s in ["Кредиторы:", "1. ПАО «Сбербанк»", "8. ООО «МКК НФ»",
              "г. Ростов-на-Дону", "Дата", "Подпись", "2026",
              "500 000", "231/4", "ИНН 7707083893", "На №",
              "www.nalog.gov.ru", "tns-rostov@rostov.tns-e.ru",
              "Мирский Алексей Степанович"]:
        assert _is_junk_text(s) is False, s


def test_junk_text_keeps_short_cyr_fragments():
    """Двух+буквенные кир-фрагменты (предлоги разорванной строки) — НЕ мусор:
    иначе из «в / лице / ИФНС» теряется слово."""
    for s in ["в", "из", "на", "об", "ор", "лице"]:
        assert _is_junk_text(s) is False, s


def test_junk_text_long_block_not_judged():
    """Длинный абзац с вкраплённым мусором НЕ удаляется целиком."""
    s = ("rN Если, ero, - f 1. Перед ПАО «Сбербанк» общая сумма "
         "задолженности составляет 9 713 руб. 03 коп")
    assert _is_junk_text(s) is False


#  OCR_FIXES — инлайн-чистка тела (clean_body_text)

from docling_dev.ocr_fixes import clean_body_text as _clean


def test_clean_glued_close_quote():
    """Закрывающая кавычка, приклеенная к слову, и непарные — снимаются."""
    assert _clean("14. ООО «ПКО »Санколлект»") == "14. ООО «ПКО Санколлект»"
    assert _clean("»Санколлект") == "Санколлект"


def test_clean_unmatched_brackets():
    """Непарные скобки удаляются, парные — сохраняются."""
    assert _clean("текст (без закрытия") == "текст без закрытия"
    assert _clean("ст.213.4 ФЗ (О банкротстве)") == "ст.213.4 ФЗ (О банкротстве)"


def test_clean_noise_tokens():
    """Одиночная латиница, шум-символы, одинокие кавычки — убираются."""
    assert _clean("- f '1. Перед ПАО «Сбербанк»") == "- 1. Перед ПАО «Сбербанк»"
    assert _clean("исполнены. * Кроме , ero") == "исполнены. Кроме ,"
    assert _clean("заявления ' Должником не") == "заявления Должником не"


def test_clean_homoglyph_words_not_lost():
    """Латиница-гомоглиф реального слова («Ha»→«На», «He»→«Не») конвертируется,
    НЕ удаляется как латиница-мусор (иначе теряется отрицание/смысл)."""
    assert _clean("на сумму ‚ Ha сумму") == "на сумму Ha сумму".replace("Ha", "На")
    assert _clean("Должником He совершались") == "Должником Не совершались"
    # настоящая латиница-мусор при этом удаляется
    assert _clean("Кроме ero и ty") == "Кроме и"


def test_clean_preserves_content():
    """Не трогаем: 4+ латиницу (бренды), числа с буквой, предлоги, длинные слова."""
    assert _clean("автомобиль NISSAN PRIMERA, 2003 года") == \
        "автомобиль NISSAN PRIMERA, 2003 года"
    assert _clean("км 22-Й (Киевское) двлд. 6") == "км 22-Й (Киевское) двлд. 6"
    assert _clean("долями в уставном капитале") == "долями в уставном капитале"
    assert _clean("Форте Пром ГМбХ") == "Форте Пром ГМбХ"
    # сокращения буква+точка (пункт/лист/город) — не мусор
    assert _clean("Согласно п. 1 Постановления") == "Согласно п. 1 Постановления"
    assert _clean("на 1 л. в 1 экз.") == "на 1 л. в 1 экз."
    assert _clean("г. Ростов, д. 5") == "г. Ростов, д. 5"
    # заглавный короткий лат. код (модель авто) — не мусор
    assert _clean("Автомобиль КИА JF (ОПТИМА)") == "Автомобиль КИА JF (ОПТИМА)"
    # открывающая « разорванной между блоками цитаты — не удаляем (нет закрытия)
    assert _clean("№ 351 «Об утверждении Порядка выбора") == \
        "№ 351 «Об утверждении Порядка выбора"


def test_clean_standalone_punct_dropped():
    """Одиночный токен-пунктуация («!», «:», «;», «&») — OCR-шум, убирается.
    Тире и приклеенное двоеточие (легитимные) — сохраняются."""
    assert _clean("наступил !") == "наступил"
    assert _clean("не ; исполнены") == "не исполнены"
    assert _clean("обстоятельств : &") == "обстоятельств"
    # приклеенное двоеточие в конце слова — легитимно, не трогаем
    assert _clean("следующих обстоятельств:") == "следующих обстоятельств:"
    # тире между словами — легитимно
    assert _clean("Должник — не имеет") == "Должник — не имеет"


def test_clean_leading_list_junk():
    """Ведущий мусор перед номером пункта / словом срезается слева."""
    assert _clean(".1. Документ") == "1. Документ"
    assert _clean("#8. Справка") == "8. Справка"
    assert _clean(". Справка об отсутствии") == "Справка об отсутствии"
    assert _clean("&03 июля 2026 г.") == "03 июля 2026 г."
    # тире-маркер списка и открывающая кавычка/скобка слева — НЕ трогаем
    assert _clean("- по договору займа") == "- по договору займа"
    assert _clean("«Об утверждении Порядка") == "«Об утверждении Порядка"


def test_clean_dup_function_word():
    """Дубль служебного слова подряд («в в») схлопывается; знач. слова — нет."""
    assert _clean("долями в в уставном капитале") == "долями в уставном капитале"
    assert _clean("и и по уплате") == "и по уплате"
    # повтор незначащего слова из белого списка не трогаем: «отчет отчет» — не служебное
    assert _clean("кредитный отчет отчет") == "кредитный отчет отчет"


def test_clean_rejoin_split_words():
    """Расщеплённое OCR-слово из двух строчных кир-фрагментов склеивается, если
    даёт словарное слово; ФИО (с заглавной) и пары валидных слов — не трогаем."""
    assert _clean("государ ственной пошлины") == "государственной пошлины"
    # ФИО с заглавной буквы НЕ склеиваем (защита имён)
    assert _clean("отношении Ми ирского Алексея") == "отношении Ми ирского Алексея"
    # два валидных слова подряд не сливаем
    assert _clean("по уплате налога") == "по уплате налога"
    assert _clean("в течение трех месяцев") == "в течение трех месяцев"


from docling_dev.converter import _merge_header_lines as _merge_hdr


def test_merge_header_lines_fns_staircase():
    """Пословно-разорванная строка шапки ФНС (один top, растущий x0) склеивается
    в один блок; левый штамп той же строки (большой зазор) — отдельно; тело ниже
    заголовка `ЗАЯВЛЕНИЕ` не трогается."""
    items = [
        _line_frag(95, 277, 118, h=12, text="МИНФИН РОССИИ ФНС"),   # 0 левый штамп
        _line_frag(334, 393, 120, h=9,  text="Заявитель:"),          # 1 правый
        _line_frag(397, 462, 120, h=9,  text="ФНС России"),          # 2 → склеить с 1
        _line_frag(334, 339, 135, h=8,  text="в"),                   # 3
        _line_frag(342, 366, 135, h=8,  text="лице"),                # 4
        _line_frag(369, 442, 135, h=8,  text="Межрайонной"),         # 5
        _line_frag(446, 480, 135, h=8,  text="ИФНС"),                # 6
        _line_frag(484, 519, 135, h=8,  text="России"),              # 7
        _line_frag(526, 530, 135, h=8,  text="№"),                   # 8
        _line_frag(537, 548, 135, h=8,  text="26"),                  # 9
        _line_frag(314, 384, 300, h=9,  text="ЗАЯВЛЕНИЕ"),           # 10 стоп-заголовок
        _line_frag(85, 560, 320, h=12, text="о включении требований в реестр"),  # 11 тело
    ]
    skip: set = set()
    n = _merge_hdr(items, {1: (595.0, 842.0)}, pdf_native=False, skip_indices=skip)
    assert items[1][0].text == "Заявитель: ФНС России"
    assert items[3][0].text == "в лице Межрайонной ИФНС России № 26"
    assert {2, 4, 5, 6, 7, 8, 9}.issubset(skip)          # хвостовые фрагменты
    assert items[0][0].text == "МИНФИН РОССИИ ФНС"        # левый штамп не тронут
    assert 0 not in skip and 11 not in skip              # штамп и тело целы
    assert items[3][0].prov[0].bbox.r == 548             # bbox.r расширен до конца строки
    assert n == 7


def test_merge_header_lines_skips_long_fragments():
    """Соседний фрагмент >3 слов не сливается — защита абзацев тела."""
    items = [
        _line_frag(334, 400, 120, text="Заявитель:"),
        _line_frag(405, 560, 120, text="ФНС России по большой длинной области тут"),
        _line_frag(314, 384, 300, text="ЗАЯВЛЕНИЕ"),
    ]
    skip: set = set()
    _merge_hdr(items, {1: (595.0, 842.0)}, pdf_native=False, skip_indices=skip)
    assert items[0][0].text == "Заявитель:"
    assert 1 not in skip


def test_sort_tall_picture_does_not_chain_lines():
    """Высокая картинка (пятно/логотип) перекрывает несколько строк по
    вертикали, но НЕ сцепляет их в один ряд: строки сохраняют порядок по Y."""
    def _pic(x0, x1, top, h):
        bbox = types.SimpleNamespace(l=x0, r=x1, t=top, b=top + h)
        prov = types.SimpleNamespace(bbox=bbox, page_no=1, page_w=595.0, page_h=842.0)
        return (types.SimpleNamespace(label="picture", prov=[prov], text=""), 0)
    items = [
        _pic(40, 70, 98, 60),                                   # пятно на 3 строки
        _line_frag(430, 560, 100, text="2. ПАО МФК Займер"),
        _line_frag(360, 560, 116, text="ИНН 5406836941"),
        _line_frag(280, 560, 132, text="Адрес: 630099"),
    ]
    out = sort_reading_order(list(items), pdf_native=False)
    texts = [it.text for it, _ in out if it.text]
    assert texts == ["2. ПАО МФК Займер", "ИНН 5406836941", "Адрес: 630099"]


#  OCR FIXES — латиница после дат, суммы, капс-аббревиатуры

@pytest.mark.parametrize("inp,expected", [
    # латинская r после года → «г.»
    ("по сроку оплаты 28.10.2024 r., дата",  "по сроку оплаты 28.10.2024 г., дата"),
    ("28.12.2023 r. в сумме",                "28.12.2023 г. в сумме"),
    # латиница в денежных сокращениях
    ("2 118 547,65 py6., штрафы",            "2 118 547,65 руб., штрафы"),
    ("800 руб. 24 kon.",                     "800 руб. 24 коп."),
    ("507 руб. 25 кон. из которых",          "507 руб. 25 коп. из которых"),
    # капс-аббревиатуры: латиница-двойник → кириллица
    ("решение KHII о привлечении",           "решение КНП о привлечении"),
    ("решение КНII о привлечении",           "решение КНП о привлечении"),
    ("взносов COB, занимающихся",            "взносов СОВ, занимающихся"),
    # марки/латинские аббревиатуры НЕ трогаем
    ("автомобиль NISSAN PRIMERA, 2003",      "автомобиль NISSAN PRIMERA, 2003"),
    ("двигатель BMW не менялся",             "двигатель BMW не менялся"),
])
def test_latin_lookalike_fixes(inp, expected):
    assert postprocess(inp) == expected


#  CONVERTER — детект углового штампа (шапка ФНС)

from docling_dev.converter import _detect_corner_letterhead


def _zone_entry(x0, x1, top, text, h=12.0):
    bbox = types.SimpleNamespace(l=x0, r=x1, t=top, b=top + h)
    return (0, types.SimpleNamespace(text=text), bbox, text)


def _bbox_ns(l, r, t=100, b=180):
    return types.SimpleNamespace(l=l, r=r, t=t, b=b)


def test_corner_detected_fns():
    """ФНС-шапка правовыровненная (Зайцев): герб слева, левый кластер с маркером
    налоговой шапки, правый кластер прижат к полю → right_align=RIGHT."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    pw = 595.0
    left_texts = ["ФЕДЕРАЛЬНАЯ НАЛОГОВАЯ СЛУЖБА", "УПРАВЛЕНИЕ ФНС по области",
                  "МЕЖРАЙОННАЯ ИНСПЕКЦИЯ", "стр 4", "стр 5"]
    zone = (
        [_zone_entry(60, 250, 100 + i * 15, left_texts[i]) for i in range(5)]
        + [_zone_entry(320, 560, 100 + i * 15, f"адресат {i}") for i in range(4)]
    )
    res = _detect_corner_letterhead(zone, _bbox_ns(120, 190), pw)
    assert res is not None
    left, right, right_align = res
    assert len(left) == 5 and len(right) == 4
    assert right_align == WD_ALIGN_PARAGRAPH.RIGHT


def test_corner_detected_fns_left_aligned():
    """ФНС-шапка левовыровненная (Артемов/Геворгян/6л): адресат с общим левым
    краем и рваным правым → детект проходит, right_align=LEFT."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    pw = 595.0
    left_texts = ["ФЕДЕРАЛЬНАЯ НАЛОГОВАЯ СЛУЖБА", "УПРАВЛЕНИЕ ФНС",
                  "МЕЖРАЙОННАЯ ИНСПЕКЦИЯ", "l4", "l5"]
    # правый кластер левовыровнен по x0=340, правый край рваный (400..480)
    zone = (
        [_zone_entry(60, 250, 100 + i * 15, left_texts[i]) for i in range(5)]
        + [_zone_entry(340, 400 + i * 20, 100 + i * 15, f"адресат {i}") for i in range(5)]
    )
    res = _detect_corner_letterhead(zone, _bbox_ns(120, 190), pw)
    assert res is not None
    _left, _right, right_align = res
    assert right_align == WD_ALIGN_PARAGRAPH.LEFT


def test_corner_not_detected_non_fns():
    """Левый кластер без маркеров ФНС — не наша шапка (защита от ложных
    срабатываний на чужих двухколоночных раскладках)."""
    pw = 595.0
    zone = (
        [_zone_entry(60, 250, 100 + i * 15, f"колонка {i}") for i in range(4)]
        + [_zone_entry(320, 560, 100 + i * 15, f"адресат {i}") for i in range(4)]
    )
    assert _detect_corner_letterhead(zone, _bbox_ns(120, 190), pw) is None


def test_corner_not_detected_logo_right():
    """Герб/лого в правой половине — не угловой штамп."""
    pw = 595.0
    zone = (
        [_zone_entry(60, 250, 100 + i * 15, f"л{i}") for i in range(4)]
        + [_zone_entry(320, 560, 100 + i * 15, f"п{i}") for i in range(4)]
    )
    assert _detect_corner_letterhead(zone, _bbox_ns(400, 470), pw) is None


def test_corner_not_detected_spanning_block():
    """Блок через обе половины страницы — не двухколоночная шапка."""
    pw = 595.0
    zone = (
        [_zone_entry(60, 250, 100 + i * 15, f"л{i}") for i in range(4)]
        + [_zone_entry(320, 560, 100 + i * 15, f"п{i}") for i in range(4)]
        + [_zone_entry(100, 500, 180, "широкий блок")]
    )
    assert _detect_corner_letterhead(zone, _bbox_ns(120, 190), pw) is None


def test_corner_not_detected_few_blocks():
    """Меньше 3 блоков в кластере (лого слева от реквизитов, Центр-инвест) — не corner."""
    pw = 595.0
    zone = (
        [_zone_entry(60, 250, 100, "одна строка")]
        + [_zone_entry(320, 560, 100 + i * 15, f"п{i}") for i in range(5)]
    )
    assert _detect_corner_letterhead(zone, _bbox_ns(120, 190), pw) is None


def test_corner_not_detected_scattered_right():
    """Правый кластер БЕЗ единой колонки — две подколонки метка/значение с
    разбросом и левого, и правого края (меточная шапка). Не адресат, не corner
    даже при налоговом левом кластере."""
    pw = 595.0
    left_texts = ["ФЕДЕРАЛЬНАЯ НАЛОГОВАЯ СЛУЖБА", "УПРАВЛЕНИЕ", "ИНСПЕКЦИЯ", "l4"]
    # x0 скачет между 300 (метки) и 430 (значения), правый край тоже рваный
    xs = [(300, 360), (430, 470), (300, 355), (430, 480), (300, 350), (430, 465)]
    zone = (
        [_zone_entry(60, 250, 100 + i * 15, left_texts[i]) for i in range(4)]
        + [_zone_entry(x0, x1, 100 + i * 15, f"п{i}") for i, (x0, x1) in enumerate(xs)]
    )
    assert _detect_corner_letterhead(zone, _bbox_ns(120, 190), pw) is None
