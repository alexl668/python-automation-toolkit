#!/usr/bin/env python3
"""
Sitemap Crawler — Parse XML sitemaps, check URL status, and find broken links.

Handles standard sitemaps, sitemap index files, and news/video/image sitemaps.
Checks HTTP status codes and reports broken URLs with rich output.

Usage:
    python sitemap_crawler.py check https://example.com/sitemap.xml
    python sitemap_crawler.py check https://example.com/sitemap.xml --output report.csv
    python sitemap_crawler.py discover https://example.com
"""

from __future__ import annotations

import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse

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


@dataclass
class URLStatus:
    """Result of checking a single URL."""
    url: str
    status_code: int = 0
    redirect_url: str = ""
    error: str = ""
    response_time_ms: int = 0


@dataclass
class SitemapResult:
    """Full sitemap crawl result."""
    source: str
    total_urls: int = 0
    urls_checked: int = 0
    ok: int = 0
    redirects: int = 0
    broken: int = 0
    errors: int = 0
    results: list = field(default_factory=list)


def fetch_xml(url: str, timeout: int = 30) -> BeautifulSoup:
    """Fetch and parse an XML sitemap."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    # Handle both XML and text content types
    content_type = resp.headers.get("content-type", "")
    if "xml" not in content_type and "text" not in content_type:
        raise ValueError(f"Unexpected content type: {content_type}")
    return BeautifulSoup(resp.content, "lxml-xml")


def parse_sitemap_urls(soup: BeautifulSoup) -> list[dict]:
    """Parse URLs from a sitemap, extracting loc, lastmod, changefreq, priority."""
    urls = []
    for url_tag in soup.find_all("url"):
        loc = url_tag.find("loc")
        if not loc or not loc.string:
            continue
        entry = {"url": loc.string.strip()}
        lastmod = url_tag.find("lastmod")
        if lastmod and lastmod.string:
            entry["lastmod"] = lastmod.string.strip()
        changefreq = url_tag.find("changefreq")
        if changefreq and changefreq.string:
            entry["changefreq"] = changefreq.string.strip()
        priority = url_tag.find("priority")
        if priority and priority.string:
            entry["priority"] = priority.string.strip()
        urls.append(entry)
    return urls


def parse_sitemap_index(soup: BeautifulSoup) -> list[str]:
    """Extract child sitemap URLs from a sitemap index."""
    sitemaps = []
    for sitemap_tag in soup.find_all("sitemap"):
        loc = sitemap_tag.find("loc")
        if loc and loc.string:
            sitemaps.append(loc.string.strip())
    return sitemaps


def is_sitemap_index(soup: BeautifulSoup) -> bool:
    """Check if the parsed XML is a sitemap index."""
    return soup.find("sitemapindex") is not None


def collect_all_urls(sitemap_url: str, max_sitemaps: int = 50) -> list[dict]:
    """Recursively resolve a sitemap (or sitemap index) into a flat list of URL entries."""
    all_urls: list[dict] = []
    to_process = [sitemap_url]
    processed = set()

    while to_process and len(processed) < max_sitemaps:
        url = to_process.pop(0)
        if url in processed:
            continue
        processed.add(url)

        try:
            soup = fetch_xml(url)
        except Exception as exc:
            console.print(f"[yellow]Warning:[/] Could not fetch {url}: {exc}")
            continue

        if is_sitemap_index(soup):
            child_sitemaps = parse_sitemap_index(soup)
            to_process.extend(child_sitemaps)
            console.print(f"  [dim]Sitemap index:[/] {url} → {len(child_sitemaps)} child sitemaps")
        else:
            urls = parse_sitemap_urls(soup)
            all_urls.extend(urls)
            console.print(f"  [dim]Sitemap:[/] {url} → {len(urls)} URLs")

    return all_urls


def check_url(url: str, timeout: int = 10) -> URLStatus:
    """Check the HTTP status of a single URL."""
    result = URLStatus(url=url)
    try:
        start = time.monotonic()
        resp = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        elapsed = int((time.monotonic() - start) * 1000)
        result.status_code = resp.status_code
        result.response_time_ms = elapsed
        if resp.url != url:
            result.redirect_url = resp.url
    except requests.Timeout:
        result.error = "Timeout"
    except requests.ConnectionError:
        result.error = "Connection error"
    except requests.RequestException as exc:
        result.error = str(exc)[:100]
    return result


def discover_sitemaps(domain: str) -> list[str]:
    """Try common sitemap locations for a domain."""
    parsed = urlparse(domain)
    if not parsed.scheme:
        domain = f"https://{domain}"
        parsed = urlparse(domain)

    base = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap-index.xml",
        f"{base}/sitemaps.xml",
        f"{base}/sitemap1.xml",
        f"{base}/post-sitemap.xml",
        f"{base}/page-sitemap.xml",
    ]

    # Also try robots.txt
    try:
        resp = requests.get(f"{base}/robots.txt", headers=HEADERS, timeout=10)
        if resp.ok:
            for line in resp.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sm_url = line.split(":", 1)[1].strip()
                    if sm_url not in candidates:
                        candidates.insert(0, sm_url)
    except requests.RequestException:
        pass

    found = []
    for url in candidates:
        try:
            resp = requests.head(url, headers=HEADERS, timeout=8, allow_redirects=True)
            if resp.status_code == 200:
                found.append(url)
        except requests.RequestException:
            continue
    return found


def status_icon(code: int, error: str) -> str:
    """Return a colored status indicator."""
    if error:
        return "[bold red]✗ ERR[/]"
    if 200 <= code < 300:
        return "[green]✓[/]"
    if 300 <= code < 400:
        return "[yellow]→[/]"
    if code == 404:
        return "[bold red]✗ 404[/]"
    if code >= 500:
        return "[bold red]✗ {code}[/]"
    return f"[yellow]{code}[/]"


# ── CLI ─────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Parse XML sitemaps, check URLs, and find broken links."""


