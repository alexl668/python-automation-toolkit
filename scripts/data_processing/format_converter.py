#!/usr/bin/env python3
"""Format Converter — Convert between CSV, JSON, XML, YAML, Excel, and Markdown.

Auto-detects input format, supports batch conversion of entire directories,
and handles nested/flat data structures appropriately for each target format.

Usage examples:
    python format_converter.py convert data.csv --to json
    python format_converter.py convert data.json --to yaml --output result.yaml
    python format_converter.py convert data.xlsx --to csv --sheet "Sheet1"
    python format_converter.py batch ./input_dir --from csv --to json --output-dir ./output_dir
    python format_converter.py detect mystery_file.dat
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

import click
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()

SUPPORTED_FORMATS = ("csv", "json", "xml", "yaml", "yml", "xlsx", "xls", "md", "markdown")


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def _detect_format(path: Path) -> str:
    """Detect file format from extension or content sniffing."""
    ext = path.suffix.lower().lstrip(".")
    if ext in ("csv", "tsv"):
        return "csv"
    if ext in ("json", "jsonl", "ndjson"):
        return "json"
    if ext in ("xml",):
        return "xml"
    if ext in ("yaml", "yml"):
        return "yaml"
    if ext in ("xlsx", "xls"):
        return "excel"
    if ext in ("md", "markdown"):
        return "markdown"

    # Content sniffing
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(2048).strip()
        if head.startswith(("{", "[")):
            return "json"
        if head.startswith("<?xml") or head.startswith("<"):
            return "xml"
        # Check if it looks like CSV (has commas and multiple lines)
        lines = head.split("\n")
        if len(lines) > 1 and ("," in lines[0] or "\t" in lines[0]):
            return "csv"
        if ":" in head and not head.startswith("{"):
            return "yaml"
    except Exception:
        pass

    console.print(f"[yellow]Warning:[/yellow] Could not detect format for {path}. Assuming CSV.")
    return "csv"


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _read_csv(path: Path, **kwargs: Any) -> pd.DataFrame:
    """Read CSV/TSV into DataFrame."""
    delimiter = "\t" if path.suffix.lower() == ".tsv" else kwargs.get("delimiter", ",")
    return pd.read_csv(path, delimiter=delimiter, encoding=kwargs.get("encoding", "utf-8"))


def _read_json(path: Path, **kwargs: Any) -> pd.DataFrame:
    """Read JSON into DataFrame. Handles arrays of objects and nested structures."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return pd.json_normalize(data)
    elif isinstance(data, dict):
        # Try to find an array within
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return pd.json_normalize(v)
        # Single object → single-row DataFrame
        return pd.json_normalize([data])
    else:
        console.print("[red]Error:[/red] JSON must contain objects or array of objects.")
        raise SystemExit(1)


def _read_xml(path: Path, **kwargs: Any) -> pd.DataFrame:
    """Read XML into DataFrame. Each top-level child element becomes a row."""
    tree = ET.parse(path)
    root = tree.getroot()

    records: list[dict[str, str]] = []
    for child in root:
        record: dict[str, str] = {}
        # Attributes
        record.update(child.attrib)
        # Child elements
        for elem in child:
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            record[tag] = elem.text or ""
        if record:
            records.append(record)

    if not records:
        console.print("[yellow]Warning:[/yellow] No records found in XML.")
    return pd.DataFrame(records)


def _read_yaml(path: Path, **kwargs: Any) -> pd.DataFrame:
    """Read YAML into DataFrame."""
    try:
        import yaml
    except ImportError:
        console.print("[red]Error:[/red] PyYAML not installed. Run: pip install pyyaml")
        raise SystemExit(1)

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if isinstance(data, list):
        return pd.json_normalize(data)
    elif isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return pd.json_normalize(v)
        return pd.json_normalize([data])
    else:
        console.print("[red]Error:[/red] YAML must contain mapping or sequence of mappings.")
        raise SystemExit(1)


def _read_excel(path: Path, **kwargs: Any) -> pd.DataFrame:
    """Read Excel into DataFrame."""
    sheet = kwargs.get("sheet", 0)
    return pd.read_excel(path, sheet_name=sheet)


