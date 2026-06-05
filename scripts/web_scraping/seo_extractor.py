#!/usr/bin/env python3
"""
SEO Extractor — Analyze on-page SEO signals from any URL.

Extracts title, meta description, heading hierarchy (h1-h6), canonical URL,
Open Graph and Twitter Card tags, and performs basic robots.txt analysis.

Usage:
    python seo_extractor.py analyze https://example.com
    python seo_extractor.py analyze https://example.com --export seo_report.json
    python seo_extractor.py robots https://example.com
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse

import click
import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

console = Console()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


@dataclass
class SEOReport:
    """SEO analysis results for a single URL."""
    url: str
    status_code: int = 0
    title: str = ""
    title_length: int = 0
    meta_description: str = ""
    meta_description_length: int = 0
    canonical: str = ""
    headings: dict = field(default_factory=dict)  # h1 -> [text, ...], h2 -> [...]
    og_tags: dict = field(default_factory=dict)
    twitter_tags: dict = field(default_factory=dict)
    robots_meta: str = ""
    issues: list = field(default_factory=list)


def fetch(url: str, timeout: int = 15) -> requests.Response:
    """Fetch a URL with standard headers."""
    return requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)


def extract_seo(url: str) -> SEOReport:
    """Extract all on-page SEO signals from a URL."""
    report = SEOReport(url=url)
    try:
        resp = fetch(url)
        report.status_code = resp.status_code
    except requests.RequestException as exc:
        report.issues.append(f"Fetch failed: {exc}")
        return report

    soup = BeautifulSoup(resp.text, "lxml")

    # Title
    if soup.title and soup.title.string:
        report.title = soup.title.string.strip()
    report.title_length = len(report.title)
    if not report.title:
        report.issues.append("Missing <title> tag")
    elif report.title_length > 60:
        report.issues.append(f"Title too long ({report.title_length} chars, recommended ≤60)")
    elif report.title_length < 30:
        report.issues.append(f"Title short ({report.title_length} chars, aim for 30-60)")

    # Meta description
    meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta_desc and meta_desc.get("content"):
        report.meta_description = meta_desc["content"].strip()
    report.meta_description_length = len(report.meta_description)
    if not report.meta_description:
        report.issues.append("Missing meta description")
    elif report.meta_description_length > 160:
        report.issues.append(f"Meta description too long ({report.meta_description_length} chars, recommended ≤160)")
    elif report.meta_description_length < 70:
        report.issues.append(f"Meta description short ({report.meta_description_length} chars, aim for 70-160)")

    # Canonical
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        report.canonical = canonical["href"].strip()
    if not report.canonical:
        report.issues.append("Missing canonical link")

    # Headings
    for level in range(1, 7):
        tag = f"h{level}"
        els = soup.find_all(tag)
        if els:
            report.headings[tag] = [el.get_text(strip=True)[:120] for el in els]
    if "h1" not in report.headings:
        report.issues.append("Missing <h1> tag")
    elif len(report.headings.get("h1", [])) > 1:
        report.issues.append(f"Multiple H1 tags ({len(report.headings['h1'])})")

    # Open Graph
    for meta in soup.find_all("meta", property=re.compile(r"^og:", re.I)):
        key = meta.get("property", "")
        val = meta.get("content", "")
        if key and val:
            report.og_tags[key] = val
    if not report.og_tags:
        report.issues.append("No Open Graph tags found")
    else:
        for required in ("og:title", "og:description", "og:image"):
            if required not in report.og_tags:
                report.issues.append(f"Missing {required}")

    # Twitter Card
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"^twitter:", re.I)}):
        key = meta.get("name", "")
        val = meta.get("content", "")
        if key and val:
            report.twitter_tags[key] = val

    # Robots meta
    robots_meta = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    if robots_meta and robots_meta.get("content"):
        report.robots_meta = robots_meta["content"]
        if "noindex" in report.robots_meta.lower():
            report.issues.append("Page is set to noindex")

    return report


def analyze_robots_txt(base_url: str) -> dict:
    """Fetch and parse robots.txt from a domain."""
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    result = {"url": robots_url, "content": "", "sitemaps": [], "disallow": [], "allow": [], "issues": []}
    try:
        resp = fetch(robots_url)
        if resp.status_code == 404:
            result["issues"].append("robots.txt not found (404)")
            return result
        result["content"] = resp.text
        current_ua = "*"
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("user-agent:"):
                current_ua = line.split(":", 1)[1].strip()
            elif line.lower().startswith("sitemap:"):
                result["sitemaps"].append(line.split(":", 1)[1].strip())
            elif line.lower().startswith("disallow:") and current_ua == "*":
                path = line.split(":", 1)[1].strip()
                if path:
                    result["disallow"].append(path)
            elif line.lower().startswith("allow:") and current_ua == "*":
                result["allow"].append(line.split(":", 1)[1].strip())
    except requests.RequestException as exc:
        result["issues"].append(f"Could not fetch robots.txt: {exc}")
    return result


def issue_color(issue: str) -> str:
    """Return a rich color tag based on issue severity."""
    if any(w in issue.lower() for w in ("missing", "noindex", "not found", "fail")):
        return f"[bold red]{issue}[/]"
    return f"[yellow]{issue}[/]"


# ── CLI ─────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Analyze on-page SEO signals from any URL."""


