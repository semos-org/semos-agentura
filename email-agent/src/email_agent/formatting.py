"""Markdown and HTML formatting conversion for email output."""

from __future__ import annotations

import re

import markdown as md_lib


def md_to_plain(text: str) -> str:
    """Convert Markdown to clean plain text for email."""
    s = text
    # Headers: ## Header -> HEADER
    s = re.sub(
        r"^#{1,6}\s+(.+)$",
        lambda m: m.group(1).upper(),
        s, flags=re.MULTILINE,
    )
    # Bold: **text** or __text__ -> UPPERCASE
    s = re.sub(r"\*\*(.+?)\*\*", lambda m: m.group(1).upper(), s)
    s = re.sub(r"__(.+?)__", lambda m: m.group(1).upper(), s)
    # Italic: *text* or _text_ -> text
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", s)
    s = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", s)
    # Inline code: `code` -> code
    s = re.sub(r"`(.+?)`", r"\1", s)
    # Code blocks: ```...``` -> indented
    s = re.sub(
        r"```\w*\n(.*?)```",
        lambda m: "\n".join("    " + line for line in m.group(1).splitlines()),
        s, flags=re.DOTALL,
    )
    # Unicode symbols that LLMs love
    _REPLACEMENTS = {
        "\u2713": "x", "\u2714": "x",
        "\u2717": "-", "\u2718": "-",
        "\u2022": "-", "\u2023": "-",
        "\u2192": "->", "\u2190": "<-",
        "\u2014": "--", "\u2013": "-",
        "\u2026": "...", "\u2605": "*",
    }
    for old, new in _REPLACEMENTS.items():
        s = s.replace(old, new)
    return s


def md_to_html(text: str, style: str = "") -> str:
    """Convert Markdown to HTML fragment for email.

    If *style* is provided, it is applied to all block-level
    elements so the response matches the surrounding email.
    """
    # Ensure list blocks have a blank line before them
    s = re.sub(r"(\S)\n([ \t]*[-*+] )", r"\1\n\n\2", text)
    s = re.sub(r"(\S)\n([ \t]*\d+\. )", r"\1\n\n\2", s)
    html = md_lib.markdown(s, extensions=["tables", "nl2br"])
    # Convert headings to bold paragraphs (no large fonts)
    for level in range(1, 7):
        html = re.sub(
            rf"<h{level}>(.*?)</h{level}>",
            r"<p><b>\1</b></p>",
            html, flags=re.DOTALL,
        )
    if style:
        for tag in ("p", "li", "td", "th"):
            html = html.replace(f"<{tag}>", f'<{tag} style="{style}">')
    return html


def html_to_annotated_text(html: str) -> str:
    """Convert HTML formatting cues to text annotations.

    Translates semantic HTML (strikethrough, highlight, etc.)
    into plain-text markers the LLM can understand.
    """
    s = html
    # Strikethrough: <strike>, <s>, <del>
    s = re.sub(
        r"<(?:strike|s|del)>(.*?)</(?:strike|s|del)>",
        r"~~\1~~",
        s, flags=re.DOTALL | re.IGNORECASE,
    )
    # Strikethrough via style
    s = re.sub(
        r'<span[^>]*style="[^"]*text-decoration:\s*line-through[^"]*"[^>]*>(.*?)</span>',
        r"~~\1~~",
        s, flags=re.DOTALL | re.IGNORECASE,
    )
    # Highlight via background-color
    s = re.sub(
        r'<(?:span|mark)[^>]*style="[^"]*background(?:-color)?:\s*'
        r"(?:yellow|#ff[fe]\w*|rgb\(255,\s*255,\s*\d+\))"
        r'[^"]*"[^>]*>(.*?)</(?:span|mark)>',
        r"[HIGHLIGHT: \1]",
        s, flags=re.DOTALL | re.IGNORECASE,
    )
    # <mark> without style
    s = re.sub(
        r"<mark>(.*?)</mark>",
        r"[HIGHLIGHT: \1]",
        s, flags=re.DOTALL | re.IGNORECASE,
    )
    return s


def extract_prompt_style(html: str, tag_text: str) -> str:
    """Extract inline CSS from the element containing the tag.

    Looks backwards from the tag for the nearest <span>, <p>,
    <div>, or <font> with a style= attribute.
    """
    tag_pos = html.find(tag_text)
    if tag_pos < 0:
        return ""

    chunk = html[max(0, tag_pos - 500):tag_pos]
    matches = list(re.finditer(
        r'<(?:span|p|div|font)[^>]*?style="([^"]*)"',
        chunk, re.IGNORECASE,
    ))
    if matches:
        return matches[-1].group(1)
    return ""
