#!/usr/bin/env python3
"""
file_organizer.py — Smart File Organizer

Organize files by type, date, or size into structured directories.
Features:
- Organize by file extension/type (images, documents, videos, etc.)
- Organize by modification date (year/month folders)
- Organize by file size (tiny/small/medium/large/huge)
- Custom rules via JSON config
- Dry-run mode — preview before moving
- Full undo capability via operation log
- Duplicate handling (rename, skip, overwrite)

Usage:
    python file_organizer.py organize ~/Downloads --by-type
    python file_organizer.py organize ~/Downloads --by-date
    python file_organizer.py organize ~/Downloads --by-size
    python file_organizer.py organize ~/Downloads --config rules.json --dry-run
    python file_organizer.py undo <log-file>
    python file_organizer.py scan ~/Downloads
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.tree import Tree

console = Console()


# ---------------------------------------------------------------------------
# File type mappings
# ---------------------------------------------------------------------------

FILE_TYPES: dict[str, list[str]] = {
    "Images": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tiff", ".tif", ".heic", ".heif", ".raw", ".cr2", ".nef"],
    "Documents": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt", ".ods", ".odp", ".csv", ".md", ".tex", ".pages", ".numbers", ".key"],
    "Videos": [".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts"],
    "Audio": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a", ".opus", ".aiff"],
    "Archives": [".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz", ".tar.gz", ".tar.bz2"],
    "Code": [".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".bash", ".zsh", ".sql", ".r", ".lua", ".pl"],
    "Data": [".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".db", ".sqlite", ".sqlite3", ".parquet", ".avro"],
    "Web": [".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte"],
    "Fonts": [".ttf", ".otf", ".woff", ".woff2", ".eot"],
    "Executables": [".exe", ".msi", ".dmg", ".app", ".deb", ".rpm", ".AppImage", ".snap", ".flatpak"],
    "Disk Images": [".iso", ".img", ".vdi", ".vmdk", ".qcow2"],
}

# Build reverse lookup: extension -> category
_EXT_TO_TYPE: dict[str, str] = {}
for category, exts in FILE_TYPES.items():
    for ext in exts:
        _EXT_TO_TYPE[ext.lower()] = category

# Size thresholds (bytes)
SIZE_CATEGORIES = [
    ("Tiny", 0, 1024),               # < 1 KB
    ("Small", 1024, 102_400),         # 1 KB – 100 KB
    ("Medium", 102_400, 10_485_760),  # 100 KB – 10 MB
    ("Large", 10_485_760, 104_857_600),  # 10 MB – 100 MB
    ("Huge", 104_857_600, float("inf")),  # > 100 MB
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FileAction:
    """Record of one file move operation (for undo)."""

    source: str
    destination: str
    timestamp: float = field(default_factory=time.time)
    action: str = "move"  # "move" or "copy"


@dataclass
class OrganizeResult:
    """Summary of an organize operation."""

    total_scanned: int = 0
    total_moved: int = 0
    total_skipped: int = 0
    total_errors: int = 0
    actions: list[FileAction] = field(default_factory=list)
    by_category: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _get_file_type(filepath: Path) -> str:
    """Determine the file type category from extension."""
    ext = filepath.suffix.lower()
    return _EXT_TO_TYPE.get(ext, "Other")


def _get_size_category(size_bytes: int) -> str:
    """Classify file by size."""
    for name, lo, hi in SIZE_CATEGORIES:
        if lo <= size_bytes < hi:
            return name
    return "Huge"


def _get_date_folder(filepath: Path, fmt: str = "%Y/%m") -> str:
    """Get a date-based folder path from file modification time."""
    mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
    return mtime.strftime(fmt)


def _safe_move(src: Path, dst: Path, duplicate: str = "rename") -> Path:
    """Move a file safely, handling duplicates."""
    if not dst.parent.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists():
        if duplicate == "skip":
            raise FileExistsError(f"Destination exists: {dst}")
        elif duplicate == "overwrite":
            pass  # shutil.move will overwrite
        elif duplicate == "rename":
            stem = dst.stem
            suffix = dst.suffix
            counter = 1
            while dst.exists():
                dst = dst.parent / f"{stem}_{counter}{suffix}"
                counter += 1

    shutil.move(str(src), str(dst))
    return dst


def organize_by_type(
    source_dir: Path,
    target_dir: Path,
    recursive: bool = False,
    duplicate: str = "rename",
    dry_run: bool = False,
    exclude_hidden: bool = True,
) -> OrganizeResult:
    """Organize files into type-based folders."""
    result = OrganizeResult()

    pattern = "**/*" if recursive else "*"
    files = sorted(source_dir.glob(pattern))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Organizing by type...", total=None)

        for f in files:
            if not f.is_file():
                continue
            if exclude_hidden and f.name.startswith("."):
                continue

            result.total_scanned += 1

            file_type = _get_file_type(f)
            dest_dir = target_dir / file_type
            dest = dest_dir / f.name

            if f.resolve() == dest.resolve():
                result.total_skipped += 1
                continue

            try:
                if dry_run:
                    result.actions.append(FileAction(source=str(f), destination=str(dest)))
                    result.total_moved += 1
                else:
                    actual_dest = _safe_move(f, dest, duplicate=duplicate)
                    result.actions.append(FileAction(source=str(f), destination=str(actual_dest)))
                    result.total_moved += 1
                result.by_category[file_type] += 1
            except Exception as exc:
                result.total_errors += 1
                result.errors.append(f"{f.name}: {exc}")

            progress.advance(task)

    return result


def organize_by_date(
    source_dir: Path,
    target_dir: Path,
    date_format: str = "%Y/%m",
    recursive: bool = False,
    duplicate: str = "rename",
    dry_run: bool = False,
    exclude_hidden: bool = True,
) -> OrganizeResult:
    """Organize files into date-based folders."""
    result = OrganizeResult()

    pattern = "**/*" if recursive else "*"
    files = sorted(source_dir.glob(pattern))

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task = progress.add_task("Organizing by date...", total=None)

        for f in files:
            if not f.is_file():
                continue
            if exclude_hidden and f.name.startswith("."):
                continue

            result.total_scanned += 1
            date_folder = _get_date_folder(f, date_format)
            dest = target_dir / date_folder / f.name

            try:
                if dry_run:
                    result.actions.append(FileAction(source=str(f), destination=str(dest)))
                    result.total_moved += 1
                else:
                    actual_dest = _safe_move(f, dest, duplicate=duplicate)
                    result.actions.append(FileAction(source=str(f), destination=str(actual_dest)))
                    result.total_moved += 1
                result.by_category[date_folder] += 1
            except Exception as exc:
                result.total_errors += 1
                result.errors.append(f"{f.name}: {exc}")

            progress.advance(task)

    return result


def organize_by_size(
    source_dir: Path,
    target_dir: Path,
    recursive: bool = False,
    duplicate: str = "rename",
    dry_run: bool = False,
    exclude_hidden: bool = True,
) -> OrganizeResult:
    """Organize files into size-based folders."""
    result = OrganizeResult()

    pattern = "**/*" if recursive else "*"
    files = sorted(source_dir.glob(pattern))

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task = progress.add_task("Organizing by size...", total=None)

        for f in files:
            if not f.is_file():
                continue
            if exclude_hidden and f.name.startswith("."):
                continue

            result.total_scanned += 1
            size = f.stat().st_size
            category = _get_size_category(size)
            dest = target_dir / category / f.name

            try:
                if dry_run:
                    result.actions.append(FileAction(source=str(f), destination=str(dest)))
                    result.total_moved += 1
                else:
                    actual_dest = _safe_move(f, dest, duplicate=duplicate)
                    result.actions.append(FileAction(source=str(f), destination=str(actual_dest)))
                    result.total_moved += 1
                result.by_category[category] += 1
            except Exception as exc:
                result.total_errors += 1
                result.errors.append(f"{f.name}: {exc}")

            progress.advance(task)

    return result


def organize_with_rules(
    source_dir: Path,
    target_dir: Path,
    rules: list[dict[str, Any]],
    recursive: bool = False,
    duplicate: str = "rename",
    dry_run: bool = False,
    exclude_hidden: bool = True,
) -> OrganizeResult:
    """Organize files using custom rules from config."""
    result = OrganizeResult()

    pattern = "**/*" if recursive else "*"
    files = sorted(source_dir.glob(pattern))

    # Pre-compile rules for performance
    for rule in rules:
        rule["_ext_set"] = {e.lower() for e in rule.get("extensions", [])}
        rule["_name_contains"] = [s.lower() for s in rule.get("name_contains", [])]
        rule["_max_size"] = rule.get("max_size", float("inf"))
        rule["_min_size"] = rule.get("min_size", 0)

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task = progress.add_task("Organizing with rules...", total=None)

        for f in files:
            if not f.is_file():
                continue
            if exclude_hidden and f.name.startswith("."):
                continue

            result.total_scanned += 1
            ext = f.suffix.lower()
            size = f.stat().st_size
            name_lower = f.name.lower()
            matched = False

            for rule in rules:
                ext_match = not rule["_ext_set"] or ext in rule["_ext_set"]
                name_match = not rule["_name_contains"] or any(s in name_lower for s in rule["_name_contains"])
                size_match = rule["_min_size"] <= size <= rule["_max_size"]

                if ext_match and name_match and size_match:
                    folder = rule.get("folder", "Other")
                    dest = target_dir / folder / f.name

                    try:
                        if dry_run:
                            result.actions.append(FileAction(source=str(f), destination=str(dest)))
                        else:
                            _safe_move(f, dest, duplicate=duplicate)
                            result.actions.append(FileAction(source=str(f), destination=str(dest)))
                        result.total_moved += 1
                        result.by_category[folder] += 1
                    except Exception as exc:
                        result.total_errors += 1
                        result.errors.append(f"{f.name}: {exc}")

                    matched = True
                    break

            if not matched:
                result.total_skipped += 1

            progress.advance(task)

    return result


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def undo_operations(log_path: Path) -> int:
    """Reverse all operations recorded in a log file. Returns count of undone files."""
    with open(log_path, "r", encoding="utf-8") as fh:
        actions = [FileAction(**a) for a in json.load(fh)]

    undone = 0
    # Reverse order
    for action in reversed(actions):
        src = Path(action.source)
        dst = Path(action.destination)
        if dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            undone += 1
            console.print(f"  [green]↩[/green] {dst.name} → {src}")
        else:
            console.print(f"  [yellow]⚠[/yellow] Not found: {dst}")

    return undone


# ---------------------------------------------------------------------------
# Scan / Preview
# ---------------------------------------------------------------------------

def scan_directory(source: Path) -> None:
    """Scan and display file distribution without moving anything."""
    files = [f for f in source.iterdir() if f.is_file() and not f.name.startswith(".")]

    type_counter: Counter = Counter()
    size_total = 0
    for f in files:
        type_counter[_get_file_type(f)] += 1
        size_total += f.stat().st_size

    tree = Tree(f"[bold]{source}[/bold] ({len(files)} files, {_fmt_bytes(size_total)})")
    for cat, count in type_counter.most_common():
        tree.add(f"[cyan]{cat}[/cyan]: {count} files")
    console.print(tree)


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def display_result(result: OrganizeResult, dry_run: bool) -> None:
    """Display organize results."""
    if dry_run:
        console.print("[yellow]DRY-RUN MODE — no files were actually moved[/yellow]\n")

    t = Table(title="File Distribution", show_lines=True)
    t.add_column("Category", style="cyan")
    t.add_column("Count", justify="right", style="green")
    for cat, count in result.by_category.most_common():
        t.add_row(cat, str(count))
    if t.row_count > 0:
        console.print(t)

    console.print(
        Panel(
            f"[green]Moved: {result.total_moved}[/green]  "
            f"[yellow]Skipped: {result.total_skipped}[/yellow]  "
            f"[red]Errors: {result.total_errors}[/red]  "
            f"Total scanned: {result.total_scanned}",
            title="Summary",
        )
    )

    if result.errors:
        console.print("\n[red]Errors:[/red]")
        for err in result.errors[:20]:
            console.print(f"  • {err}")
        if len(result.errors) > 20:
            console.print(f"  ... and {len(result.errors) - 20} more")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="1.0.0", prog_name="file_organizer")
def cli() -> None:
    """File Organizer — sort files by type, date, size, or custom rules."""


@cli.command()
@click.argument("source", type=click.Path(exists=True))
@click.option("--target", type=click.Path(), default="", help="Target directory (default: same as source).")
@click.option("--by-type", "mode", flag_value="type", help="Organize by file type.")
@click.option("--by-date", "mode", flag_value="date", help="Organize by modification date.")
@click.option("--by-size", "mode", flag_value="size", help="Organize by file size.")
@click.option("--config", type=click.Path(exists=True), help="JSON file with custom rules.")
@click.option("--recursive/--no-recursive", default=False, help="Process subdirectories.")
@click.option("--duplicate", type=click.Choice(["rename", "skip", "overwrite"]), default="rename", help="How to handle duplicates.")
@click.option("--dry-run", is_flag=True, help="Preview without moving files.")
@click.option("--log", type=click.Path(), default="", help="Save operation log for undo.")
@click.option("--date-format", default="%Y/%m", help="Date folder format (strftime).")
@click.option("--include-hidden", is_flag=True, help="Include hidden files.")
def organize(
    source: str,
    target: str,
    mode: str | None,
    config: str | None,
    recursive: bool,
    duplicate: str,
    dry_run: bool,
    log: str,
    date_format: str,
    include_hidden: bool,
) -> None:
    """Organize files in a directory."""
    source_path = Path(source).resolve()
    target_path = Path(target).resolve() if target else source_path

    if not mode and not config:
        console.print("[red]Error:[/red] Provide --by-type, --by-date, --by-size, or --config")
        sys.exit(1)

    if config:
        with open(config, "r", encoding="utf-8") as fh:
            rules = json.load(fh)
        items = rules if isinstance(rules, list) else rules.get("rules", [])
        result = organize_with_rules(
            source_path, target_path, items,
            recursive=recursive, duplicate=duplicate,
            dry_run=dry_run, exclude_hidden=not include_hidden,
        )
    elif mode == "type":
        result = organize_by_type(source_path, target_path, recursive=recursive, duplicate=duplicate, dry_run=dry_run, exclude_hidden=not include_hidden)
    elif mode == "date":
        result = organize_by_date(source_path, target_path, date_format=date_format, recursive=recursive, duplicate=duplicate, dry_run=dry_run, exclude_hidden=not include_hidden)
    elif mode == "size":
        result = organize_by_size(source_path, target_path, recursive=recursive, duplicate=duplicate, dry_run=dry_run, exclude_hidden=not include_hidden)
    else:
        console.print("[red]Error:[/red] Unknown mode")
        sys.exit(1)

    display_result(result, dry_run)

    # Save log for undo
    if result.actions:
        log_path = Path(log) if log else Path(f".file_organizer_log_{int(time.time())}.json")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump([asdict(a) for a in result.actions], fh, indent=2)
        console.print(f"\n[dim]Log saved: {log_path} (use 'undo' to reverse)[/dim]")


@cli.command()
@click.argument("log-file", type=click.Path(exists=True))
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def undo(log_file: str, yes: bool) -> None:
    """Undo a previous organize operation from its log file."""
    log_path = Path(log_file)
    with open(log_path, "r", encoding="utf-8") as fh:
        actions = json.load(fh)

    console.print(f"Found [bold]{len(actions)}[/bold] file operations to undo.")
    if not yes:
        if not click.confirm("Proceed with undo?"):
            console.print("Cancelled.")
            return

    count = undo_operations(log_path)
    console.print(f"\n[green]✓ Undone {count} operations[/green]")


@cli.command()
@click.argument("source", type=click.Path(exists=True))
def scan(source: str) -> None:
    """Scan a directory and show file type distribution."""
    scan_directory(Path(source))


@cli.command()
def sample_config() -> None:
    """Print a sample custom rules config file."""
    sample = {
        "rules": [
            {
                "extensions": [".jpg", ".png", ".gif", ".webp"],
                "folder": "Photos",
            },
            {
                "extensions": [".mp4", ".mkv", ".avi", ".mov"],
                "name_contains": ["screenshot", "screen_recording"],
                "folder": "Screen Recordings",
            },
            {
                "extensions": [".pdf"],
                "folder": "PDFs",
            },
            {
                "extensions": [".zip", ".tar", ".gz", ".rar", ".7z"],
                "folder": "Archives",
            },
            {
                "name_contains": ["invoice", "receipt"],
                "folder": "Finance",
            },
            {
                "max_size": 102400,
                "folder": "Small Files",
            },
        ]
    }
    console.print_json(json.dumps(sample, indent=2))


if __name__ == "__main__":
    cli()
