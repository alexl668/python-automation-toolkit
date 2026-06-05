#!/usr/bin/env python3
"""
Price Monitor — Track product prices over time.

Extracts prices from e-commerce product pages using multiple CSS selector
strategies, logs history to CSV, and optionally runs on a polling schedule.

Usage:
    python price_monitor.py track https://example.com/product --interval 60
    python price_monitor.py history --csv price_history.csv
    python price_monitor.py check https://example.com/product
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ── Common price selector patterns ──────────────────────────────────────────
SELECTORS = [
    # Structured data (JSON-LD)
    {"type": "json-ld", "field": "price"},
    # Common e-commerce selectors
    {"type": "css", "selector": "[data-price]", "attr": "data-price"},
    {"type": "css", "selector": "[itemprop='price']", "attr": "content"},
    {"type": "css", "selector": ".price", "attr": None},
    {"type": "css", "selector": ".product-price", "attr": None},
    {"type": "css", "selector": "#priceblock_ourprice", "attr": None},
    {"type": "css", "selector": "#priceblock_dealprice", "attr": None},
    {"type": "css", "selector": ".sale-price", "attr": None},
    {"type": "css", "selector": ".current-price", "attr": None},
    {"type": "css", "selector": ".offer-price", "attr": None},
    {"type": "css", "selector": "[data-testid='price']", "attr": None},
    {"type": "css", "selector": ".a-price .a-offscreen", "attr": None},
    {"type": "css", "selector": ".priceAmount", "attr": None},
    {"type": "css", "selector": ".price-current", "attr": None},
    # OG meta
    {"type": "css", "selector": "meta[property='product:price:amount']", "attr": "content"},
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_page(url: str, timeout: int = 15) -> BeautifulSoup:
    """Fetch a URL and return a BeautifulSoup object."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")


def extract_price_from_jsonld(soup: BeautifulSoup) -> Optional[float]:
    """Try to extract price from JSON-LD structured data."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Handle both single objects and @graph arrays
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") in (
                    "Product", "Offer", "AggregateOffer",
                ):
                    offers = item.get("offers", item)
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price_val = offers.get("price") or offers.get("lowPrice")
                    if price_val is not None:
                        return float(str(price_val).replace(",", ""))
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
            continue
    return None


def extract_price_text(text: str) -> Optional[float]:
    """Parse a numeric price from arbitrary text like '$29.99' or '¥199'."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text.strip())
    # Handle European format 1.234,56
    if cleaned.count(",") == 1 and cleaned.count(".") >= 1:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(",") == 1 and cleaned.count(".") == 0:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_price(soup: BeautifulSoup) -> Optional[float]:
    """Try every selector strategy until a price is found."""
    # 1) JSON-LD
    price = extract_price_from_jsonld(soup)
    if price:
        return price

    # 2) CSS selectors
    for sel in SELECTORS:
        if sel["type"] != "css":
            continue
        el = soup.select_one(sel["selector"])
        if not el:
            continue
        value = el.get(sel["attr"]) if sel["attr"] else el.get_text(strip=True)
        price = extract_price_text(value)
        if price:
            return price
    return None


def extract_title(soup: BeautifulSoup) -> str:
    """Extract a human-readable page title."""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return "(unknown)"


def save_to_csv(csv_path: str, url: str, title: str, price: Optional[float], currency: str = "") -> None:
    """Append a price observation to a CSV file."""
    path = Path(csv_path)
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "url", "title", "price", "currency"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            url,
            title,
            price if price is not None else "",
            currency,
        ])


# ── CLI ─────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Track product prices from e-commerce pages."""


@cli.command()
@click.argument("url")
@click.option("--selector", "-s", default=None, help="Custom CSS selector to override auto-detection.")
@click.option("--csv", "csv_file", default="price_history.csv", help="CSV file to append results.")
def check(url: str, selector: Optional[str], csv_file: str):
    """Check the current price of a product at URL."""
    try:
        soup = fetch_page(url)
    except requests.RequestException as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        raise SystemExit(1)

    title = extract_title(soup)
    price: Optional[float] = None

    if selector:
        el = soup.select_one(selector)
        if el:
            price = extract_price_text(el.get_text(strip=True))
    else:
        price = extract_price(soup)

    table = Table(title="Price Check", show_lines=True)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("URL", url)
    table.add_row("Title", title)
    table.add_row("Price", f"{price}" if price is not None else "[red]Not found[/]")
    console.print(table)

    save_to_csv(csv_file, url, title, price)
    console.print(f"[dim]Saved to {csv_file}[/]")


@cli.command()
@click.argument("url")
@click.option("--interval", "-i", default=300, type=int, help="Poll interval in seconds (default: 300).")
@click.option("--csv", "csv_file", default="price_history.csv", help="CSV file for history.")
@click.option("--selector", "-s", default=None, help="Custom CSS selector.")
@click.option("--runs", "-n", default=0, type=int, help="Number of checks (0 = infinite).")
def track(url: str, interval: int, csv_file: str, selector: Optional[str], runs: int):
    """Continuously track a product price on a schedule."""
    console.print(Panel(f"Tracking [cyan]{url}[/] every {interval}s", title="Price Monitor"))
    count = 0
    try:
        while True:
            count += 1
            try:
                soup = fetch_page(url)
                title = extract_title(soup)
                price = None
                if selector:
                    el = soup.select_one(selector)
                    if el:
                        price = extract_price_text(el.get_text(strip=True))
                else:
                    price = extract_price(soup)
                save_to_csv(csv_file, url, title, price)
                ts = datetime.now().strftime("%H:%M:%S")
                price_str = f"${price:.2f}" if price else "N/A"
                console.print(f"[{ts}] #{count}  {title}  →  [green]{price_str}[/]")
            except requests.RequestException as exc:
                console.print(f"[bold red]Fetch error:[/] {exc}")
            except Exception as exc:
                console.print(f"[bold red]Error:[/] {exc}")

            if runs and count >= runs:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/]")


@cli.command("history")
@click.option("--csv", "csv_file", default="price_history.csv", help="CSV file to read.")
@click.option("--last", "-n", default=20, type=int, help="Show last N entries.")
def show_history(csv_file: str, last: int):
    """Display price history from a CSV file."""
    path = Path(csv_file)
    if not path.exists():
        console.print(f"[red]File not found:[/] {csv_file}")
        raise SystemExit(1)

    with open(path, encoding="utf-8") as f:
        reader = list(csv.DictReader(f))

    rows = reader[-last:]
    table = Table(title=f"Price History ({len(reader)} total, showing last {len(rows)})")
    table.add_column("#", style="dim")
    table.add_column("Timestamp", style="cyan")
    table.add_column("Title", max_width=40)
    table.add_column("Price", justify="right", style="green")
    for i, row in enumerate(rows, 1):
        table.add_row(str(i), row.get("timestamp", ""), row.get("title", ""), row.get("price", "N/A"))
    console.print(table)


if __name__ == "__main__":
    cli()
