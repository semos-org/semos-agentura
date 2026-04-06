"""DOCX form filling and inspection.

Supports two types of Word form fields:
- Modern content controls (w:sdt) - date pickers, text, checkboxes, dropdowns
- Legacy form fields (w:fldChar + w:ffData) - text inputs, checkboxes

Uses lxml to preserve all namespace declarations in the original document.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from lxml import etree

logger = logging.getLogger(__name__)

# XML namespaces used in DOCX
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
WVAL = f"{{{NS['w']}}}val"
W14VAL = f"{{{NS['w14']}}}val"


def inspect_docx_fields(file_path: Path) -> list[dict]:
    """Inspect all form fields in a DOCX. Returns a list of field metadata dicts."""
    root = _read_document_xml(file_path)

    fields: list[dict] = []

    # Modern content controls (w:sdt)
    for i, sdt in enumerate(root.findall(".//w:sdt", NS)):
        entry = _inspect_sdt(sdt, i)
        if entry:
            fields.append(entry)

    # Legacy form fields (w:fldChar with w:ffData)
    for fc in root.findall(".//w:fldChar", NS):
        ffdata = fc.find(".//w:ffData", NS)
        if ffdata is not None:
            entry = _inspect_legacy_field(ffdata)
            if entry:
                fields.append(entry)

    # Table cells: label in one cell, value in adjacent empty cell
    fields.extend(_inspect_table_cells(root))

    return fields


def fill_docx_fields(file_path: Path, output_path: Path, data: dict[str, Any]) -> Path:
    """Fill form fields in a DOCX and write to output_path.

    Args:
        file_path: Source DOCX with form fields.
        output_path: Where to write the filled DOCX.
        data: Mapping of field name/index -> value.
            - Text fields: str
            - Checkboxes: bool
            - Date fields: str (e.g. "2026-03-17" or "17.03.2026")
            - Dropdowns: str (the display value)

    Field identification:
        - Named fields: use the field name (alias/tag for sdt, name for legacy)
        - Unnamed fields: use "sdt_<index>" for content controls
    """
    root = _read_document_xml(file_path)

    filled_count = 0

    # Fill modern content controls
    for i, sdt in enumerate(root.findall(".//w:sdt", NS)):
        key = _sdt_key(sdt, i)
        if key in data:
            if _fill_sdt(sdt, data[key]):
                filled_count += 1
            else:
                logger.warning("Could not fill sdt field '%s'", key)

    # Fill legacy form fields
    filled_count += _fill_legacy_fields(root, data)

    # Fill table cells (label -> adjacent empty cell)
    filled_count += _fill_table_cells(root, data)

    # Write back
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, output_path)

    xml_bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    _replace_in_zip(output_path, "word/document.xml", xml_bytes)

    logger.info("Filled %d fields, written: %s", filled_count, output_path)
    return output_path


# ---------------------------------------------------------------------------
# XML I/O
# ---------------------------------------------------------------------------


def _read_document_xml(file_path: Path) -> etree._Element:
    """Read and parse word/document.xml from a DOCX, preserving namespaces."""
    with ZipFile(file_path) as z:
        with z.open("word/document.xml") as f:
            # Use lxml parser which preserves all namespace declarations
            parser = etree.XMLParser(remove_blank_text=False)
            tree = etree.parse(f, parser)
            return tree.getroot()


def _replace_in_zip(zip_path: Path, member: str, data: bytes) -> None:
    """Replace a single file inside a ZIP archive (DOCX)."""
    tmp = zip_path.with_suffix(".tmp")
    with ZipFile(zip_path, "r") as zin, ZipFile(tmp, "w") as zout:
        for item in zin.infolist():
            if item.filename == member:
                zout.writestr(item, data)
            else:
                zout.writestr(item, zin.read(item.filename))
    tmp.replace(zip_path)


# ---------------------------------------------------------------------------
# Content control (w:sdt) inspection & filling
# ---------------------------------------------------------------------------


def _sdt_key(sdt: etree._Element, index: int) -> str:
    """Determine the key for a content control: alias > tag > sdt_<index>."""
    sdt_pr = sdt.find("w:sdtPr", NS)
    if sdt_pr is not None:
        alias = sdt_pr.find("w:alias", NS)
        if alias is not None and alias.get(WVAL):
            return alias.get(WVAL)
        tag = sdt_pr.find("w:tag", NS)
        if tag is not None and tag.get(WVAL):
            return tag.get(WVAL)
    return f"sdt_{index}"


def _inspect_sdt(sdt: etree._Element, index: int) -> dict | None:
    """Extract metadata from a content control."""
    sdt_pr = sdt.find("w:sdtPr", NS)
    if sdt_pr is None:
        return None

    kind = "text"
    if sdt_pr.find("w:date", NS) is not None:
        kind = "date"
    elif sdt_pr.find("w14:checkbox", NS) is not None:
        kind = "checkbox"
    elif sdt_pr.find("w:dropDownList", NS) is not None:
        kind = "dropdown"
    elif sdt_pr.find("w:comboBox", NS) is not None:
        kind = "combobox"

    key = _sdt_key(sdt, index)

    # Current value
    content = sdt.find(".//w:sdtContent", NS)
    current = ""
    if content is not None:
        texts = content.findall(".//w:t", NS)
        current = "".join(t.text or "" for t in texts)

    entry: dict[str, Any] = {
        "name": key,
        "type": kind,
        "format": "sdt",
        "value": current,
    }

    if kind in ("dropdown", "combobox"):
        tag_name = "w:dropDownList" if kind == "dropdown" else "w:comboBox"
        dd = sdt_pr.find(tag_name, NS)
        if dd is not None:
            entry["options"] = [
                item.get(f"{{{NS['w']}}}displayText", item.get(WVAL, "")) for item in dd.findall("w:listItem", NS)
            ]

    if kind == "checkbox":
        checked_el = sdt_pr.find(".//w14:checked", NS)
        if checked_el is not None:
            entry["value"] = checked_el.get(W14VAL) == "1"
        else:
            entry["value"] = current in ("\u2611", "\u2612")

    return entry


def _fill_sdt(sdt: etree._Element, value: Any) -> bool:
    """Fill a single content control with a value."""
    sdt_pr = sdt.find("w:sdtPr", NS)
    if sdt_pr is None:
        return False

    if sdt_pr.find("w14:checkbox", NS) is not None:
        return _fill_sdt_checkbox(sdt, sdt_pr, bool(value))
    elif sdt_pr.find("w:date", NS) is not None:
        return _fill_sdt_date(sdt, sdt_pr, str(value))
    elif sdt_pr.find("w:dropDownList", NS) is not None or sdt_pr.find("w:comboBox", NS) is not None:
        return _fill_sdt_text(sdt, str(value))
    else:
        return _fill_sdt_text(sdt, str(value))


def _fill_sdt_text(sdt: etree._Element, text: str) -> bool:
    """Set the text content of a content control."""
    content = sdt.find("w:sdtContent", NS)
    if content is None:
        return False

    t_elem = content.find(".//w:t", NS)
    if t_elem is not None:
        t_elem.text = text
        all_t = content.findall(".//w:t", NS)
        for extra_t in all_t[1:]:
            extra_t.text = ""
        return True

    # No text element found - create one in a new run
    p = content.find("w:p", NS)
    if p is None:
        p = etree.SubElement(content, f"{{{NS['w']}}}p")
    r = etree.SubElement(p, f"{{{NS['w']}}}r")
    t = etree.SubElement(r, f"{{{NS['w']}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return True


def _fill_sdt_date(sdt: etree._Element, sdt_pr: etree._Element, value: str) -> bool:
    """Fill a date content control, setting both fullDate attribute and display text."""
    date_el = sdt_pr.find("w:date", NS)
    if date_el is None:
        return False

    dt = None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        logger.warning("Could not parse date '%s', writing as plain text", value)
        return _fill_sdt_text(sdt, value)

    # Set w:fullDate attribute on the w:date element
    date_el.set(f"{{{NS['w']}}}fullDate", dt.strftime("%Y-%m-%dT00:00:00Z"))

    # Format display text according to w:dateFormat
    date_fmt_el = date_el.find("w:dateFormat", NS)
    if date_fmt_el is not None:
        word_fmt = date_fmt_el.get(WVAL, "dd.MM.yyyy")
        display = _format_date_word(dt, word_fmt)
    else:
        display = dt.strftime("%d.%m.%Y")

    return _fill_sdt_text(sdt, display)


def _format_date_word(dt: datetime, word_fmt: str) -> str:
    """Convert a datetime to a string using Word's date format patterns."""
    result = word_fmt
    # Order matters: longest tokens first to avoid partial replacement
    result = result.replace("dddd", dt.strftime("%A"))
    result = result.replace("ddd", dt.strftime("%a"))
    result = result.replace("dd", f"{dt.day:02d}")
    # Avoid replacing the 'd' in 'dd' already processed - skip single d
    result = result.replace("MMMM", dt.strftime("%B"))
    result = result.replace("MMM", dt.strftime("%b"))
    result = result.replace("MM", f"{dt.month:02d}")
    result = result.replace("yyyy", str(dt.year))
    result = result.replace("yy", f"{dt.year % 100:02d}")
    return result


