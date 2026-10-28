"""docling_dev — модульный конвертер отсканированных PDF → DOCX.

Имена отдаются лениво (PEP 562): импорт любого из них тянет docling с torch,
а это 15-40 с. Раньше за них платил каждый, кто просто поднимал сервер —
`from docling_dev.api import router` исполнял этот модуль целиком.

ВАЖНО ПРИ ДОБАВЛЕНИИ ИМЕНИ: его нужно вписать в `_ОТКУДА`, иначе `from
docling_dev import X` упадёт с AttributeError. Обычный `from .модуль import X`
наверху файла снова сделал бы импорт пакета дорогим.
"""

_ОТКУДА = {
    "convert_pdf": ".converter",
    # Версия, которая БРОСАЕТ ConvertError вместо возврата False (п.4.2):
    # HTTP-слой зовёт её, потому что ему нужна причина отказа.
    "convert_pdf_checked": ".converter",
    "ConvertError": ".errors",
    "DoclingBatchConverter": ".converter",
    "build_converter": ".pipeline",
    "detect_doc_type": ".doc_type",
    "DOC_LEGAL": ".doc_type",
    "DOC_GENERIC": ".doc_type",
    "analyse_pages": ".page_analyser",
    "PageInfo": ".page_analyser",
    "render_document": ".renderer",
}

__all__ = list(_ОТКУДА)


def __getattr__(name: str):
    module = _ОТКУДА.get(name)
    if module is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    from importlib import import_module
    return getattr(import_module(module, __name__), name)


def __dir__():
    return sorted(__all__)
