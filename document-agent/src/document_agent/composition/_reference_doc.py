"""Generate a pandoc reference DOCX from style definitions."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from zipfile import ZipFile

logger = logging.getLogger(__name__)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def parse_styles_from_markdown(md_text: str) -> dict | None:
    """Extract styles dict from YAML front matter in markdown.

    Returns None if no styles block is present.
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", md_text, re.DOTALL)
    if not m:
        return None
    yaml_block = m.group(1)

    # Lightweight YAML parsing (no PyYAML dependency needed)
    # Handles our simple nested structure: styles: / section: / key: value
    result: dict = {}
    current_section: dict | None = None
    current_key: str | None = None

    for line in yaml_block.split("\n"):
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#"):
            continue

        # Top level: "styles:"
        if stripped == "styles:":
            continue

        # Section level: "  body:" / "  heading1:" / "  page:"
        indent = len(line) - len(line.lstrip())
        if indent == 2 and stripped.endswith(":"):
            current_key = stripped.strip().rstrip(":")
            current_section = {}
            result[current_key] = current_section
            continue

        # Property level: "    font: "Times New Roman""
        if indent >= 4 and current_section is not None and ":" in stripped:
            k, _, v = stripped.strip().partition(":")
            raw = v.strip()
            was_quoted = (raw.startswith('"') and raw.endswith('"')) or (
                raw.startswith("'") and raw.endswith("'")
            )
            v = raw.strip('"').strip("'")
            if v.lower() == "true":
                current_section[k] = True
            elif v.lower() == "false":
                current_section[k] = False
            elif was_quoted:
                current_section[k] = v
            else:
                try:
                    current_section[k] = int(v)
                except ValueError:
                    try:
                        current_section[k] = float(v)
                    except ValueError:
                        current_section[k] = v

    return result if result else None


def generate_reference_doc(
    styles: dict,
    output_path: Path,
    header_footer_source: Path | None = None,
) -> Path:
    """Generate a minimal DOCX reference document with the given styles.

    Args:
        styles: Dict with keys like "body", "heading1", "page", etc.
        output_path: Where to write the reference DOCX.
        header_footer_source: Optional DOCX to copy headers/footers from.

    Returns:
        Path to the generated reference DOCX.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles_xml = _build_styles_xml(styles)
    doc_xml = _build_document_xml(styles)
    content_types = _CONTENT_TYPES
    doc_rels = _DOC_RELS

    # Extract headers/footers from source DOCX if provided
    hf_files: dict[str, bytes] = {}
    hf_rels: list[str] = []
    hf_ct_overrides: list[str] = []
    if header_footer_source and header_footer_source.exists():
        hf_files, hf_rels, hf_ct_overrides, sect_xml = _extract_headers_footers(header_footer_source)
        if sect_xml:
            # Inject header/footer references into the sectPr
            doc_xml = _inject_hf_into_document(doc_xml, sect_xml)
        if hf_ct_overrides:
            # Add content type overrides
            insert_before = "</Types>"
            ct_additions = "\n  ".join(hf_ct_overrides)
            content_types = content_types.replace(insert_before, f"  {ct_additions}\n{insert_before}")
        if hf_rels:
            # Add relationship entries
            insert_before = "</Relationships>"
            rel_additions = "\n  ".join(hf_rels)
            doc_rels = doc_rels.replace(insert_before, f"  {rel_additions}\n{insert_before}")

    with ZipFile(output_path, "w") as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", styles_xml)
        z.writestr("word/document.xml", doc_xml)
        for name, data in hf_files.items():
            z.writestr(name, data)

    logger.info("Generated reference doc: %s", output_path)
    return output_path


def _extract_headers_footers(
    source: Path,
) -> tuple[dict[str, bytes], list[str], list[str], str]:
    """Extract header/footer files, relationships, and content types from a DOCX.

    Returns (files, rel_entries, ct_overrides, sectPr_hf_refs).
    """
    import re as _re

    files: dict[str, bytes] = {}
    rel_entries: list[str] = []
    ct_overrides: list[str] = []
    sect_hf_refs = ""

    with ZipFile(source) as z:
        # Read document.xml.rels to find header/footer relationships
        if "word/_rels/document.xml.rels" not in z.namelist():
            return files, rel_entries, ct_overrides, sect_hf_refs

        rels_xml = z.read("word/_rels/document.xml.rels").decode("utf-8")

        hf_types = {
            "header": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
            "footer": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer",
        }

        for rel_match in _re.finditer(
            r'<Relationship\s+Id="([^"]+)"\s+'
            r'Type="([^"]+)"\s+'
            r'Target="([^"]+)"',
            rels_xml,
        ):
            rid, rtype, target = rel_match.groups()
            is_header = rtype == hf_types["header"]
            is_footer = rtype == hf_types["footer"]
            if not (is_header or is_footer):
                continue

            member = f"word/{target}"
            if member in z.namelist():
                files[member] = z.read(member)
                rel_entries.append(f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>')
                kind = "header" if is_header else "footer"
                ct_overrides.append(
                    f'<Override PartName="/{member}" '
                    f'ContentType="application/vnd.openxmlformats-officedocument.'
                    f'wordprocessingml.{kind}+xml"/>'
                )

        # Extract headerReference/footerReference elements from sectPr
        if "word/document.xml" in z.namelist():
            doc = z.read("word/document.xml").decode("utf-8")
            refs = _re.findall(r"(<w:(?:headerReference|footerReference)[^/]*/>)", doc)
            sect_hf_refs = "\n    ".join(refs)

    return files, rel_entries, ct_overrides, sect_hf_refs


def _inject_hf_into_document(doc_xml: str, sect_hf_refs: str) -> str:
    """Inject header/footer references into the document's sectPr."""
    import re as _re

    # Insert before the closing </w:sectPr>
    return _re.sub(
        r"(</w:sectPr>)",
        f"    {sect_hf_refs}\n  \\1",
        doc_xml,
    )