def _fill_sdt_checkbox(sdt: etree._Element, sdt_pr: etree._Element, checked: bool) -> bool:
    """Toggle a content control checkbox."""
    cb = sdt_pr.find("w14:checkbox", NS)
    if cb is None:
        return False

    checked_el = cb.find("w14:checked", NS)
    if checked_el is None:
        checked_el = etree.SubElement(cb, f"{{{NS['w14']}}}checked")
    checked_el.set(W14VAL, "1" if checked else "0")

    # Determine display characters from checkedState/uncheckedState
    checked_sym = cb.find("w14:checkedState", NS)
    unchecked_sym = cb.find("w14:uncheckedState", NS)

    checked_char = "\u2611"  # ☑
    unchecked_char = "\u2610"  # ☐
    if checked_sym is not None:
        code = checked_sym.get(W14VAL)
        if code:
            try:
                checked_char = chr(int(code, 16))
            except (ValueError, OverflowError):
                pass
    if unchecked_sym is not None:
        code = unchecked_sym.get(W14VAL)
        if code:
            try:
                unchecked_char = chr(int(code, 16))
            except (ValueError, OverflowError):
                pass

    display_char = checked_char if checked else unchecked_char
    content = sdt.find("w:sdtContent", NS)
    if content is not None:
        t_elem = content.find(".//w:t", NS)
        if t_elem is not None:
            t_elem.text = display_char

    return True