@cli.command()
@click.argument("url")
@click.option("--export", "-e", default=None, help="Export full report to JSON file.")
def analyze(url: str, export: Optional[str]):
    """Run a full SEO analysis on URL."""
    console.print(f"[cyan]Analyzing[/] {url} …")
    report = extract_seo(url)

    # Status
    status_color = "green" if 200 <= report.status_code < 400 else "red"
    console.print(f"  Status: [{status_color}]{report.status_code}[/]")

    # Title
    title_assess = ""
    if report.title_length > 60:
        title_assess = " [yellow]⚠ too long[/]"
    elif report.title_length < 30:
        title_assess = " [yellow]⚠ short[/]"
    else:
        title_assess = " [green]✓[/]"
    console.print(Panel(
        f"[bold]{report.title}[/]\n[dim]{report.title_length} characters[/]{title_assess}",
        title="Title",
    ))

    # Meta description
    desc_assess = ""
    if report.meta_description_length > 160:
        desc_assess = " [yellow]⚠ too long[/]"
    elif report.meta_description_length < 70:
        desc_assess = " [yellow]⚠ short[/]"
    else:
        desc_assess = " [green]✓[/]"
    console.print(Panel(
        f"{report.meta_description}\n[dim]{report.meta_description_length} characters[/]{desc_assess}",
        title="Meta Description",
    ))

    # Canonical
    console.print(f"  Canonical: [cyan]{report.canonical or '[red]None[/]'}[/]")

    # Headings tree
    tree = Tree("[bold]Headings[/]")
    for tag in sorted(report.headings.keys()):
        branch = tree.add(f"[cyan]{tag}[/] ({len(report.headings[tag])})")
        for text in report.headings[tag][:10]:
            branch.add(text)
    console.print(tree)

    # OG Tags
    if report.og_tags:
        og_table = Table(title="Open Graph Tags", show_lines=True)
        og_table.add_column("Property", style="cyan")
        og_table.add_column("Content", max_width=60)
        for k, v in report.og_tags.items():
            og_table.add_row(k, v[:120])
        console.print(og_table)
    else:
        console.print("  [red]No Open Graph tags found[/]")

    # Twitter Card
    if report.twitter_tags:
        tw_table = Table(title="Twitter Card Tags")
        tw_table.add_column("Name", style="cyan")
        tw_table.add_column("Content", max_width=60)
        for k, v in report.twitter_tags.items():
            tw_table.add_row(k, v[:120])
        console.print(tw_table)

    # Robots meta
    if report.robots_meta:
        console.print(f"  Robots meta: [yellow]{report.robots_meta}[/]")

    # Issues
    if report.issues:
        console.print()
        issues_table = Table(title="⚠ Issues Found", show_lines=True)
        issues_table.add_column("#", style="dim")
        issues_table.add_column("Issue")
        for i, issue in enumerate(report.issues, 1):
            issues_table.add_row(str(i), issue_color(issue))
        console.print(issues_table)
    else:
        console.print("\n[bold green]No issues found! ✓[/]")

    # Export
    if export:
        data = asdict(report)
        with open(export, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        console.print(f"\n[green]Report exported to {export}[/]")


@cli.command()
@click.argument("url")
def robots(url: str):
    """Analyze robots.txt for a domain."""
    result = analyze_robots_txt(url)

    if result["issues"]:
        for issue in result["issues"]:
            console.print(f"[red]• {issue}[/]")
    else:
        console.print(f"[green]robots.txt found at[/] {result['url']}")
        if result["sitemaps"]:
            console.print("\n[bold]Sitemaps:[/]")
            for sm in result["sitemaps"]:
                console.print(f"  • [cyan]{sm}[/]")
        if result["disallow"]:
            console.print(f"\n[bold]Disallowed paths ({len(result['disallow'])}):[/]")
            for path in result["disallow"][:20]:
                console.print(f"  • {path}")
            if len(result["disallow"]) > 20:
                console.print(f"  [dim]… and {len(result['disallow']) - 20} more[/]")


if __name__ == "__main__":
    cli()