def _cm_to_twips(cm_str: str) -> int:
    """Convert a string like '2.5cm' to twips."""
    val = float(cm_str.replace("cm", "").strip())
    return round(val * 567)


def _build_rpr(props: dict) -> str:
    """Build a w:rPr XML fragment from a style props dict."""
    parts = []
    if "font" in props:
        f = props["font"]
        parts.append(f'<w:rFonts w:ascii="{f}" w:hAnsi="{f}" w:cs="{f}"/>')
    if "size" in props:
        half_pts = int(props["size"]) * 2
        parts.append(f'<w:sz w:val="{half_pts}"/>')
        parts.append(f'<w:szCs w:val="{half_pts}"/>')
    if props.get("bold"):
        parts.append("<w:b/>")
    if props.get("italic"):
        parts.append("<w:i/>")
    if "color" in props:
        parts.append(f'<w:color w:val="{props["color"]}"/>')
    return "".join(parts)


def _build_ppr(props: dict) -> str:
    """Build a w:pPr XML fragment for spacing."""
    spacing_attrs = []
    if "spacing-before" in props:
        spacing_attrs.append(f'w:before="{_cm_to_twips(props["spacing-before"])}"')
    if "spacing-after" in props:
        spacing_attrs.append(f'w:after="{_cm_to_twips(props["spacing-after"])}"')
    if "line-spacing" in props:
        line_val = round(float(props["line-spacing"]) * 240)
        spacing_attrs.append(f'w:line="{line_val}"')
    if not spacing_attrs:
        return ""
    return f"<w:spacing {' '.join(spacing_attrs)}/>"


