#!/usr/bin/env python3
"""CSV Transformer — Read, transform, merge, and pivot CSV files.

Standalone CLI tool for CSV data transformations including column renaming,
row filtering, computed columns, multi-file merging, and pivot tables.
Outputs to CSV, Excel, or JSON.

Usage examples:
    python csv_transformer.py transform input.csv --rename "old:new" --filter "age>30"
    python csv_transformer.py merge file1.csv file2.csv --on id --how left
    python csv_transformer.py pivot input.csv --index date --columns category --values amount
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()


def _read_csv(path: str, encoding: str = "utf-8", delimiter: str = ",") -> pd.DataFrame:
    """Read a CSV file and return a DataFrame."""
    p = Path(path)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {path}")
        raise SystemExit(1)
    try:
        df = pd.read_csv(p, encoding=encoding, delimiter=delimiter)
        console.print(f"[green]✓[/green] Loaded {len(df)} rows × {len(df.columns)} columns from [cyan]{p.name}[/cyan]")
        return df
    except Exception as exc:
        console.print(f"[red]Error reading CSV:[/red] {exc}")
        raise SystemExit(1)


def _parse_renames(items: tuple[str, ...]) -> dict[str, str]:
    """Parse 'old:new' rename pairs into a dict."""
    mapping: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            console.print(f"[yellow]Warning:[/yellow] Skipping malformed rename (expected 'old:new'): {item}")
            continue
        old, new = item.split(":", 1)
        mapping[old.strip()] = new.strip()
    return mapping


def _parse_filters(items: tuple[str, ...]) -> str:
    """Combine multiple filter expressions with 'and'."""
    return " and ".join(f"({expr})" for expr in items)


def _parse_computed(items: tuple[str, ...]) -> list[tuple[str, str]]:
    """Parse 'name=expression' computed column specs."""
    result: list[tuple[str, str]] = []
    for item in items:
        if "=" not in item:
            console.print(f"[yellow]Warning:[/yellow] Skipping malformed computed column (expected 'name=expr'): {item}")
            continue
        name, expr = item.split("=", 1)
        result.append((name.strip(), expr.strip()))
    return result


def _write_output(df: pd.DataFrame, output: str | None, fmt: str | None) -> None:
    """Write DataFrame to the specified format."""
    if output is None:
        # Default to CSV stdout
        console.print(df.to_csv(index=False))
        return

    out_path = Path(output)
    fmt = fmt or out_path.suffix.lstrip(".")

    try:
        if fmt in ("csv",):
            df.to_csv(out_path, index=False)
        elif fmt in ("xlsx", "xls", "excel"):
            df.to_excel(out_path, index=False)
        elif fmt in ("json",):
            df.to_json(out_path, orient="records", indent=2, force_ascii=False)
        else:
            console.print(f"[red]Unsupported output format:[/red] {fmt}")
            raise SystemExit(1)
        console.print(f"[green]✓[/green] Written to [cyan]{out_path}[/cyan] ({fmt})")
    except Exception as exc:
        console.print(f"[red]Error writing output:[/red] {exc}")
        raise SystemExit(1)


def _show_preview(df: pd.DataFrame, rows: int = 10) -> None:
    """Show a rich preview table of the DataFrame."""
    preview = df.head(rows)
    table = Table(title="Data Preview", box=box.ROUNDED, show_lines=True)
    for col in preview.columns:
        table.add_column(str(col), overflow="fold")
    for _, row in preview.iterrows():
        table.add_row(*(str(v) for v in row))
    console.print(table)
    if len(df) > rows:
        console.print(f"[dim]... and {len(df) - rows} more rows[/dim]")


@click.group()
@click.version_option("1.0.0", prog_name="csv_transformer")
def cli() -> None:
    """CSV Transformer — transform, merge, and pivot CSV data."""


@cli.command()
@click.argument("input_file")
@click.option("--rename", "-r", multiple=True, help="Rename columns: 'old_name:new_name'. Repeatable.")
@click.option("--filter", "-f", "filters", multiple=True, help="Filter expression (pandas query syntax). Repeatable.")
@click.option("--computed", "-c", multiple=True, help="Computed column: 'name=expression'. Repeatable.")
@click.option("--drop", "-d", multiple=True, help="Columns to drop. Repeatable.")
@click.option("--sort", "-s", default=None, help="Sort by column (prefix '-' for descending).")
@click.option("--head", "-n", type=int, default=None, help="Keep only first N rows.")
@click.option("--sample", type=float, default=None, help="Random sample fraction (0-1).")
@click.option("--dedup", is_flag=True, help="Remove duplicate rows.")
@click.option("--dedup-cols", default=None, help="Columns to check for duplicates (comma-separated).")
@click.option("--output", "-o", default=None, help="Output file path.")
@click.option("--format", "fmt", default=None, type=click.Choice(["csv", "xlsx", "json"]), help="Output format.")
@click.option("--preview", "-p", is_flag=True, help="Show preview table instead of writing.")
def transform(
    input_file: str,
    rename: tuple[str, ...],
    filters: tuple[str, ...],
    computed: tuple[str, ...],
    drop: tuple[str, ...],
    sort: str | None,
    head: int | None,
    sample: float | None,
    dedup: bool,
    dedup_cols: str | None,
    output: str | None,
    fmt: str | None,
    preview: bool,
) -> None:
    """Apply transformations to a single CSV file."""
    df = _read_csv(input_file)

    # Rename columns
    rename_map = _parse_renames(rename)
    if rename_map:
        df = df.rename(columns=rename_map)
        console.print(f"[blue]Renamed columns:[/blue] {rename_map}")

    # Drop columns
    if drop:
        existing = [c for c in drop if c in df.columns]
        df = df.drop(columns=existing)
        if existing:
            console.print(f"[blue]Dropped columns:[/blue] {existing}")

    # Filter rows
    if filters:
        expr = _parse_filters(filters)
        before = len(df)
        try:
            df = df.query(expr)
            console.print(f"[blue]Filtered:[/blue] {before} → {len(df)} rows")
        except Exception as exc:
            console.print(f"[red]Filter error:[/red] {exc}")
            raise SystemExit(1)

    # Computed columns
    for name, expr in _parse_computed(computed):
        try:
            df[name] = df.eval(expr)
            console.print(f"[blue]Added column:[/blue] {name} = {expr}")
        except Exception as exc:
            console.print(f"[red]Computed column error for '{name}':[/red] {exc}")
            raise SystemExit(1)

    # Sort
    if sort:
        ascending = True
        col = sort
        if sort.startswith("-"):
            ascending = False
            col = sort[1:]
        df = df.sort_values(col, ascending=ascending)
        console.print(f"[blue]Sorted by:[/blue] {col} ({'asc' if ascending else 'desc'})")

    # Head
    if head is not None:
        df = df.head(head)
        console.print(f"[blue]Kept first {head} rows[/blue]")

    # Sample
    if sample is not None:
        df = df.sample(frac=sample)
        console.print(f"[blue]Sampled {sample:.0%} → {len(df)} rows[/blue]")

    # Dedup
    if dedup:
        subset = dedup_cols.split(",") if dedup_cols else None
        before = len(df)
        df = df.drop_duplicates(subset=subset)
        console.print(f"[blue]Deduplicated:[/blue] {before} → {len(df)} rows")

    if preview:
        _show_preview(df)
    else:
        _write_output(df, output, fmt)


@cli.command()
@click.argument("files", nargs=-1, required=True)
@click.option("--on", "-k", default=None, help="Column(s) to merge on (comma-separated).")
@click.option("--how", "-h", default="inner", type=click.Choice(["inner", "outer", "left", "right"]), help="Merge type.")
@click.option("--output", "-o", default=None, help="Output file path.")
@click.option("--format", "fmt", default=None, type=click.Choice(["csv", "xlsx", "json"]), help="Output format.")
@click.option("--preview", "-p", is_flag=True, help="Show preview table instead of writing.")
def merge(
    files: tuple[str, ...],
    on: str | None,
    how: str,
    output: str | None,
    fmt: str | None,
    preview: bool,
) -> None:
    """Merge multiple CSV files."""
    if len(files) < 2:
        console.print("[red]Error:[/red] Need at least 2 files to merge.")
        raise SystemExit(1)

    dfs = [_read_csv(f) for f in files]
    merge_cols = on.split(",") if on else None

    result = dfs[0]
    for i, df in enumerate(dfs[1:], 2):
        try:
            result = result.merge(df, on=merge_cols, how=how, suffixes=("", f"_{i}"))
        except Exception as exc:
            console.print(f"[red]Merge error with file {i}:[/red] {exc}")
            raise SystemExit(1)

    console.print(f"[green]Merged {len(files)} files → {len(result)} rows × {len(result.columns)} columns[/green]")

    if preview:
        _show_preview(result)
    else:
        _write_output(result, output, fmt)


@cli.command()
@click.argument("input_file")
@click.option("--index", "-i", required=True, help="Column to use as pivot index (rows).")
@click.option("--columns", "-c", required=True, help="Column to use as pivot columns.")
@click.option("--values", "-v", required=True, help="Column to use as pivot values.")
@click.option("--aggfunc", "-a", default="mean", help="Aggregation function: mean, sum, count, first, last.")
@click.option("--fill", "-f", default=None, help="Fill value for missing entries.")
@click.option("--output", "-o", default=None, help="Output file path.")
@click.option("--format", "fmt", default=None, type=click.Choice(["csv", "xlsx", "json"]), help="Output format.")
@click.option("--preview", "-p", is_flag=True, help="Show preview table instead of writing.")
def pivot(
    input_file: str,
    index: str,
    columns: str,
    values: str,
    aggfunc: str,
    fill: str | None,
    output: str | None,
    fmt: str | None,
    preview: bool,
) -> None:
    """Create a pivot table from CSV data."""
    df = _read_csv(input_file)

    fill_val = None
    if fill is not None:
        try:
            fill_val = float(fill)
        except ValueError:
            fill_val = fill

    try:
        pivot_df = pd.pivot_table(
            df,
            index=index,
            columns=columns,
            values=values,
            aggfunc=aggfunc,
            fill_value=fill_val,
        )
        pivot_df = pivot_df.reset_index()
    except Exception as exc:
        console.print(f"[red]Pivot error:[/red] {exc}")
        raise SystemExit(1)

    console.print(f"[green]Pivot:[/green] {len(pivot_df)} rows × {len(pivot_df.columns)} columns")

    if preview:
        _show_preview(pivot_df)
    else:
        _write_output(pivot_df, output, fmt)


@cli.command()
@click.argument("input_file")
@click.option("--encoding", "-e", default="utf-8", help="File encoding.")
@click.option("--delimiter", "-d", default=",", help="CSV delimiter.")
def info(input_file: str, encoding: str, delimiter: str) -> None:
    """Show summary info about a CSV file."""
    df = _read_csv(input_file, encoding=encoding, delimiter=delimiter)

    # Basic info
    info_table = Table(title="CSV Info", box=box.ROUNDED)
    info_table.add_column("Property", style="cyan")
    info_table.add_column("Value", style="green")
    info_table.add_row("Rows", str(len(df)))
    info_table.add_row("Columns", str(len(df.columns)))
    info_table.add_row("Memory", f"{df.memory_usage(deep=True).sum() / 1024:.1f} KB")
    info_table.add_row("Dtypes", str(dict(df.dtypes.value_counts())))
    console.print(info_table)

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


@cli.command()
@click.argument("input_file")
@click.option("--delimiter", "-d", default=",", help="CSV delimiter.")
def validate(input_file: str, delimiter: str) -> None:
    """Validate CSV file structure and data quality."""
    p = Path(input_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {input_file}")
        raise SystemExit(1)

    issues: list[str] = []

    try:
        df = pd.read_csv(p, delimiter=delimiter, on_bad_lines="warn")
    except Exception as exc:
        console.print(f"[red]Cannot parse CSV:[/red] {exc}")
        raise SystemExit(1)

    # Check for empty file
    if df.empty:
        issues.append("File is empty (no data rows)")

    # Check for unnamed columns
    unnamed = [c for c in df.columns if str(c).startswith("Unnamed")]
    if unnamed:
        issues.append(f"Unnamed columns detected: {unnamed}")

    # Check for duplicate column names
    dupes = df.columns[df.columns.duplicated()].tolist()
    if dupes:
        issues.append(f"Duplicate column names: {dupes}")

    # Check for high null rates
    for col in df.columns:
        null_rate = df[col].isna().mean()
        if null_rate > 0.5:
            issues.append(f"Column '{col}' has {null_rate:.0%} null values")

    # Check for duplicate rows
    dup_rows = df.duplicated().sum()
    if dup_rows > 0:
        issues.append(f"{dup_rows} duplicate rows found")

    if issues:
        console.print(Panel("\n".join(f"• {i}" for i in issues), title="[yellow]Validation Issues[/yellow]", border_style="yellow"))
    else:
        console.print(Panel("[green]✓ No issues found[/green]", title="Validation", border_style="green"))


if __name__ == "__main__":
    cli()
