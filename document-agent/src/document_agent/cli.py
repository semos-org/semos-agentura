"""CLI for document-agent: digest and compose subcommands."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Settings
from .models import OutputFormat, OutputMode


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="document-agent",
        description="Document digestion (OCR to Markdown) and composition (Markdown to documents).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command")

    # --- digest subcommand ---
    dig = subparsers.add_parser("digest", help="OCR document(s) to Markdown")
    dig.add_argument("files", nargs="*", help="PDF, image, or Office files to process")
    dig.add_argument("--dir", help="Process all supported files in a directory")
    dig.add_argument("--output-dir", help="Output directory (default: same as input)")
    dig.add_argument("--inline", action="store_true", help="Return markdown with base64 images (print to stdout)")
    dig.add_argument("--schema", help="Path to a Pydantic schema .py file for structured extraction")
    dig.add_argument("--prompt", help="Annotation prompt (requires --schema)")
    dig.add_argument("--max-pages", type=int, help="Max pages per PDF chunk")
    dig.add_argument(
        "--table-format", choices=["markdown", "html"],
        help="Table output format (default: from settings, usually markdown)",
    )

    # --- compose subcommand ---
    comp = subparsers.add_parser("compose", help="Markdown to document")
    comp.add_argument("input", help="Markdown file path")
    comp.add_argument("output", help="Output file path")
    comp.add_argument(
        "--format", required=True,
        choices=["pdf", "docx", "odt", "pptx", "html"],
        help="Output format",
    )
    comp.add_argument("--slides", action="store_true", help="Use Marp for slide generation")
    comp.add_argument("--no-mermaid", action="store_true", help="Skip mermaid diagram rendering")
    comp.add_argument("--no-drawio", action="store_true", help="Skip drawio diagram rendering")

    # --- inspect subcommand ---
    insp = subparsers.add_parser("inspect", help="Inspect form fields in a PDF or DOCX")
    insp.add_argument("file", help="PDF or DOCX file with form fields")
    insp.add_argument("--json", action="store_true", help="Output as JSON")

    # --- fill subcommand ---
    fill = subparsers.add_parser("fill", help="Fill form fields in a PDF or DOCX")
    fill.add_argument("file", help="Source PDF or DOCX with form fields")
    fill.add_argument("output", help="Output file path")
    fill.add_argument("--data", required=True, help="JSON file or inline JSON string with field values")
    fill.add_argument("--template", help="Template JSON mapping semantic field names to internal names")

    # --- diagram subcommand ---
    diag = subparsers.add_parser(
        "diagram",
        help="Generate a diagram from description with LLM optimization",
    )
    diag.add_argument(
        "description", nargs="?", default=None,
        help="Natural-language description or modification instructions",
    )
    diag.add_argument(
        "--source", "-s",
        help="Existing diagram: file path (.mmd, .drawio, .drawio.png, .svg, image) or inline code",
    )
    diag.add_argument(
        "--type", default=None,
        choices=["mermaid", "drawio"],
        help="Diagram type (auto-detected from source if omitted)",
    )
    diag.add_argument(
        "--output", "-o", required=True,
        help="Output image path (PNG)",
    )
    diag.add_argument(
        "--max-iterations", type=int, default=3,
        help="Max optimization iterations (default: 3)",
    )
    diag.add_argument(
        "--code-output",
        help="Save final diagram source code to this path",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)-30s  %(levelname)-8s  %(message)s",
    )

    settings = Settings()

    if args.command == "digest":
        _run_digest(args, settings)
    elif args.command == "compose":
        _run_compose(args, settings)
    elif args.command == "inspect":
        _run_inspect(args)
    elif args.command == "fill":
        _run_fill(args)
    elif args.command == "diagram":
        _run_diagram(args, settings)


def _run_digest(args: argparse.Namespace, settings: Settings) -> None:
    from ._constants import SUPPORTED_EXTENSIONS
    from .digestion import digest

    files: list[Path] = []
    if args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            print(f"Error: Not a directory: {dir_path}", file=sys.stderr)
            sys.exit(1)
        for p in sorted(dir_path.iterdir()):
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(p)
        if not files:
            print(f"No supported files found in {dir_path}", file=sys.stderr)
            sys.exit(1)

    for f in (args.files or []):
        p = Path(f)
        if not p.exists():
            print(f"Error: File not found: {p}", file=sys.stderr)
            sys.exit(1)
        files.append(p)

    if not files:
        print("Error: No input files specified. Use positional args or --dir.", file=sys.stderr)
        sys.exit(1)

    if args.prompt and not args.schema:
        print("Error: --prompt requires --schema", file=sys.stderr)
        sys.exit(1)

    output_mode = OutputMode.INLINE if args.inline else OutputMode.FILE
    output_dir = Path(args.output_dir) if args.output_dir else None

    if args.table_format:
        settings.table_format = args.table_format

    for file_path in files:
        result = digest(
            file_path,
            output_dir=output_dir,
            output_mode=output_mode,
            schema=args.schema,
            annotation_prompt=args.prompt,
            max_pages=args.max_pages,
            settings=settings,
        )
        if output_mode == OutputMode.INLINE:
            print(result.markdown)
        else:
            print(f"Written: {result.output_path}")
            if result.images_dir:
                print(f"Images: {result.images_dir}")
            if result.annotation_path:
                print(f"Annotation: {result.annotation_path}")

    print(f"\nDone. Processed {len(files)} file(s).")


def _run_compose(args: argparse.Namespace, settings: Settings) -> None:
    from .composition import compose

    fmt = OutputFormat(args.format)
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    result = compose(
        input_path,
        output_path,
        fmt,
        is_slides=args.slides,
        render_mermaid=not args.no_mermaid,
        render_drawio=not args.no_drawio,
        settings=settings,
    )
    print(f"Written: {result.output_path}")


def _run_inspect(args: argparse.Namespace) -> None:
    import json as json_mod

    from .forms import inspect_form

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    fields = inspect_form(file_path)
    if args.json:
        print(json_mod.dumps(fields, indent=2, ensure_ascii=False, default=str))
    else:
        if not fields:
            print("No form fields found.")
            return
        print(f"Form fields ({len(fields)}):\n")
        for f in fields:
            opts = f" options={f['options']}" if "options" in f else ""
            fmt = f" [{f['format']}]" if "format" in f else ""
            print(f"  {f['type']:10s} {f['name']:40s} = {f.get('value', '')!r}{opts}{fmt}")


def _run_fill(args: argparse.Namespace) -> None:
    from .forms import fill_form, fill_form_with_template

    file_path = Path(args.file)
    output_path = Path(args.output)
    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    if args.template:
        result = fill_form_with_template(file_path, output_path, args.data, args.template)
    else:
        result = fill_form(file_path, output_path, args.data)
    print(f"Written: {result}")


def _run_diagram(
    args: argparse.Namespace, settings: Settings,
) -> None:
    import asyncio
    import shutil

    from .composition import generate_diagram

    if not args.description and not args.source:
        print(
            "Error: provide a description, --source, or both.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_path = Path(args.output)
    output_dir = output_path.parent / f".diagram_work_{output_path.stem}"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = asyncio.run(generate_diagram(
            args.description,
            args.type,
            source=args.source,
            output_dir=output_dir,
            max_iterations=args.max_iterations,
            settings=settings,
        ))

        # Copy final image to requested output
        shutil.copy2(result.image_path, output_path)
        print(f"Written: {output_path}")
        print(
            f"Iterations: {result.iterations}, "
            f"passed: {any(r.get('pass') for r in result.review_log)}"
        )

        if args.code_output:
            code_path = Path(args.code_output)
            code_path.write_text(result.code, encoding="utf-8")
            print(f"Code: {code_path}")

    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
