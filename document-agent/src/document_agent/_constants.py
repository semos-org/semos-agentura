"""Shared constants for file type detection and MIME mapping."""

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tiff", ".bmp"}
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp"}
SUPPORTED_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS | OFFICE_EXTENSIONS

MIME_MAP = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "tiff": "image/tiff",
    "bmp": "image/bmp",
}
