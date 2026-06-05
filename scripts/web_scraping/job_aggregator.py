#!/usr/bin/env python3
"""
Job Aggregator — Scrape job listings from career pages and job boards.

Extracts title, company, location, salary, and link from job listing pages
that follow common HTML patterns (Indeed/LinkedIn/Glassdoor-style layouts).

Usage:
    python job_aggregator.py scrape https://example.com/careers --output jobs.csv
    python job_aggregator.py scrape https://example.com/jobs --format json --output jobs.json
    python job_aggregator.py scrape https://example.com/openings --pages 3
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse

import click
import requests
from bs4 import BeautifulSoup, Tag
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class JobListing:
    """Represents a single job posting."""
    title: str
    company: str = ""
    location: str = ""
    salary: str = ""
    link: str = ""
    snippet: str = ""


# ── Selector strategies ─────────────────────────────────────────────────────
# Each strategy is a dict describing how to find job cards and fields inside them.
JOB_CARD_SELECTORS = [
    # Generic job board card patterns
    {
        "card": ".job_seen_beacon, .jobsearch-SerpJobCard, .job_listing, .job-card, .jobCard, [data-tn-component='organicJob']",
        "title": ".jobTitle a, h2 a, .title a, .jobTitle-color-green, [data-tn-element='jobTitle']",
        "company": ".companyName, .company, .company-name, [data-tn-component='companyName']",
        "location": ".companyLocation, .location, .job-location, .recJobLoc",
        "salary": ".salary-snippet, .salaryText, .salary, .estimated-salary, [data-testid='attribute_snippet_testid']",
        "link_from": "title",
    },
    # Glassdoor-style
    {
        "card": ".JobCard_jobCard__jjf1b, .job-listing, .jl, .react-job-listing",
        "title": ".JobCard_jobTitle__GLyJ1, .jobTitle, .job-title a",
        "company": ".JobCard_employerName__Hp32m, .employer-name, .company",
        "location": ".JobCard_location__N_iYE, .job-location, .location",
        "salary": ".JobCard_salaryEstimate__arV5J, .salary-estimate, .salary",
        "link_from": "title",
    },
    # LinkedIn-style
    {
        "card": ".jobs-search__results-list li, .job-card-container, .jobs-unified-top-card",
        "title": ".job-card-list__title, .artdeco-entity-lockup__title, .job-card-container__link",
        "company": ".job-card-container__primary-description, .artdeco-entity-lockup__subtitle",
        "location": ".job-card-container__metadata-item, .artdeco-entity-lockup__caption",
        "salary": ".job-card-container__metadata-item--salary, .salary",
        "link_from": "title",
    },
    # JSON-LD job postings
    {"card": "json-ld"},
]


def fetch_soup(url: str, timeout: int = 20) -> BeautifulSoup:
    """Fetch a URL and return parsed HTML."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")


def extract_jsonld_jobs(soup: BeautifulSoup, base_url: str) -> list[JobListing]:
    """Extract jobs from JSON-LD JobPosting structured data."""
    jobs: list[JobListing] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            # Handle @graph wrapper
            if len(items) == 1 and isinstance(items[0], dict) and "@graph" in items[0]:
                items = items[0]["@graph"]
            for item in items:
                if not isinstance(item, dict):
                    continue
                jtype = item.get("@type", "")
                if isinstance(jtype, list):
                    jtype = " ".join(jtype)
                if "JobPosting" not in str(jtype):
                    continue
                title = item.get("title", "")
                company = ""
                org = item.get("hiringOrganization", {})
                if isinstance(org, dict):
                    company = org.get("name", "")
                loc = item.get("jobLocation", {})
                if isinstance(loc, dict):
                    addr = loc.get("address", {})
                    if isinstance(addr, dict):
                        parts = [addr.get("addressLocality", ""), addr.get("addressRegion", "")]
                        loc = ", ".join(p for p in parts if p)
                    else:
                        loc = str(addr)
                elif isinstance(loc, list) and loc:
                    loc = loc[0]
                salary_info = item.get("baseSalary", {})
                salary = ""
                if isinstance(salary_info, dict):
                    val = salary_info.get("value", {})
                    if isinstance(val, dict):
                        mn, mx = val.get("minValue"), val.get("maxValue")
                        if mn or mx:
                            salary = f"${mn:,.0f}–${mx:,.0f}" if mx else f"${mn:,.0f}"
                link = item.get("url", "")
                jobs.append(JobListing(
                    title=title, company=str(company), location=str(loc),
                    salary=salary, link=link,
                ))
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
            continue
    return jobs


def _get_text(el: Optional[Tag]) -> str:
    """Safely get stripped text from a BeautifulSoup element."""
    if el is None:
        return ""
    return el.get_text(strip=True)


