#!/usr/bin/env python3
"""
Contact Finder — Crawl a website to find emails, phone numbers, and social links.

Crawls internal pages up to a configurable depth, extracts contact information
using regex patterns, and deduplicates results.

Usage:
    python contact_finder.py crawl https://example.com
    python contact_finder.py crawl https://example.com --depth 3 --output contacts.json
    python contact_finder.py crawl https://example.com --export csv --output contacts.csv
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse, urldefrag

import click
import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

console = Console()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ── Patterns ────────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-!#$&'*/=?^`{|}~]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.I,
)

PHONE_RE = re.compile(
    r"(?:(?:\+?\d{1,3}[\s\-]?)?\(?\d{2,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4})"
    r"(?=\s|$|[^\d])",
)

SOCIAL_PATTERNS: dict[str, re.Pattern] = {
    "twitter": re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/[a-zA-Z0-9_]{1,15}", re.I),
    "linkedin": re.compile(r"https?://(?:www\.)?linkedin\.com/(?:in|company)/[a-zA-Z0-9\-_%]+", re.I),
    "facebook": re.compile(r"https?://(?:www\.)?facebook\.com/[a-zA-Z0-9.\-_%]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[a-zA-Z0-9._]+", re.I),
    "github": re.compile(r"https?://(?:www\.)?github\.com/[a-zA-Z0-9\-]+", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/(?:@|c/|channel/|user/)[a-zA-Z0-9\-_]+", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@[a-zA-Z0-9._]+", re.I),
}


@dataclass
class ContactResult:
    """All contact info found on a site."""
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    social: dict = field(default_factory=lambda: {k: set() for k in SOCIAL_PATTERNS})
    pages_crawled: int = 0


def is_same_domain(url: str, base: str) -> bool:
    """Check if url belongs to the same domain as base."""
    return urlparse(url).netloc == urlparse(base).netloc


def normalize_url(url: str, base: str) -> Optional[str]:
    """Resolve and normalize a URL, stripping fragments."""
    full = urljoin(base, url)
    defragged, _ = urldefrag(full)
    parsed = urlparse(defragged)
    if parsed.scheme not in ("http", "https"):
        return None
    return defragged


def extract_contacts_from_text(text: str, result: ContactResult) -> None:
    """Extract emails, phones, and social links from raw text."""
    for match in EMAIL_RE.findall(text):
        email = match.lower().strip(".")
        # Filter false positives
        if not email.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js")):
            result.emails.add(email)

    for match in PHONE_RE.findall(text):
        phone = re.sub(r"[^\d+]", "", match)
        if 7 <= len(phone) <= 15:
            result.phones.add(match.strip())

    for platform, pattern in SOCIAL_PATTERNS.items():
        for match in pattern.findall(text):
            clean = match.rstrip("/").split("?")[0]
            result.social[platform].add(clean)


def extract_contacts_from_page(url: str, timeout: int = 15) -> tuple[ContactResult, list[str]]:
    """Fetch a page, extract contacts and return found internal links."""
    result = ContactResult()
    links: list[str] = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return result, links

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type:
        return result, links

    text = resp.text
    extract_contacts_from_text(text, result)

    soup = BeautifulSoup(text, "lxml")

    # Also check mailto: and tel: links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("mailto:"):
            email = href.replace("mailto:", "").split("?")[0].strip()
            if email:
                result.emails.add(email.lower())
        elif href.startswith("tel:"):
            phone = href.replace("tel:", "").strip()
            if phone:
                result.phones.add(phone)

    # Collect internal links
    for a in soup.find_all("a", href=True):
        link = normalize_url(a["href"], url)
        if link and is_same_domain(link, url):
            links.append(link)

    return result, links


def crawl_site(start_url: str, max_depth: int = 2, max_pages: int = 100, delay: float = 0.5) -> ContactResult:
    """Crawl a site up to max_depth and extract all contact info."""
    result = ContactResult()
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(start_url, 0)]

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed} pages"),
        console=console,
    ) as progress:
        task = progress.add_task("Crawling...", total=max_pages)

        while queue and result.pages_crawled < max_pages:
            url, depth = queue.pop(0)
            if url in visited or depth > max_depth:
                continue
            visited.add(url)

            page_result, links = extract_contacts_from_page(url)
            result.emails |= page_result.emails
            result.phones |= page_result.phones
            for platform in SOCIAL_PATTERNS:
                result.social[platform] |= page_result.social[platform]
            result.pages_crawled += 1

            progress.advance(task)
            progress.update(task, description=f"Crawling depth {depth} — {len(result.emails)} emails")

            if depth < max_depth:
                for link in links:
                    if link not in visited:
                        queue.append((link, depth + 1))

            if delay > 0:
                time.sleep(delay)

    return result


def print_results(result: ContactResult, domain: str) -> None:
    """Pretty-print contact results with Rich."""
    # Emails
    if result.emails:
        table = Table(title=f"📧 Emails ({len(result.emails)})", show_lines=True)
        table.add_column("Email", style="cyan")
        for email in sorted(result.emails):
            table.add_row(email)
        console.print(table)
    else:
        console.print("[dim]No emails found.[/]")

    # Phones
    if result.phones:
        table = Table(title=f"📞 Phone Numbers ({len(result.phones)})", show_lines=True)
        table.add_column("Phone", style="green")
        for phone in sorted(result.phones):
            table.add_row(phone)
        console.print(table)
    else:
        console.print("[dim]No phone numbers found.[/]")

    # Social
    any_social = False
    for platform, urls in result.social.items():
        if urls:
            any_social = True
            table = Table(title=f"🔗 {platform.title()} ({len(urls)})", show_lines=True)
            table.add_column("URL", style="blue")
            for u in sorted(urls):
                table.add_row(u)
            console.print(table)
    if not any_social:
        console.print("[dim]No social media links found.[/]")

    console.print(f"\n[dim]Crawled {result.pages_crawled} pages on {domain}[/]")


# ── CLI ─────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Find emails, phone numbers, and social media links on websites."""