# ---------------------------------------------------------------------------
# Legacy form fields (w:fldChar + w:ffData)
# ---------------------------------------------------------------------------


def _inspect_legacy_field(ffdata: etree._Element) -> dict | None:
    """Extract metadata from a legacy form field."""
    name_el = ffdata.find("w:name", NS)
    name = name_el.get(WVAL, "(unnamed)") if name_el is not None else "(unnamed)"

    cb = ffdata.find("w:checkBox", NS)
    text_input = ffdata.find("w:textInput", NS)
    dd = ffdata.find("w:ddList", NS)

    if cb is not None:
        checked_el = cb.find("w:checked", NS)
        if checked_el is None:
            checked_el = cb.find("w:default", NS)
        checked = checked_el is not None and checked_el.get(WVAL) == "1"
        return {"name": name, "type": "checkbox", "format": "legacy", "value": checked}
    elif text_input is not None:
        default_el = text_input.find("w:default", NS)
        default_val = default_el.get(WVAL, "") if default_el is not None else ""
        max_len_el = text_input.find("w:maxLength", NS)
        entry: dict[str, Any] = {"name": name, "type": "text", "format": "legacy", "value": default_val}
        if max_len_el is not None:
            entry["max_length"] = int(max_len_el.get(WVAL, "0"))
        return entry
    elif dd is not None:
        items = dd.findall("w:listEntry", NS)
        options = [item.get(WVAL, "") for item in items]
        result_el = dd.find("w:result", NS)
        selected_idx = int(result_el.get(WVAL, "0")) if result_el is not None else 0
        return {
            "name": name,
            "type": "dropdown",
            "format": "legacy",
            "value": options[selected_idx] if options and selected_idx < len(options) else "",
            "options": options,
        }

    return None