def _get_link(el: Optional[Tag], base_url: str) -> str:
    """Extract href from element or its first child <a>."""
    if el is None:
        return ""
    a = el if el.name == "a" else el.find("a")
    if a and a.get("href"):
        return urljoin(base_url, a["href"])
    return ""


def extract_jobs_from_html(soup: BeautifulSoup, base_url: str) -> list[JobListing]:
    """Try multiple CSS strategies to extract job listings from HTML."""
    # First try JSON-LD
    jsonld_jobs = extract_jsonld_jobs(soup, base_url)
    if jsonld_jobs:
        return jsonld_jobs

    jobs: list[JobListing] = []
    for strategy in JOB_CARD_SELECTORS:
        if strategy.get("card") == "json-ld":
            continue
        cards = soup.select(strategy["card"])
        if not cards:
            continue
        for card in cards:
            title_el = card.select_one(strategy["title"])
            title = _get_text(title_el)
            if not title:
                continue
            link = _get_link(title_el, base_url) if title_el else ""
            if not link:
                link = _get_link(card, base_url)
            company = _get_text(card.select_one(strategy["company"]))
            location = _get_text(card.select_one(strategy["location"]))
            salary = _get_text(card.select_one(strategy["salary"]))
            jobs.append(JobListing(
                title=title, company=company, location=location,
                salary=salary, link=link,
            ))
        if jobs:
            break  # First strategy that yields results wins
    return jobs


def find_next_page(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    """Try to find a 'next page' link for pagination."""
    for sel in [
        "a.next", "a[rel='next']", ".pagination .next a",
        "a[data-testid='pagination-next']", "li.next a",
        ".pagination-next a", "a:contains('Next')",
    ]:
        el = soup.select_one(sel)
        if el and el.get("href"):
            return urljoin(current_url, el["href"])
    # Fallback: look for numbered pagination
    for a in soup.select("a[href]"):
        text = a.get_text(strip=True).lower()
        if text in ("next", "next ›", "›", "»", "next page"):
            return urljoin(current_url, a["href"])
    return None


# ── CLI ─────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Scrape job listings from career pages and job boards."""


@cli.command()
@click.argument("url")
@click.option("--output", "-o", default="jobs.csv", help="Output file path.")
@click.option("--format", "-f", "fmt", type=click.Choice(["csv", "json"]), default="csv", help="Output format.")
@click.option("--pages", "-p", default=1, type=int, help="Number of pages to scrape (follows pagination).")
@click.option("--delay", "-d", default=2.0, type=float, help="Delay between page requests (seconds).")
def scrape(url: str, output: str, fmt: str, pages: int, delay: float):
    """Scrape job listings from URL."""
    all_jobs: list[JobListing] = []
    current_url = url

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Scraping...", total=pages)

        for page_num in range(1, pages + 1):
            progress.update(task, description=f"Page {page_num}/{pages}")
            try:
                soup = fetch_soup(current_url)
            except requests.RequestException as exc:
                console.print(f"[bold red]Error fetching {current_url}:[/] {exc}")
                break

            jobs = extract_jobs_from_html(soup, current_url)
            if not jobs:
                console.print(f"[yellow]No jobs found on page {page_num}.[/]")
                break

            all_jobs.extend(jobs)
            progress.advance(task)

            if page_num < pages:
                next_url = find_next_page(soup, current_url)
                if not next_url:
                    console.print("[yellow]No next page link found.[/]")
                    break
                current_url = next_url
                time.sleep(delay)

    # Deduplicate by title + company
    seen = set()
    unique_jobs: list[JobListing] = []
    for j in all_jobs:
        key = (j.title.lower().strip(), j.company.lower().strip())
        if key not in seen:
            seen.add(key)
            unique_jobs.append(j)

    if not unique_jobs:
        console.print("[bold red]No job listings found.[/]")
        raise SystemExit(1)

    # Display
    table = Table(title=f"Found {len(unique_jobs)} Jobs", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="cyan", max_width=40)
    table.add_column("Company", max_width=25)
    table.add_column("Location", max_width=20)
    table.add_column("Salary", style="green", max_width=20)
    for i, j in enumerate(unique_jobs, 1):
        table.add_row(str(i), j.title, j.company, j.location, j.salary or "—")
    console.print(table)

    # Save
    if fmt == "json":
        with open(output, "w", encoding="utf-8") as f:
            json.dump([asdict(j) for j in unique_jobs], f, indent=2, ensure_ascii=False)
    else:
        with open(output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["title", "company", "location", "salary", "link", "snippet"])
            writer.writeheader()
            for j in unique_jobs:
                writer.writerow(asdict(j))

    console.print(f"[green]Saved {len(unique_jobs)} jobs to {output}[/]")


if __name__ == "__main__":
    cli()
