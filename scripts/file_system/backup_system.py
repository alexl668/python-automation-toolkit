#!/usr/bin/env python3
"""Automated backup system with compression, incremental backups, and rotation policy.

Usage:
    python backup_system.py run --source ~/projects --dest /backups
    python backup_system.py run --source ~/projects --dest /backups --incremental
    python backup_system.py list --dest /backups
    python backup_system.py restore --archive /backups/backup_20240115_120000.tar.gz --dest ~/restore
    python backup_system.py cleanup --dest /backups --keep 5
"""

import json
import os
import shutil
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TransferSpeedColumn
from rich.panel import Panel

console = Console()


def get_timestamp() -> str:
    """Return a timestamp string for archive naming."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def create_archive(source: Path, dest_dir: Path, incremental: bool = False,
                   compression: str = "gz", exclude: Optional[list[str]] = None) -> Path:
    """Create a compressed tar archive of the source directory.

    Args:
        source: Source directory to back up.
        dest_dir: Destination directory for the archive.
        incremental: If True, only include files modified since last backup.
        compression: Compression type (gz, bz2, xz).
        exclude: List of glob patterns to exclude.

    Returns:
        Path to the created archive.
    """
    source = source.expanduser().resolve()
    dest_dir = dest_dir.expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    suffix = f".tar.{compression}"
    ts = get_timestamp()
    mode = "incremental" if incremental else "full"
    archive_name = f"backup_{mode}_{source.name}_{ts}{suffix}"
    archive_path = dest_dir / archive_name

    # Determine which files to include
    files_to_backup: list[Path] = []
    exclude_set = set(exclude or [])
    snapshot_file = dest_dir / f".snapshot_{source.name}.json"
    last_backup_time = 0.0

    if incremental and snapshot_file.exists():
        snapshot = json.loads(snapshot_file.read_text())
        last_backup_time = snapshot.get("timestamp", 0)

    for root, dirs, files in os.walk(source):
        root_path = Path(root)

        # Skip excluded directories
        dirs[:] = [d for d in dirs if not _should_exclude(d, exclude_set)]

        for fname in files:
            if _should_exclude(fname, exclude_set):
                continue
            fpath = root_path / fname
            try:
                if incremental:
                    if fpath.stat().st_mtime <= last_backup_time:
                        continue
                files_to_backup.append(fpath)
            except OSError:
                continue

    if not files_to_backup:
        console.print("[yellow]No new or modified files to back up.[/yellow]")
        return archive_path

    # Create archive
    compression_map = {"gz": "gz", "bz2": "bz2", "xz": "xz", "none": ""}
    comp = compression_map.get(compression, "gz")
    mode_str = f"w:{comp}" if comp else "w"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} files"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Creating archive...", total=len(files_to_backup))

        with tarfile.open(archive_path, mode_str) as tar:
            for fpath in files_to_backup:
                arcname = fpath.relative_to(source.parent)
                try:
                    tar.add(str(fpath), arcname=str(arcname))
                except (OSError, PermissionError) as e:
                    console.print(f"[yellow]⚠ Skipping {fpath}: {e}[/yellow]")
                progress.advance(task)

    # Update snapshot
    snapshot_data = {
        "timestamp": datetime.now().timestamp(),
        "source": str(source),
        "files_count": len(files_to_backup),
        "archive": str(archive_path),
    }
    snapshot_file.write_text(json.dumps(snapshot_data, indent=2))

    return archive_path


def _should_exclude(name: str, patterns: set[str]) -> bool:
    """Check if a filename matches any exclusion pattern."""
    for pat in patterns:
        if pat.startswith("*.") and name.endswith(pat[1:]):
            return True
        if name == pat or name.startswith(pat):
            return True
        if pat in name:
            return True
    return False


def list_backups(dest_dir: Path) -> list[dict]:
    """List all backup archives in the destination directory."""
    dest_dir = dest_dir.expanduser().resolve()
    backups = []
    for f in sorted(dest_dir.glob("backup_*.tar.*"), reverse=True):
        stat = f.stat()
        parts = f.stem.split("_")
        mode = parts[1] if len(parts) > 1 else "unknown"
        backups.append({
            "path": f,
            "name": f.name,
            "size": stat.st_size,
            "mode": mode,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return backups


@click.group()
def cli():
    """💾 Backup System — automated backups with compression and rotation."""


@cli.command()
@click.option("--source", "-s", required=True, type=click.Path(exists=True), help="Source directory to back up.")
@click.option("--dest", "-d", required=True, type=click.Path(), help="Destination directory for archives.")
@click.option("--incremental", is_flag=True, help="Only back up files changed since last backup.")
@click.option("--compression", type=click.Choice(["gz", "bz2", "xz"]), default="gz", help="Compression type.")
@click.option("--exclude", "-e", multiple=True, help="Glob patterns to exclude (e.g. *.pyc __pycache__ node_modules).")
def run(source: str, dest: str, incremental: bool, compression: str, exclude: tuple):
    """Run a backup."""
    src = Path(source).expanduser().resolve()
    dst = Path(dest).expanduser().resolve()

    mode_str = "Incremental" if incremental else "Full"
    console.print(f"[cyan]{mode_str} backup:[/cyan] {src} → {dst}")
    if exclude:
        console.print(f"  Excluding: {', '.join(exclude)}")

    archive = create_archive(src, dst, incremental, compression, list(exclude))

    if archive.exists():
        size = archive.stat().st_size
        console.print(Panel(
            f"[green]✓ Backup complete![/green]\n\n"
            f"  Archive: [cyan]{archive.name}[/cyan]\n"
            f"  Size:    {format_size(size)}\n"
            f"  Mode:    {mode_str}\n"
            f"  Dest:    {dst}",
            title="💾 Backup",
            border_style="green",
        ))
    else:
        console.print("[yellow]No archive was created (no files to back up).[/yellow]")


@cli.command("list")
@click.option("--dest", "-d", required=True, type=click.Path(exists=True), help="Backup directory.")
def list_cmd(dest: str):
    """List available backups."""
    dst = Path(dest)
    backups = list_backups(dst)

    if not backups:
        console.print("[yellow]No backups found.[/yellow]")
        return

    table = Table(title="Available Backups")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Archive Name", style="cyan")
    table.add_column("Mode", style="magenta")
    table.add_column("Size", justify="right")
    table.add_column("Modified", style="dim")

    for i, b in enumerate(backups, 1):
        table.add_row(str(i), b["name"], b["mode"], format_size(b["size"]), b["modified"])

    console.print(table)


@cli.command()
@click.option("--dest", "-d", required=True, type=click.Path(exists=True), help="Backup directory.")
@click.option("--keep", "-k", default=5, help="Number of backups to keep.")
def cleanup(dest: str, keep: int):
    """Remove old backups, keeping the N most recent."""
    dst = Path(dest)
    backups = list_backups(dst)

    if len(backups) <= keep:
        console.print(f"[green]Only {len(backups)} backups exist (keep={keep}). Nothing to clean.[/green]")
        return

    to_remove = backups[keep:]
    console.print(f"[yellow]Removing {len(to_remove)} old backups (keeping {keep})...[/yellow]")

    for b in to_remove:
        b["path"].unlink()
        console.print(f"  [red]✗[/red] {b['name']}")

    console.print(f"[green]✓ Cleanup complete. {len(backups) - len(to_remove)} backups remaining.[/green]")


@cli.command()
@click.option("--archive", "-a", required=True, type=click.Path(exists=True), help="Archive to restore.")
@click.option("--dest", "-d", required=True, type=click.Path(), help="Destination directory for restore.")
@click.option("--dry-run", is_flag=True, help="List files without extracting.")
def restore(archive: str, dest: str, dry_run: bool):
    """Restore a backup archive."""
    arc = Path(archive)
    dst = Path(dest)

    try:
        with tarfile.open(str(arc), "r:*") as tar:
            members = tar.getmembers()
            console.print(f"Archive contains [bold]{len(members)}[/bold] entries.")

            if dry_run:
                table = Table(title="Archive Contents")
                table.add_column("Type")
                table.add_column("Path")
                table.add_column("Size", justify="right")
                for m in members[:100]:
                    ftype = "📁" if m.isdir() else "📄"
                    table.add_row(ftype, m.name, format_size(m.size) if m.isfile() else "")
                if len(members) > 100:
                    table.add_row("...", f"({len(members) - 100} more)", "")
                console.print(table)
                return

            dst.mkdir(parents=True, exist_ok=True)

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Restoring...", total=len(members))
                for member in members:
                    tar.extract(member, path=str(dst))
                    progress.advance(task)

            console.print(f"[green]✓ Restored to: {dst}[/green]")

    except tarfile.TarError as e:
        console.print(f"[red]Error reading archive: {e}[/red]")


@cli.command()
@click.option("--dest", "-d", required=True, type=click.Path(exists=True), help="Backup directory.")
def info(dest: str):
    """Show backup statistics."""
    dst = Path(dest)
    backups = list_backups(dst)

    if not backups:
        console.print("[yellow]No backups found.[/yellow]")
        return

    total_size = sum(b["size"] for b in backups)
    full_count = sum(1 for b in backups if b["mode"] == "full")
    incr_count = sum(1 for b in backups if b["mode"] == "incremental")

    panel = Panel(
        f"[bold]Backup Statistics[/bold]\n\n"
        f"  Total backups: {len(backups)}\n"
        f"  Full backups:  {full_count}\n"
        f"  Incremental:   {incr_count}\n"
        f"  Total size:    {format_size(total_size)}\n"
        f"  Oldest:        {backups[-1]['modified']}\n"
        f"  Newest:        {backups[0]['modified']}",
        title="💾 Info",
        border_style="cyan",
    )
    console.print(panel)


if __name__ == "__main__":
    cli()