def _read_markdown(path: Path, **kwargs: Any) -> pd.DataFrame:
    """Parse a Markdown table into DataFrame."""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    # Find table lines (contain |)
    table_lines = [l.strip() for l in lines if "|" in l]
    if len(table_lines) < 2:
        console.print("[red]Error:[/red] No Markdown table found.")
        raise SystemExit(1)

    # Parse header
    header = [c.strip() for c in table_lines[0].strip("|").split("|")]
    # Skip separator line (---|---)
    data_lines = table_lines[2:] if len(table_lines) > 2 else []

    rows: list[list[str]] = []
    for line in data_lines:
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)

    return pd.DataFrame(rows, columns=header)


READERS = {
    "csv": _read_csv,
    "json": _read_json,
    "xml": _read_xml,
    "yaml": _read_yaml,
    "yml": _read_yaml,
    "excel": _read_excel,
    "xlsx": _read_excel,
    "xls": _read_excel,
    "markdown": _read_markdown,
    "md": _read_markdown,
}


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_csv(df: pd.DataFrame, path: Path | None, **kwargs: Any) -> None:
    """Write DataFrame as CSV."""
    if path:
        df.to_csv(path, index=False, encoding="utf-8")
        console.print(f"[green]✓[/green] Written CSV → [cyan]{path}[/cyan]")
    else:
        console.print(df.to_csv(index=False))


def _write_json(df: pd.DataFrame, path: Path | None, **kwargs: Any) -> None:
    """Write DataFrame as JSON array of objects."""
    indent = kwargs.get("indent", 2)
    records = df.to_dict(orient="records")
    text = json.dumps(records, indent=indent, ensure_ascii=False, default=str)
    if path:
        path.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/green] Written JSON → [cyan]{path}[/cyan]")
    else:
        console.print_json(text)


def _write_xml(df: pd.DataFrame, path: Path | None, **kwargs: Any) -> None:
    """Write DataFrame as XML. Root element <records>, each row is <record>."""
    root = ET.Element("records")
    for _, row in df.iterrows():
        record = ET.SubElement(root, "record")
        for col in df.columns:
            elem = ET.SubElement(record, str(col))
            val = row[col]
            elem.text = "" if pd.isna(val) else str(val)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    if path:
        tree.write(path, encoding="unicode", xml_declaration=True)
        console.print(f"[green]✓[/green] Written XML → [cyan]{path}[/cyan]")
    else:
        buf = io.StringIO()
        tree.write(buf, encoding="unicode", xml_declaration=True)
        console.print(buf.getvalue())


def _write_yaml(df: pd.DataFrame, path: Path | None, **kwargs: Any) -> None:
    """Write DataFrame as YAML."""
    try:
        import yaml
    except ImportError:
        console.print("[red]Error:[/red] PyYAML not installed. Run: pip install pyyaml")
        raise SystemExit(1)

    records = df.to_dict(orient="records")
    text = yaml.dump(records, default_flow_style=False, allow_unicode=True, sort_keys=False)
    if path:
        path.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/green] Written YAML → [cyan]{path}[/cyan]")
    else:
        console.print(text)


def _write_excel(df: pd.DataFrame, path: Path | None, **kwargs: Any) -> None:
    """Write DataFrame as Excel (.xlsx)."""
    if not path:
        console.print("[red]Error:[/red] Excel output requires --output path.")
        raise SystemExit(1)
    df.to_excel(path, index=False, engine="openpyxl")
    console.print(f"[green]✓[/green] Written Excel → [cyan]{path}[/cyan]")