def _fill_legacy_fields(root: etree._Element, data: dict[str, Any]) -> int:
    """Fill legacy form fields in the document tree. Returns count of filled fields."""
    filled = 0

    for fc in root.findall(".//w:fldChar", NS):
        ffdata = fc.find("w:ffData", NS)
        if ffdata is None:
            continue
        name_el = ffdata.find("w:name", NS)
        name = name_el.get(WVAL, "") if name_el is not None else ""
        if name not in data:
            continue

        value = data[name]
        cb = ffdata.find("w:checkBox", NS)
        text_input = ffdata.find("w:textInput", NS)
        dd = ffdata.find("w:ddList", NS)

        if cb is not None:
            _fill_legacy_checkbox(ffdata, bool(value))
            filled += 1
        elif text_input is not None:
            _fill_legacy_text(fc, str(value))
            filled += 1
        elif dd is not None:
            _fill_legacy_dropdown(ffdata, str(value))
            filled += 1

    return filled


def _fill_legacy_checkbox(ffdata: etree._Element, checked: bool) -> None:
    """Toggle a legacy checkbox."""
    cb = ffdata.find("w:checkBox", NS)
    if cb is None:
        return

    checked_el = cb.find("w:checked", NS)
    if checked_el is None:
        checked_el = etree.SubElement(cb, f"{{{NS['w']}}}checked")
    checked_el.set(WVAL, "1" if checked else "0")

    default_el = cb.find("w:default", NS)
    if default_el is not None:
        default_el.set(WVAL, "1" if checked else "0")


def _fill_legacy_text(fld_char: etree._Element, text: str) -> None:
    """Fill a legacy text field.

    Legacy text fields store their value in w:t elements between
    the 'separate' and 'end' fldChar elements within the same paragraph.
    """
    # Walk up to the containing run, then to the paragraph
    begin_run = fld_char.getparent()
    if begin_run is None:
        return
    paragraph = begin_run.getparent()
    if paragraph is None:
        return

    runs = list(paragraph)
    in_field = False
    found_separate = False
    text_set = False

    for run in runs:
        fc = run.find("w:fldChar", NS)
        if fc is not None:
            fc_type = fc.get(f"{{{NS['w']}}}fldCharType", "")
            if fc is fld_char:
                in_field = True
            elif fc_type == "separate" and in_field:
                found_separate = True
            elif fc_type == "end" and in_field:
                break

        if found_separate and fc is None:
            t_elem = run.find("w:t", NS)
            if t_elem is not None:
                if not text_set:
                    t_elem.text = text
                    text_set = True
                else:
                    t_elem.text = ""


def _fill_legacy_dropdown(ffdata: etree._Element, value: str) -> None:
    """Select an option in a legacy dropdown."""
    dd = ffdata.find("w:ddList", NS)
    if dd is None:
        return

    items = dd.findall("w:listEntry", NS)
    for i, item in enumerate(items):
        if item.get(WVAL, "") == value:
            result_el = dd.find("w:result", NS)
            if result_el is None:
                result_el = etree.SubElement(dd, f"{{{NS['w']}}}result")
            result_el.set(WVAL, str(i))
            return

    logger.warning("Dropdown value '%s' not found in options", value)


# ---------------------------------------------------------------------------
# Table cell inspection & filling (label -> adjacent empty cell)
# ---------------------------------------------------------------------------


def _cell_text(cell: etree._Element) -> str:
    """Get the plain text of a table cell, ignoring content controls."""
    texts = []
    for t in cell.findall(".//w:t", NS):
        # Skip text inside content controls (sdt) - those are handled separately
        parent = t.getparent()
        while parent is not None and parent is not cell:
            if etree.QName(parent.tag).localname == "sdt":
                break
            parent = parent.getparent()
        else:
            if t.text:
                texts.append(t.text)
    return "".join(texts).strip()


def _cell_is_empty(cell: etree._Element) -> bool:
    """Check if a cell has no user-visible text (ignoring placeholders in sdts)."""
    return _cell_text(cell) == ""


def _cell_has_sdt(cell: etree._Element) -> bool:
    """Check if a cell contains a content control (already handled separately)."""
    return cell.find(".//w:sdt", NS) is not None


def _normalize_label(text: str) -> str:
    """Normalize a label for use as a field key: strip colons, parens, hints."""
    import re

    # Take only the first line / first sentence
    text = text.split("\n")[0].strip()
    # Remove all parenthetical hints
    text = re.sub(r"\s*\([^)]*\)", "", text)
    text = text.rstrip(":").strip()
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text


