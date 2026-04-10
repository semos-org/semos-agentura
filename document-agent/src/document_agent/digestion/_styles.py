"""Extract document styles from DOCX for YAML front matter."""

from __future__ import annotations

import logging
from pathlib import Path
from zipfile import ZipFile

from lxml import etree

logger = logging.getLogger(__name__)

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
WVAL = f"{{{NS['w']}}}val"


def extract_styles(file_path: Path) -> dict:
    """Extract style metadata from a DOCX file.

    Returns a dict suitable for YAML front matter:
        {
            "page": {"size": "A4", "margin-top": "2.5cm", ...},
            "body": {"font": "Calibri", "size": 11, ...},
            "heading1": {"font": "Arial", "size": 14, "bold": true, ...},
            ...
        }
    """
    result: dict = {}
    with ZipFile(file_path) as z:
        if "word/styles.xml" in z.namelist():
            styles_root = _parse_xml(z, "word/styles.xml")
            result.update(_extract_text_styles(styles_root))
        if "word/document.xml" in z.namelist():
            doc_root = _parse_xml(z, "word/document.xml")
            page = _extract_page_props(doc_root)
            if page:
                result["page"] = page
    return result


def format_yaml_frontmatter(styles: dict) -> str:
    """Format extracted styles as YAML front matter block."""
    if not styles:
        return ""
    lines = ["---", "styles:"]
    for section_key in ["page", "body", "heading1", "heading2", "heading3"]:
        section = styles.get(section_key)
        if not section:
            continue
        lines.append(f"  {section_key}:")
        for k, v in section.items():
            if isinstance(v, bool):
                lines.append(f"    {k}: {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f'    {k}: "{v}"')
            else:
                lines.append(f"    {k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _parse_xml(z: ZipFile, member: str) -> etree._Element:
    with z.open(member) as f:
        parser = etree.XMLParser(remove_blank_text=False)
        return etree.parse(f, parser).getroot()


def _extract_text_styles(root: etree._Element) -> dict:
    """Extract body and heading styles from styles.xml."""
    result: dict = {}

    # docDefaults -> body baseline
    dd = root.find(".//w:docDefaults/w:rPrDefault/w:rPr", NS)
    if dd is not None:
        body = _rpr_to_dict(dd)
        if body:
            result["body"] = body

    # Named styles
    style_map = {
        "Normal": "body",
        "Heading1": "heading1",
        "heading1": "heading1",
        "Heading2": "heading2",
        "heading2": "heading2",
        "Heading3": "heading3",
        "heading3": "heading3",
    }

    for style in root.findall(".//w:style", NS):
        sid = style.get(f"{{{NS['w']}}}styleId", "")
        target = style_map.get(sid)
        if not target:
            continue

        props: dict = {}
        rpr = style.find("w:rPr", NS)
        if rpr is not None:
            props.update(_rpr_to_dict(rpr))

        ppr = style.find("w:pPr", NS)
        if ppr is not None:
            spacing = ppr.find("w:spacing", NS)
            if spacing is not None:
                before = spacing.get(f"{{{NS['w']}}}before")
                after = spacing.get(f"{{{NS['w']}}}after")
                line = spacing.get(f"{{{NS['w']}}}line")
                if before:
                    props["spacing-before"] = f"{int(before) / 567:.1f}cm"
                if after:
                    props["spacing-after"] = f"{int(after) / 567:.1f}cm"
                if line:
                    props["line-spacing"] = round(int(line) / 240, 2)

        if props:
            if target in result:
                result[target].update(props)
            else:
                result[target] = props

    return result


def _rpr_to_dict(rpr: etree._Element) -> dict:
    """Convert a w:rPr element to a flat dict."""
    d: dict = {}
    fonts = rpr.find("w:rFonts", NS)
    if fonts is not None:
        ascii_font = fonts.get(f"{{{NS['w']}}}ascii")
        if ascii_font:
            d["font"] = ascii_font

    sz = rpr.find("w:sz", NS)
    if sz is not None:
        half_pts = sz.get(WVAL)
        if half_pts:
            d["size"] = int(half_pts) // 2

    if rpr.find("w:b", NS) is not None:
        d["bold"] = True
    if rpr.find("w:i", NS) is not None:
        d["italic"] = True

    color = rpr.find("w:color", NS)
    if color is not None:
        val = color.get(WVAL)
        if val and val != "auto":
            d["color"] = val

    return d


def _extract_page_props(doc_root: etree._Element) -> dict:
    """Extract page size and margins from the last sectPr."""
    # Last sectPr in body defines the document's page layout
    sects = doc_root.findall(".//" + f"{{{NS['w']}}}sectPr")
    if not sects:
        return {}
    sect = sects[-1]

    result: dict = {}
    pgSz = sect.find("w:pgSz", NS)
    if pgSz is not None:
        w = int(pgSz.get(f"{{{NS['w']}}}w", "0"))
        h = int(pgSz.get(f"{{{NS['w']}}}h", "0"))
        # Detect standard sizes
        if abs(w - 11906) < 100 and abs(h - 16838) < 100:
            result["size"] = "A4"
        elif abs(w - 12240) < 100 and abs(h - 15840) < 100:
            result["size"] = "Letter"
        else:
            result["width"] = f"{w / 567:.1f}cm"
            result["height"] = f"{h / 567:.1f}cm"

    pgMar = sect.find("w:pgMar", NS)
    if pgMar is not None:
        for side in ["top", "bottom", "left", "right"]:
            val = pgMar.get(f"{{{NS['w']}}}{side}")
            if val:
                result[f"margin-{side}"] = f"{int(val) / 567:.1f}cm"

    return result