def _write_markdown(df: pd.DataFrame, path: Path | None, **kwargs: Any) -> None:
    """Write DataFrame as a Markdown table."""
    cols = df.columns.tolist()
    lines = [
        "| " + " | ".join(str(c) for c in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for _, row in df.iterrows():
        cells = ["" if pd.isna(v) else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")

    text = "\n".join(lines) + "\n"
    if path:
        path.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/green] Written Markdown → [cyan]{path}[/cyan]")
    else:
        console.print(text)


WRITERS = {
    "csv": _write_csv,
    "json": _write_json,
    "xml": _write_xml,
    "yaml": _write_yaml,
    "yml": _write_yaml,
    "excel": _write_excel,
    "xlsx": _write_excel,
    "markdown": _write_markdown,
    "md": _write_markdown,
}


def _get_writer(fmt: str):
    """Resolve writer function for the target format."""
    fmt = fmt.lower().lstrip(".")
    writer = WRITERS.get(fmt)
    if not writer:
        console.print(f"[red]Error:[/red] Unsupported output format: {fmt}")
        console.print(f"[dim]Supported: {', '.join(sorted(set(WRITERS.keys())))}[/dim]")
        raise SystemExit(1)
    return writer


def _infer_output_format(output: str | None, to: str | None) -> str:
    """Infer output format from --to flag or output file extension."""
    if to:
        return to.lower().lstrip(".")
    if output:
        ext = Path(output).suffix.lower().lstrip(".")
        if ext:
            return ext
    return "csv"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option("1.0.0", prog_name="format_converter")
def cli() -> None:
    """Format Converter — convert between CSV, JSON, XML, YAML, Excel, and Markdown."""


@cli.command()
@click.argument("input_file")
@click.option("--to", "-t", "target_fmt", default=None, help="Target format (csv/json/xml/yaml/xlsx/md).")
@click.option("--output", "-o", default=None, help="Output file path.")
@click.option("--from", "source_fmt", default=None, help="Force source format (auto-detected by default).")
@click.option("--sheet", default=None, help="Excel sheet name or index (for .xlsx input).")
@click.option("--delimiter", "-d", default=",", help="CSV delimiter.")
@click.option("--indent", "-i", type=int, default=2, help="JSON indentation.")
@click.option("--root-tag", default="records", help="XML root element tag name.")
@click.option("--preview", "-p", is_flag=True, help="Preview first 20 rows instead of writing.")
def convert(
    input_file: str,
    target_fmt: str | None,
    output: str | None,
    source_fmt: str | None,
    sheet: str | None,
    delimiter: str,
    indent: int,
    root_tag: str,
    preview: bool,
) -> None:
    """Convert a data file from one format to another."""
    p = Path(input_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {input_file}")
        raise SystemExit(1)

    # Detect or use source format
    src = source_fmt or _detect_format(p)
    console.print(f"[blue]Source format:[/blue] {src}")

    # Read
    reader = READERS.get(src)
    if not reader:
        console.print(f"[red]Error:[/red] Cannot read format: {src}")
        raise SystemExit(1)

    try:
        kwargs: dict[str, Any] = {"delimiter": delimiter}
        if sheet is not None:
            kwargs["sheet"] = sheet
        df = reader(p, **kwargs)
    except Exception as exc:
        console.print(f"[red]Read error:[/red] {exc}")
        raise SystemExit(1)

    console.print(f"[green]✓[/green] Loaded {len(df)} rows × {len(df.columns)} columns")

    # Preview
    if preview:
        preview_table = Table(title="Preview (first 20 rows)", box=box.ROUNDED)
        for col in df.columns:
            preview_table.add_column(str(col), overflow="fold")
        for _, row in df.head(20).iterrows():
            preview_table.add_row(*("" if pd.isna(v) else str(v)[:60] for v in row))
        console.print(preview_table)
        return

    # Resolve target format
    tgt = _infer_output_format(output, target_fmt)
    console.print(f"[blue]Target format:[/blue] {tgt}")

    # Write
    writer = _get_writer(tgt)
    out_path = Path(output) if output else None

    try:
        kwargs = {"indent": indent}
        writer(df, out_path, **kwargs)
    except Exception as exc:
        console.print(f"[red]Write error:[/red] {exc}")
        raise SystemExit(1)


@cli.command()
@click.argument("input_dir")
@click.option("--from", "source_fmt", default=None, help="Source format filter.")
@click.option("--to", "-t", "target_fmt", required=True, help="Target format.")
@click.option("--output-dir", "-o", default=None, help="Output directory (default: input_dir/converted).")
@click.option("--recursive", "-r", is_flag=True, help="Process subdirectories.")
@click.option("--delimiter", "-d", default=",", help="CSV delimiter.")
@click.option("--indent", "-i", type=int, default=2, help="JSON indentation.")
def batch(
    input_dir: str,
    source_fmt: str | None,
    target_fmt: str,
    output_dir: str | None,
    recursive: bool,
    delimiter: str,
    indent: int,
) -> None:
    """Batch-convert all files in a directory."""
    in_dir = Path(input_dir)
    if not in_dir.is_dir():
        console.print(f"[red]Error:[/red] Not a directory: {input_dir}")
        raise SystemExit(1)

    out_dir = Path(output_dir) if output_dir else in_dir / "converted"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect files
    pattern = "**/*" if recursive else "*"
    all_files = [f for f in in_dir.glob(pattern) if f.is_file()]
    if source_fmt:
        files = [f for f in all_files if _detect_format(f) == source_fmt]
    else:
        # Skip non-data files
        known_exts = {".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".xlsx", ".xls", ".md"}
        files = [f for f in all_files if f.suffix.lower() in known_exts]

    if not files:
        console.print("[yellow]No matching files found.[/yellow]")
        return

    tgt = target_fmt.lower().lstrip(".")
    writer = _get_writer(tgt)
    ext_map = {"csv": ".csv", "json": ".json", "xml": ".xml", "yaml": ".yaml", "yml": ".yaml", "xlsx": ".xlsx", "markdown": ".md", "md": ".md"}
    out_ext = ext_map.get(tgt, f".{tgt}")

    console.print(f"[blue]Converting {len(files)} file(s) → {tgt}[/blue]")

    results = Table(title="Batch Results", box=box.ROUNDED)
    results.add_column("File", style="cyan")
    results.add_column("Status")
    results.add_column("Rows", justify="right")

    success = 0
    for f in files:
        rel = f.relative_to(in_dir)
        out_file = out_dir / rel.with_suffix(out_ext)
        out_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            src = _detect_format(f)
            reader = READERS.get(src)
            if not reader:
                results.add_row(str(rel), "[yellow]Skipped[/yellow]", "—")
                continue
            df = reader(f, delimiter=delimiter)
            writer(df, out_file, indent=indent)
            results.add_row(str(rel), "[green]OK[/green]", str(len(df)))
            success += 1
        except Exception as exc:
            results.add_row(str(rel), f"[red]Error: {exc}[/red]", "—")

    console.print(results)
    console.print(f"[green]✓[/green] {success}/{len(files)} files converted → [cyan]{out_dir}[/cyan]")


@cli.command()
@click.argument("input_file")
def detect(input_file: str) -> None:
    """Detect the format of a file."""
    p = Path(input_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {input_file}")
        raise SystemExit(1)

    fmt = _detect_format(p)
    size = p.stat().st_size

    info = Table(title="File Detection", box=box.ROUNDED)
    info.add_column("Property", style="cyan")
    info.add_column("Value", style="green")
    info.add_row("File", str(p.name))
    info.add_row("Size", f"{size:,} bytes ({size / 1024:.1f} KB)")
    info.add_row("Extension", p.suffix or "(none)")
    info.add_row("Detected format", fmt)
    console.print(info)


@cli.command()
@click.argument("input_file")
@click.option("--from", "source_fmt", default=None, help="Source format.")
@click.option("--delimiter", "-d", default=",", help="CSV delimiter.")
def info(input_file: str, source_fmt: str | None, delimiter: str) -> None:
    """Show metadata about a data file (columns, types, row count)."""
    p = Path(input_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {input_file}")
        raise SystemExit(1)

    src = source_fmt or _detect_format(p)
    reader = READERS.get(src)
    if not reader:
        console.print(f"[red]Error:[/red] Cannot read format: {src}")
        raise SystemExit(1)

    try:
        df = reader(p, delimiter=delimiter)
    except Exception as exc:
        console.print(f"[red]Read error:[/red] {exc}")
        raise SystemExit(1)

    # Overview
    overview = Table(title=f"File Info: {p.name}", box=box.ROUNDED)
    overview.add_column("Property", style="cyan")
    overview.add_column("Value", style="green")
    overview.add_row("Format", src)
    overview.add_row("Rows", f"{len(df):,}")
    overview.add_row("Columns", str(len(df.columns)))
    overview.add_row("Memory", f"{df.memory_usage(deep=True).sum() / 1024:.1f} KB")
    console.print(overview)

    # Column details
    col_table = Table(title="Columns", box=box.ROUNDED)
    col_table.add_column("Column", style="cyan")
    col_table.add_column("Type")
    col_table.add_column("Non-Null")
    col_table.add_column("Null %")
    col_table.add_column("Sample")
    for col in df.columns:
        null_pct = df[col].isna().mean() * 100
        sample = str(df[col].dropna().iloc[0]) if not df[col].dropna().empty else "—"
        col_table.add_row(str(col), str(df[col].dtype), str(df[col].notna().sum()), f"{null_pct:.1f}%", sample[:50])
    console.print(col_table)


if __name__ == "__main__":
    cli()