def _make_unique(name: str, seen: dict[str, int]) -> str:
    """Append a suffix if the name was already used."""
    if name not in seen:
        seen[name] = 0
        return name
    seen[name] += 1
    return f"{name} #{seen[name] + 1}"


def _inspect_table_cells(root: etree._Element) -> list[dict]:
    """Find fillable table cells: empty cells adjacent to label cells.

    Two passes per table:
    1. Label-adjacent: a label cell followed by an empty cell (higher priority)
    2. Header-grid: empty cells under a column header (only if not already claimed)
    """
    fields = []
    seen: dict[str, int] = {}

    for ti, tbl in enumerate(root.findall(".//w:tbl", NS)):  # noqa: B007
        rows = tbl.findall("w:tr", NS)
        if not rows:
            continue

        header_cells = rows[0].findall("w:tc", NS)
        headers = [_cell_text(c) for c in header_cells]

        # Pass 1: label-adjacent (takes priority)
        claimed: set[int] = set()  # id() of claimed cells
        for ri, row in enumerate(rows):  # noqa: B007
            cells = row.findall("w:tc", NS)
            for ci, cell in enumerate(cells):
                if _cell_has_sdt(cell):
                    continue
                cell_txt = _cell_text(cell)
                if cell_txt and ci + 1 < len(cells):
                    next_cell = cells[ci + 1]
                    if _cell_is_empty(next_cell) and not _cell_has_sdt(next_cell):
                        label = _make_unique(_normalize_label(cell_txt), seen)
                        fields.append(
                            {
                                "name": label,
                                "type": "cell",
                                "format": "table",
                                "value": "",
                                "_tbl": ti,
                                "_row": ri,
                                "_col": ci + 1,
                            }
                        )
                        claimed.add(id(next_cell))

        # Pass 2: header-grid (only unclaimed empty cells)
        for ri, row in enumerate(rows):  # noqa: B007
            if ri == 0:
                continue
            cells = row.findall("w:tc", NS)
            for ci, cell in enumerate(cells):
                if id(cell) in claimed or _cell_has_sdt(cell):
                    continue
                if _cell_is_empty(cell) and ci < len(headers) and headers[ci]:
                    label = f"{_normalize_label(headers[ci])}_{ri}"
                    label = _make_unique(label, seen)
                    fields.append(
                        {
                            "name": label,
                            "type": "cell",
                            "format": "table",
                            "value": "",
                            "_tbl": ti,
                            "_row": ri,
                            "_col": ci,
                        }
                    )
                    claimed.add(id(cell))

    # Pass 3: standalone empty tables named by preceding paragraph
    _inspect_standalone_empty_tables(root, fields, seen)

    return fields


def _inspect_standalone_empty_tables(root: etree._Element, fields: list[dict], seen: dict[str, int]) -> None:
    """Find single-cell empty tables and name them by the preceding paragraph."""
    body = root.find(".//w:body", NS)
    if body is None:
        return

    prev_texts: list[str] = []
    for child in body:
        if not isinstance(child.tag, str):
            continue  # skip comment/PI nodes
        tag = etree.QName(child.tag).localname
        if tag == "p":
            texts = child.findall(".//w:t", NS)
            txt = "".join(t.text or "" for t in texts).strip()
            if txt:
                prev_texts.append(txt)
        elif tag == "tbl":
            rows = child.findall("w:tr", NS)
            total_cells = sum(len(r.findall("w:tc", NS)) for r in rows)
            if total_cells > 2 or not prev_texts:
                prev_texts = []
                continue
            all_empty = True
            for row in rows:
                for cell in row.findall("w:tc", NS):
                    if _cell_text(cell) or _cell_has_sdt(cell):
                        all_empty = False
                        break
            if all_empty and rows:
                label_raw = ""
                for pt in reversed(prev_texts):
                    normalized = _normalize_label(pt)
                    if normalized:
                        label_raw = normalized
                        break
                if label_raw:
                    label = _make_unique(label_raw, seen)
                    fields.append(
                        {
                            "name": label,
                            "type": "cell",
                            "format": "table",
                            "value": "",
                            "_tbl": -1,
                            "_row": 0,
                            "_col": 0,
                        }
                    )
            prev_texts = []


