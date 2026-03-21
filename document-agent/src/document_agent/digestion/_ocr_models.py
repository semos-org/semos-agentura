"""Normalized OCR response models."""

from __future__ import annotations

import re
from typing import Any


class OCRImage:
    """Normalized image from an OCR response."""

    def __init__(self, data: dict):
        self.id: str = data.get("id", "")
        self.image_base64: str | None = data.get("image_base64")
        self.image_annotation: str | None = data.get("image_annotation")


class OCRTable:
    """Table extracted by OCR, referenced as [tbl-N.md] in the markdown."""

    def __init__(self, data: dict):
        self.id: str = data.get("id", "")
        self.content: str = data.get("content", "")
        self.format: str = data.get("format", "markdown")


class OCRPage:
    """Normalized page from an OCR response."""

    def __init__(self, data: dict):
        self.index: int = data.get("index", 0)
        self.markdown: str = data.get("markdown", "")
        raw_images = data.get("images") or []
        self.images: list[OCRImage] = [OCRImage(i) for i in raw_images]
        raw_tables = data.get("tables") or []
        self.tables: list[OCRTable] = [
            OCRTable(t) for t in raw_tables
        ]

    def resolve_tables(self) -> str:
        """Return markdown with [tbl-N.ext](tbl-N.ext) refs replaced
        by inline table content."""
        if not self.tables:
            return self.markdown
        table_map = {t.id: t.content for t in self.tables}
        def _replace(m: re.Match) -> str:
            tid = m.group(1)
            return table_map.get(tid, m.group(0))
        return re.sub(
            r'\[([^\]]+)\]\(\1\)',
            _replace,
            self.markdown,
        )


class OCRResponse:
    """Normalized OCR response that works for both providers."""

    def __init__(self, data: Any):
        if isinstance(data, dict):
            self.pages = [OCRPage(p) for p in data.get("pages", [])]
            self.document_annotation = data.get("document_annotation")
        else:
            # Mistral SDK response object
            self.pages = []
            for p in data.pages:
                page_dict: dict[str, Any] = {
                    "index": p.index,
                    "markdown": p.markdown,
                    "images": [],
                    "tables": [],
                }
                if p.images:
                    for img in p.images:
                        img_dict: dict[str, Any] = {"id": img.id}
                        if hasattr(img, "image_base64"):
                            img_dict["image_base64"] = img.image_base64
                        if hasattr(img, "image_annotation"):
                            img_dict["image_annotation"] = img.image_annotation
                        page_dict["images"].append(img_dict)
                if hasattr(p, "tables") and p.tables:
                    for tbl in p.tables:
                        tbl_dict: dict[str, Any] = {
                            "id": getattr(tbl, "id", ""),
                            "content": getattr(tbl, "content", ""),
                            "format": getattr(tbl, "format", "markdown"),
                        }
                        page_dict["tables"].append(tbl_dict)
                self.pages.append(OCRPage(page_dict))
            self.document_annotation = getattr(data, "document_annotation", None)