def _build_styles_xml(styles: dict) -> str:
    """Build a complete word/styles.xml from the styles dict."""
    body = styles.get("body", {})
    body_rpr = _build_rpr(body)

    # docDefaults
    doc_defaults = f"""<w:docDefaults>
    <w:rPrDefault><w:rPr>{body_rpr}</w:rPr></w:rPrDefault>
  </w:docDefaults>"""

    # Normal style
    normal_ppr = _build_ppr(body)
    normal_style = f"""<w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:pPr>{normal_ppr}</w:pPr>
    <w:rPr>{body_rpr}</w:rPr>
  </w:style>"""

    # Heading styles
    heading_styles = []
    for level in range(1, 4):
        key = f"heading{level}"
        hprops = styles.get(key, {})
        if not hprops:
            # Default: inherit body font, increase size, add bold
            hprops = dict(body)
            hprops["bold"] = True
            hprops["size"] = body.get("size", 11) + (4 - level) * 2

        hrpr = _build_rpr(hprops)
        hppr = _build_ppr(hprops)
        outline = f'<w:outlineLvl w:val="{level - 1}"/>'
        heading_styles.append(
            f"""<w:style w:type="paragraph" w:styleId="Heading{level}">
    <w:name w:val="heading {level}"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:pPr>{hppr}{outline}</w:pPr>
    <w:rPr>{hrpr}</w:rPr>
  </w:style>"""
        )

    # Table style with borders
    table = styles.get("table", {})
    table_font_size = table.get("size", body.get("size", 11))
    table_half_pts = int(table_font_size) * 2
    border_color = table.get("border-color", "000000")
    border_size = table.get("border-size", 4)  # in eighths of a point
    border_xml = (
        f'<w:top w:val="single" w:sz="{border_size}" w:space="0" w:color="{border_color}"/>'
        f'<w:left w:val="single" w:sz="{border_size}" w:space="0" w:color="{border_color}"/>'
        f'<w:bottom w:val="single" w:sz="{border_size}" w:space="0" w:color="{border_color}"/>'
        f'<w:right w:val="single" w:sz="{border_size}" w:space="0" w:color="{border_color}"/>'
        f'<w:insideH w:val="single" w:sz="{border_size}" w:space="0" w:color="{border_color}"/>'
        f'<w:insideV w:val="single" w:sz="{border_size}" w:space="0" w:color="{border_color}"/>'
    )
    table_style = f"""<w:style w:type="table" w:styleId="Table">
    <w:name w:val="Table"/>
    <w:tblPr>
      <w:tblBorders>{border_xml}</w:tblBorders>
      <w:tblCellMar>
        <w:top w:w="40" w:type="dxa"/>
        <w:left w:w="60" w:type="dxa"/>
        <w:bottom w:w="40" w:type="dxa"/>
        <w:right w:w="60" w:type="dxa"/>
      </w:tblCellMar>
    </w:tblPr>
    <w:rPr><w:sz w:val="{table_half_pts}"/><w:szCs w:val="{table_half_pts}"/></w:rPr>
  </w:style>"""

    # Compact list style
    compact_style = """<w:style w:type="paragraph" w:styleId="Compact">
    <w:name w:val="Compact"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>
  </w:style>"""

    # Footnote and caption styles (9pt by default, or from table.size)
    small_size = int(table.get("size", 9)) * 2
    body_font = body.get("font", "Calibri")
    small_font_rpr = (
        f'<w:rFonts w:ascii="{body_font}" w:hAnsi="{body_font}" w:cs="{body_font}"/>'
        f'<w:sz w:val="{small_size}"/><w:szCs w:val="{small_size}"/>'
    )

    footnote_style = f"""<w:style w:type="paragraph" w:styleId="FootnoteText">
    <w:name w:val="footnote text"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:before="0" w:after="0" w:line="240"/></w:pPr>
    <w:rPr>{small_font_rpr}</w:rPr>
  </w:style>"""

    caption_style = f"""<w:style w:type="paragraph" w:styleId="Caption">
    <w:name w:val="caption"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:before="0" w:after="60"/></w:pPr>
    <w:rPr>{small_font_rpr}<w:i/></w:rPr>
  </w:style>"""

    # Table caption / image caption (pandoc uses "Table Caption" and "Image Caption")
    table_caption_style = f"""<w:style w:type="paragraph" w:styleId="TableCaption">
    <w:name w:val="Table Caption"/>
    <w:basedOn w:val="Caption"/>
    <w:rPr>{small_font_rpr}<w:i/></w:rPr>
  </w:style>"""

    image_caption_style = f"""<w:style w:type="paragraph" w:styleId="ImageCaption">
    <w:name w:val="Image Caption"/>
    <w:basedOn w:val="Caption"/>
    <w:rPr>{small_font_rpr}<w:i/></w:rPr>
  </w:style>"""

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{_W}">
  {doc_defaults}
  {normal_style}
  {"".join(heading_styles)}
  {table_style}
  {compact_style}
  {footnote_style}
  {caption_style}
  {table_caption_style}
  {image_caption_style}
</w:styles>"""


def _build_document_xml(styles: dict) -> str:
    """Build document.xml with page properties from styles."""
    page = styles.get("page", {})

    # Page size
    size = page.get("size", "A4")
    if size == "A4":
        pg_w, pg_h = 11906, 16838
    elif size == "Letter":
        pg_w, pg_h = 12240, 15840
    else:
        pg_w = _cm_to_twips(page.get("width", "21.0cm"))
        pg_h = _cm_to_twips(page.get("height", "29.7cm"))

    # Margins
    mt = _cm_to_twips(page.get("margin-top", "2.5cm"))
    mb = _cm_to_twips(page.get("margin-bottom", "2.5cm"))
    ml = _cm_to_twips(page.get("margin-left", "2.5cm"))
    mr = _cm_to_twips(page.get("margin-right", "2.5cm"))

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_W}">
<w:body>
  <w:p><w:r><w:t></w:t></w:r></w:p>
  <w:sectPr>
    <w:pgSz w:w="{pg_w}" w:h="{pg_h}"/>
    <w:pgMar w:top="{mt}" w:right="{mr}" w:bottom="{mb}" w:left="{ml}"
             w:header="720" w:footer="720" w:gutter="0"/>
  </w:sectPr>
</w:body>
</w:document>"""


# Boilerplate XML
_CONTENT_TYPES = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_ROOT_RELS = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
    Target="styles.xml"/>
</Relationships>"""