def _fill_cell_text(cell: etree._Element, text: str) -> bool:
    """Write text into a table cell."""
    # Find existing paragraph and run
    p = cell.find("w:p", NS)
    if p is None:
        p = etree.SubElement(cell, f"{{{NS['w']}}}p")

    # Try to find an existing run to preserve formatting
    r = p.find("w:r", NS)
    if r is not None:
        t = r.find("w:t", NS)
        if t is not None:
            t.text = text
            return True

    # Create new run
    r = etree.SubElement(p, f"{{{NS['w']}}}r")
    t = etree.SubElement(r, f"{{{NS['w']}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return True


def _fill_table_cells(root: etree._Element, data: dict[str, Any]) -> int:
    """Fill table cells identified by their label keys.

    Uses the same two-pass + _make_unique logic as inspection.
    """
    cell_map: dict[str, etree._Element] = {}
    seen: dict[str, int] = {}

    for ti, tbl in enumerate(root.findall(".//w:tbl", NS)):  # noqa: B007
        rows = tbl.findall("w:tr", NS)
        if not rows:
            continue

        header_cells = rows[0].findall("w:tc", NS)
        headers = [_cell_text(c) for c in header_cells]

        claimed: set[int] = set()
        # Pass 1: label-adjacent
        for ri, row in enumerate(rows):  # noqa: B007
            cells = row.findall("w:tc", NS)
            for ci, cell in enumerate(cells):
                if _cell_has_sdt(cell):
                    continue
                cell_txt = _cell_text(cell)
                if cell_txt and ci + 1 < len(cells):
                    next_cell = cells[ci + 1]
                    if _cell_is_empty(next_cell) and not _cell_has_sdt(next_cell):
                        label = _make_unique(_normalize_label(cell_txt), seen)
                        cell_map[label] = next_cell
                        claimed.add(id(next_cell))

        # Pass 2: header-grid
        for ri, row in enumerate(rows):  # noqa: B007
            if ri == 0:
                continue
            cells = row.findall("w:tc", NS)
            for ci, cell in enumerate(cells):
                if id(cell) in claimed or _cell_has_sdt(cell):
                    continue
                if _cell_is_empty(cell) and ci < len(headers) and headers[ci]:
                    label = f"{_normalize_label(headers[ci])}_{ri}"
                    label = _make_unique(label, seen)
                    cell_map[label] = cell
                    claimed.add(id(cell))

    # Pass 3: standalone empty tables
    _fill_standalone_empty_tables(root, cell_map, seen)

    filled = 0
    for key, value in data.items():
        if key in cell_map:
            if _fill_cell_text(cell_map[key], str(value)):
                filled += 1

    return filled


def _fill_standalone_empty_tables(
    root: etree._Element, cell_map: dict[str, etree._Element], seen: dict[str, int]
) -> None:
    """Map standalone empty table cells by preceding paragraph label."""
    body = root.find(".//w:body", NS)
    if body is None:
        return

    prev_texts: list[str] = []
    for child in body:
        if not isinstance(child.tag, str):
            continue  # skip comment/PI nodes
        tag = etree.QName(child.tag).localname
        if tag == "p":
            texts = child.findall(".//w:t", NS)
            txt = "".join(t.text or "" for t in texts).strip()
            if txt:
                prev_texts.append(txt)
        elif tag == "tbl":
            rows = child.findall("w:tr", NS)
            total_cells = sum(len(r.findall("w:tc", NS)) for r in rows)
            if total_cells > 2 or not prev_texts:
                prev_texts = []
                continue
            all_empty = True
            for row in rows:
                for cell in row.findall("w:tc", NS):
                    if _cell_text(cell) or _cell_has_sdt(cell):
                        all_empty = False
                        break
            if all_empty and rows:
                label_raw = ""
                for pt in reversed(prev_texts):
                    normalized = _normalize_label(pt)
                    if normalized:
                        label_raw = normalized
                        break
                if label_raw:
                    first_cell = rows[0].findall("w:tc", NS)[0]
                    label = _make_unique(label_raw, seen)
                    cell_map[label] = first_cell
            prev_texts = []
