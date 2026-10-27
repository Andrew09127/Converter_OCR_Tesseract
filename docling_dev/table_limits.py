# -*- coding: utf-8 -*-
"""
table_limits.py — предел длины последовательности TableFormer.

Зачем. TableFormer генерирует структуру таблицы авторегрессивно, токен за
токеном (формат OTSL: по тегу на ячейку плюс перевод строки на ряд), и
останавливается, когда САМ решит выдать `<end>`. На грязных сканах он в этом
решении срывается: замер на `Артемов` — таблицы 4×6 и 6×3, которым нужно 28 и 24
токена, получили 266 и 248, по 18-20 секунд каждая. Это 37 из 59 секунд
конвертации файла.

Что проверено замером по всему корпусу сканов:
  * законные таблицы укладываются в 10-115 токенов (самые длинные — 17×5 и
    20×3, их последовательности 102 и 115);
  * срыв даёт 248-266 токенов на МАЛЕНЬКИХ таблицах;
  * хвост после срыва не влияет на результат: с пределом 96 на `Артемов`
    получались те же 2 таблицы, те же 26 ячеек и тот же текст ячеек, а время
    стадии падало с 32 до 13 секунд.

Правило: `cap = max(160, 2 × число текстовых ячеек + 32)`.
  * пол 160 — с запасом 1.4× над самой длинной законной последовательностью
    корпуса (115) и хватает на разреженную таблицу вроде 25×6, где текстовых
    ячеек мало, а пустых много;
  * линейная часть снимает ограничение с больших таблиц (347 текстовых ячеек →
    предел 726), чтобы длинные реестры кредиторов не обрезались.

Результат: `Артемов` 59.0 → 41.7 s (стадия таблиц 37.7 → 21.4 s), остальные
файлы корпуса не затронуты — их последовательности короче предела, вывод
побайтово тот же.

Это НЕ гейт по кандидатам, который предлагал план: замер показал, что кандидаты
на `Артемов` — настоящие таблицы с 18 и 46 текстовыми ячейками, а не рамки и
штампы. Дорого стоит не выбор кандидатов, а срыв генерации.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Подобрано замером (см. докстринг): пол, множитель и добавка.
SEQ_FLOOR = 160
SEQ_MULT = 2.0
SEQ_ADD = 32

_STATS = {"calls": 0, "capped": 0, "max_cap": 0}


def seq_cap(n_tokens: int) -> int:
    """Предел длины последовательности для кандидата с n_tokens текстовых ячеек."""
    return int(max(SEQ_FLOOR, SEQ_MULT * max(n_tokens, 0) + SEQ_ADD))


def table_limit_stats() -> dict:
    return dict(_STATS)


def install_table_seq_limit(cls=None) -> bool:
    """Ставит патч предела. Повторный вызов — no-op.

    `cls` — класс предиктора; по умолчанию `TFPredictor` из docling_ibm_models
    (параметр нужен тестам, чтобы не тащить модель на 400 МБ).
    """
    if cls is None:
        try:
            from docling_ibm_models.tableformer.data_management.tf_predictor import (
                TFPredictor,
            )
        except Exception as exc:                    # noqa: BLE001
            log.warning("table_limits: предиктор таблиц недоступен (%s)", exc)
            return False
        cls = TFPredictor

    if getattr(cls, "_seq_limit_installed", False):
        return True

    orig = cls.multi_table_predict

    def _patched(self, page_input, table_bboxes, do_matching=True, **kw):
        cap = seq_cap(len(page_input.get("tokens") or []))
        # Предел живёт на модели: TableFormer читает его в своём цикле генерации
        # (`_max_pred_len`), а аргумент `max_steps` из конфига не используется
        # вовсе — проверено, правка конфига ни на что не влияла.
        model = getattr(self, "_model", None)
        if model is not None and hasattr(model, "_max_pred_len"):
            model._max_pred_len = cap
            _STATS["capped"] += 1
            _STATS["max_cap"] = max(_STATS["max_cap"], cap)
        else:
            log.debug("table_limits: у предиктора нет _max_pred_len — предел не задан")
        _STATS["calls"] += 1
        return orig(self, page_input, table_bboxes, do_matching=do_matching, **kw)

    cls.multi_table_predict = _patched
    cls._seq_limit_installed = True
    log.info("table_limits: предел длины последовательности включён "
             "(пол %d, %.1f×ячеек + %d)", SEQ_FLOOR, SEQ_MULT, SEQ_ADD)
    return True