@cli.command()
@click.argument("sitemap_url")
@click.option("--output", "-o", default=None, help="Export report to CSV file.")
@click.option("--workers", "-w", default=10, type=int, help="Concurrent workers for status checks.")
@click.option("--timeout", "-t", default=10, type=int, help="Request timeout in seconds.")
@click.option("--max-urls", default=0, type=int, help="Max URLs to check (0 = all).")
def check(sitemap_url: str, output: Optional[str], workers: int, timeout: int, max_urls: int):
    """Check all URLs in a sitemap for HTTP status."""
    console.print(f"[cyan]Fetching sitemap:[/] {sitemap_url}")
    all_urls = collect_all_urls(sitemap_url)
    if not all_urls:
        console.print("[red]No URLs found in sitemap.[/]")
        raise SystemExit(1)

    urls_to_check = all_urls[:max_urls] if max_urls else all_urls
    console.print(f"[cyan]Checking {len(urls_to_check)} URLs[/] with {workers} workers…\n")

    results: list[URLStatus] = []
    result = SitemapResult(source=sitemap_url, total_urls=len(all_urls))

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Checking URLs…", total=len(urls_to_check))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(check_url, entry["url"], timeout): entry["url"]
                for entry in urls_to_check
            }
            for future in as_completed(future_map):
                url_status = future.result()
                results.append(url_status)

                if url_status.error:
                    result.errors += 1
                elif 200 <= url_status.status_code < 300:
                    result.ok += 1
                elif 300 <= url_status.status_code < 400:
                    result.redirects += 1
                else:
                    result.broken += 1

                result.urls_checked += 1
                progress.advance(task)

    result.results = results

    # Summary
    summary = Table(title="Sitemap Check Summary", show_lines=True)
    summary.add_column("Metric", style="cyan")
    summary.add_column("Count", justify="right")
    summary.add_row("Total URLs in sitemap", str(result.total_urls))
    summary.add_row("URLs checked", str(result.urls_checked))
    summary.add_row("OK (2xx)", f"[green]{result.ok}[/]")
    summary.add_row("Redirects (3xx)", f"[yellow]{result.redirects}[/]")
    summary.add_row("Broken (4xx/5xx)", f"[bold red]{result.broken}[/]")
    summary.add_row("Errors", f"[red]{result.errors}[/]")
    console.print(summary)

    # Broken URLs detail
    broken = [r for r in results if r.error or r.status_code >= 400]
    if broken:
        console.print()
        table = Table(title=f"Broken URLs ({len(broken)})", show_lines=True)
        table.add_column("Status", width=10)
        table.add_column("URL", max_width=70)
        table.add_column("Redirect To", max_width=40)
        table.add_column("Time", justify="right")
        for r in sorted(broken, key=lambda x: x.status_code):
            icon = status_icon(r.status_code, r.error)
            status = r.error or str(r.status_code)
            table.add_row(icon, r.url, r.redirect_url or "—", f"{r.response_time_ms}ms")
        console.print(table)
    else:
        console.print("\n[bold green]All URLs are healthy! ✓[/]")

    # Export
    if output:
        with open(output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["url", "status_code", "redirect_url", "error", "response_time_ms"])
            writer.writeheader()
            for r in sorted(results, key=lambda x: x.status_code):
                writer.writerow(asdict(r))
        console.print(f"\n[green]Report saved to {output}[/]")


@cli.command()
@click.argument("domain")
def discover(domain: str):
    """Discover sitemaps for a domain (checks common paths + robots.txt)."""
    console.print(f"[cyan]Discovering sitemaps for[/] {domain}…\n")
    sitemaps = discover_sitemaps(domain)
    if not sitemaps:
        console.print("[yellow]No sitemaps found.[/]")
        console.print("[dim]Tried common paths (/sitemap.xml, etc.) and robots.txt[/]")
        raise SystemExit(0)

    table = Table(title=f"Sitemaps for {domain}", show_lines=True)
    table.add_column("#", style="dim")
    table.add_column("Sitemap URL", style="cyan")
    for i, sm in enumerate(sitemaps, 1):
        table.add_row(str(i), sm)
    console.print(table)
    console.print(f"\n[green]Found {len(sitemaps)} sitemap(s).[/]")
    console.print("[dim]Run `check <sitemap_url>` to verify all URLs.[/]")


if __name__ == "__main__":
    cli()
