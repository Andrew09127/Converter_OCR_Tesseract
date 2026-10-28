"""docling_dev — модульный конвертер отсканированных PDF → DOCX."""
from .converter import convert_pdf, convert_pdf_checked, DoclingBatchConverter
from .errors import ConvertError
from .pipeline import build_converter
from .doc_type import detect_doc_type, DOC_LEGAL, DOC_GENERIC
from .page_analyser import analyse_pages, PageInfo
from .renderer import render_document

__all__ = [
    "convert_pdf", "convert_pdf_checked", "ConvertError",
    "DoclingBatchConverter", "build_converter",
    "detect_doc_type", "DOC_LEGAL", "DOC_GENERIC",
    "analyse_pages", "PageInfo",
    "render_document",
]
