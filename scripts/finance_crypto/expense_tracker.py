#!/usr/bin/env python3
"""CLI Expense Tracker with SQLite storage, Rich display, and CSV export.

Usage:
    python expense_tracker.py add --amount 25.50 --category food --note "Lunch"
    python expense_tracker.py report --period monthly
    python expense_tracker.py export --output expenses.csv
"""

import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

DB_PATH = Path(__file__).parent / "expenses.db"
console = Console()


def get_db() -> sqlite3.Connection:
    """Return a database connection, creating the table if needed."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            note TEXT DEFAULT '',
            date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.commit()
    return conn


def parse_date(date_str: str) -> str:
    """Parse a date string; defaults to today if empty."""
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        console.print(f"[red]Invalid date format: {date_str}. Use YYYY-MM-DD.[/red]")
        raise SystemExit(1)


@click.group()
def cli():
    """💰 CLI Expense Tracker — track, report, and export expenses."""


@cli.command()
@click.option("--amount", "-a", required=True, type=float, help="Expense amount.")
@click.option("--category", "-c", required=True, help="Expense category (e.g. food, transport).")
@click.option("--note", "-n", default="", help="Optional note.")
@click.option("--date", "-d", default="", help="Date (YYYY-MM-DD). Defaults to today.")
def add(amount: float, category: str, note: str, date: str):
    """Add a new expense."""
    date = parse_date(date)
    conn = get_db()
    conn.execute(
        "INSERT INTO expenses (amount, category, note, date) VALUES (?, ?, ?, ?)",
        (amount, category.lower(), note, date),
    )
    conn.commit()
    console.print(f"[green]✓[/green] Added [cyan]{category}[/cyan] expense: [bold]${amount:.2f}[/bold] on {date}")


@cli.command()
@click.option("--period", "-p", type=click.Choice(["weekly", "monthly", "all"]), default="monthly", help="Report period.")
@click.option("--category", "-c", default=None, help="Filter by category.")
def report(period: str, category: str):
    """Show expense report with Rich table."""
    conn = get_db()
    today = datetime.now()

    if period == "weekly":
        start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "monthly":
        start = today.replace(day=1).strftime("%Y-%m-%d")
    else:
        start = "1970-01-01"

    query = "SELECT id, date, category, amount, note FROM expenses WHERE date >= ?"
    params: list = [start]
    if category:
        query += " AND category = ?"
        params.append(category.lower())
    query += " ORDER BY date DESC, id DESC"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        console.print("[yellow]No expenses found for the selected period.[/yellow]")
        return

    table = Table(title=f"Expense Report ({period})")
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Date", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Amount", justify="right", style="green")
    table.add_column("Note", style="white")

    total = 0.0
    for row in rows:
        total += row[3]
        table.add_row(str(row[0]), row[1], row[2], f"${row[3]:.2f}", row[4] or "")

    table.add_section()
    table.add_row("", "", "[bold]TOTAL[/bold]", f"[bold]${total:.2f}[/bold]", "")

    # Category breakdown
    cat_query = "SELECT category, SUM(amount) FROM expenses WHERE date >= ? GROUP BY category ORDER BY SUM(amount) DESC"
    if category:
        cat_query = "SELECT category, SUM(amount) FROM expenses WHERE date >= ? AND category = ? GROUP BY category"
        cat_rows = conn.execute(cat_query, params).fetchall()
    else:
        cat_rows = conn.execute(cat_query, [start]).fetchall()

    console.print(table)
    if cat_rows:
        cat_table = Table(title="By Category")
        cat_table.add_column("Category", style="magenta")
        cat_table.add_column("Total", justify="right", style="green")
        cat_table.add_column("%", justify="right", style="yellow")
        for cat, amt in cat_rows:
            pct = (amt / total * 100) if total else 0
            cat_table.add_row(cat, f"${amt:.2f}", f"{pct:.1f}%")
        console.print(cat_table)


@cli.command("list")
@click.option("--limit", "-l", default=20, help="Number of recent expenses to show.")
@click.option("--category", "-c", default=None, help="Filter by category.")
def list_expenses(limit: int, category: str):
    """List recent expenses."""
    conn = get_db()
    query = "SELECT id, date, category, amount, note FROM expenses"
    params: list = []
    if category:
        query += " WHERE category = ?"
        params.append(category.lower())
    query += " ORDER BY date DESC, id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        console.print("[yellow]No expenses found.[/yellow]")
        return

    table = Table(title="Recent Expenses")
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Date", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Amount", justify="right", style="green")
    table.add_column("Note", style="white")
    for row in rows:
        table.add_row(str(row[0]), row[1], row[2], f"${row[3]:.2f}", row[4] or "")
    console.print(table)


@cli.command()
@click.option("--output", "-o", default="expenses.csv", help="Output CSV file path.")
@click.option("--period", "-p", type=click.Choice(["weekly", "monthly", "all"]), default="all", help="Export period.")
def export(output: str, period: str):
    """Export expenses to CSV."""
    conn = get_db()
    today = datetime.now()

    if period == "weekly":
        start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "monthly":
        start = today.replace(day=1).strftime("%Y-%m-%d")
    else:
        start = "1970-01-01"

    rows = conn.execute(
        "SELECT id, date, category, amount, note FROM expenses WHERE date >= ? ORDER BY date",
        [start],
    ).fetchall()

    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "Date", "Category", "Amount", "Note"])
        writer.writerows(rows)

    console.print(f"[green]✓[/green] Exported {len(rows)} expenses to [cyan]{output}[/cyan]")


@cli.command()
@click.argument("expense_id", type=int)
def delete(expense_id: int):
    """Delete an expense by ID."""
    conn = get_db()
    row = conn.execute("SELECT id, date, category, amount FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if not row:
        console.print(f"[red]Expense #{expense_id} not found.[/red]")
        return
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    console.print(f"[green]✓[/green] Deleted expense #{expense_id}: {row[2]} ${row[3]:.2f} on {row[1]}")


if __name__ == "__main__":
    cli()
