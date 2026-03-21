"""Exception hierarchy for the document agent."""


class DocumentAgentError(Exception):
    """Base exception for all document agent errors."""


class OCRError(DocumentAgentError):
    """OCR processing failed."""


class ProviderError(DocumentAgentError):
    """Provider initialization or API call error."""


class ConversionError(DocumentAgentError):
    """Office-to-PDF or document composition conversion failed."""


class ToolNotFoundError(DocumentAgentError):
    """An external CLI tool (marp, pandoc, mmdc, libreoffice) is not available."""


class CompositionError(DocumentAgentError):
    """Document composition failed."""


class MermaidRenderError(CompositionError):
    """Mermaid diagram rendering failed."""


class DrawioRenderError(CompositionError):
    """draw.io diagram rendering failed."""