@cli.command()
@click.argument("url")
@click.option("--depth", "-d", default=2, type=int, help="Max crawl depth (default: 2).")
@click.option("--max-pages", "-m", default=50, type=int, help="Max pages to crawl (default: 50).")
@click.option("--delay", default=0.5, type=float, help="Delay between requests in seconds.")
@click.option("--output", "-o", default=None, help="Export file path.")
@click.option("--export", "-e", type=click.Choice(["csv", "json"]), default=None, help="Export format.")
def crawl(url: str, depth: int, max_pages: int, delay: float, output: Optional[str], export: Optional[str]):
    """Crawl a website and extract all contact information."""
    domain = urlparse(url).netloc
    console.print(f"[cyan]Crawling[/] {url} (depth={depth}, max_pages={max_pages})")

    result = crawl_site(url, max_depth=depth, max_pages=max_pages, delay=delay)
    print_results(result, domain)

    # Export
    if output or export:
        fmt = export or ("json" if output and output.endswith(".json") else "csv")
        out_path = output or f"contacts_{domain}.json" if fmt == "json" else f"contacts_{domain}.csv"

        if fmt == "json":
            data = {
                "domain": domain,
                "pages_crawled": result.pages_crawled,
                "emails": sorted(result.emails),
                "phones": sorted(result.phones),
                "social": {k: sorted(v) for k, v in result.social.items() if v},
            }
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        else:
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["type", "platform", "value"])
                for email in sorted(result.emails):
                    writer.writerow(["email", "", email])
                for phone in sorted(result.phones):
                    writer.writerow(["phone", "", phone])
                for platform, urls in result.social.items():
                    for u in sorted(urls):
                        writer.writerow(["social", platform, u])

        console.print(f"[green]Exported to {out_path}[/]")


if __name__ == "__main__":
    cli()
