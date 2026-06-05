#!/usr/bin/env python3
"""Find duplicate files by content hash (MD5 or SHA256) with Rich progress display.

Usage:
    python duplicate_finder.py scan ~/Downloads ~/Documents
    python duplicate_finder.py scan . --algo md5 --min-size 1024
    python duplicate_finder.py scan . --dry-run --delete-originals
"""

import hashlib
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.prompt import Confirm

console = Console()


def file_hash(filepath: Path, algo: str, chunk_size: int = 8192) -> str:
    """Compute file hash using the specified algorithm."""
    h = hashlib.new(algo)
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
    except (OSError, PermissionError):
        return ""
    return h.hexdigest()


def collect_files(directories: list[str], min_size: int = 0, extensions: Optional[set[str]] = None) -> list[Path]:
    """Collect all files from given directories."""
    files = []
    for d in directories:
        root = Path(d).expanduser().resolve()
        if not root.is_dir():
            console.print(f"[yellow]⚠ Skipping non-directory: {root}[/yellow]")
            continue
        for p in root.rglob("*"):
            if p.is_file():
                if min_size and p.stat().st_size < min_size:
                    continue
                if extensions and p.suffix.lower() not in extensions:
                    continue
                files.append(p)
    return files


def find_duplicates(files: list[Path], algo: str = "sha256") -> dict[str, list[Path]]:
    """Find duplicate files grouped by hash."""
    # First pass: group by file size (fast pre-filter)
    size_groups: dict[int, list[Path]] = defaultdict(list)
    for f in files:
        try:
            size_groups[f.stat().st_size].append(f)
        except OSError:
            continue

    # Second pass: hash only files with duplicate sizes
    candidates = [f for group in size_groups.values() if len(group) > 1 for f in group]

    if not candidates:
        return {}

    hash_groups: dict[str, list[Path]] = defaultdict(list)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Hashing with {algo.upper()}...", total=len(candidates))
        for f in candidates:
            h = file_hash(f, algo)
            if h:
                hash_groups[h].append(f)
            progress.advance(task)

    # Return only groups with duplicates
    return {h: paths for h, paths in hash_groups.items() if len(paths) > 1}


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@click.group()
def cli():
    """🔍 Duplicate Finder — find and manage duplicate files by content hash."""


@cli.command()
@click.argument("directories", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--algo", "-a", type=click.Choice(["md5", "sha256"]), default="sha256", help="Hash algorithm.")
@click.option("--min-size", default=0, help="Minimum file size in bytes.")
@click.option("--extensions", "-e", default=None, help="Comma-separated extensions (e.g. .jpg,.png,.pdf).")
@click.option("--dry-run", is_flag=True, help="Show duplicates without deleting.")
@click.option("--delete-originals", is_flag=True, help="Keep one copy, delete rest.")
@click.option("--move-to", "-m", default=None, type=click.Path(), help="Move duplicates to this directory instead of deleting.")
def scan(directories: tuple, algo: str, min_size: int, extensions: Optional[str],
         dry_run: bool, delete_originals: bool, move_to: Optional[str]):
    """Scan directories for duplicate files."""
    ext_set = {e.strip() for e in extensions.split(",")} if extensions else None

    console.print(f"[cyan]Scanning {len(directories)} directories...[/cyan]")
    files = collect_files(list(directories), min_size, ext_set)
    console.print(f"Found [bold]{len(files)}[/bold] files to analyze.\n")

    if not files:
        console.print("[yellow]No files found.[/yellow]")
        return

    dupes = find_duplicates(files, algo)

    if not dupes:
        console.print("[green]✓ No duplicate files found![/green]")
        return

    # Display results
    total_dupes = 0
    total_waste = 0
    all_to_process: list[tuple[Path, list[Path]]] = []

    for h, paths in dupes.items():
        paths.sort(key=lambda p: p.stat().st_mtime)  # oldest first (keep oldest)
        file_size = paths[0].stat().st_size
        waste = file_size * (len(paths) - 1)
        total_dupes += len(paths) - 1
        total_waste += waste
        all_to_process.append((paths[0], paths[1:]))

        table = Table(show_header=True, header_style="bold cyan", show_lines=True)
        table.add_column("Status", width=8)
        table.add_column("File Path")
        table.add_column("Size", justify="right")
        table.add_column("Modified", style="dim")

        for i, p in enumerate(paths):
            try:
                mtime = os.path.getmtime(p)
                mtime_str = f"{mtime:.0f}"
            except OSError:
                mtime_str = "?"
            status = "[green]KEEP[/green]" if i == 0 else "[red]DUP[/red]"
            table.add_row(status, str(p), format_size(file_size), mtime_str)

        console.print(table)
        console.print(f"  Hash: [dim]{h[:16]}...[/dim] | Waste: [yellow]{format_size(waste)}[/yellow]\n")

    console.print(Panel(
        f"[bold]Summary:[/bold]\n"
        f"  Duplicate sets: {len(dupes)}\n"
        f"  Duplicate files: {total_dupes}\n"
        f"  Wasted space: [yellow]{format_size(total_waste)}[/yellow]",
        title="🔍 Scan Results",
        border_style="cyan",
    ))

    if dry_run:
        console.print("[dim]Dry run — no files were modified.[/dim]")
        return

    if not (delete_originals or move_to):
        return

    # Process duplicates
    if move_to:
        dest = Path(move_to)
        dest.mkdir(parents=True, exist_ok=True)

    if delete_originals or move_to:
        if not Confirm.ask(f"Process {total_dupes} duplicate files?"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    processed = 0
    for _, dupes_list in all_to_process:
        for p in dupes_list:
            try:
                if move_to:
                    dest_file = Path(move_to) / p.name
                    if dest_file.exists():
                        dest_file = dest_file.with_stem(f"{dest_file.stem}_{hash(str(p)) & 0xFFFF:04x}")
                    shutil.move(str(p), str(dest_file))
                    console.print(f"[cyan]MOVED[/cyan] {p} → {dest_file}")
                else:
                    p.unlink()
                    console.print(f"[red]DELETED[/red] {p}")
                processed += 1
            except OSError as e:
                console.print(f"[yellow]⚠ Could not process {p}: {e}[/yellow]")

    console.print(f"\n[green]✓ Processed {processed} duplicate files.[/green]")


@cli.command()
@click.argument("directory", type=click.Path(exists=True))
def summary(directory: str):
    """Quick summary of potential duplicates by file size."""
    files = collect_files([directory])
    size_groups: dict[int, list[Path]] = defaultdict(list)
    for f in files:
        try:
            size_groups[f.stat().st_size].append(f)
        except OSError:
            continue

    dupes = {s: paths for s, paths in size_groups.items() if len(paths) > 1}
    if not dupes:
        console.print("[green]No potential duplicates by size.[/green]")
        return

    total = sum(s * (len(p) - 1) for s, p in dupes.items())
    table = Table(title="Potential Duplicates (by size)")
    table.add_column("Size", justify="right")
    table.add_column("Copies", justify="right")
    table.add_column("Sample Files")
    for size, paths in sorted(dupes.items(), key=lambda x: x[0] * len(x[1]), reverse=True)[:20]:
        samples = ", ".join(p.name for p in paths[:3])
        if len(paths) > 3:
            samples += f" (+{len(paths)-3} more)"
        table.add_row(format_size(size), str(len(paths)), samples)
    console.print(table)
    console.print(f"\nPotential wasted space: [yellow]{format_size(total)}[/yellow] (run 'scan' to verify with hash)")


if __name__ == "__main__":
    cli()
