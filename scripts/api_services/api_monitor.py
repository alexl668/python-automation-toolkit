#!/usr/bin/env python3
"""
api_monitor.py — API Endpoint Health Monitor

Monitor a list of API endpoints for:
- HTTP status codes
- Response time
- SSL certificate expiry
- Content validation (optional substring/regex check)

Alerts on failures, supports config-driven endpoint lists,
and can run as a one-shot check or continuous monitor.

Usage:
    python api_monitor.py check --config endpoints.json
    python api_monitor.py check --url https://api.example.com/health --url https://other.com/status
    python api_monitor.py watch --config endpoints.json --interval 60
    python api_monitor.py check --url https://example.com --check-ssl
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click
import requests
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class EndpointConfig:
    """Configuration for a single endpoint."""

    url: str
    name: str = ""
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout: float = 10.0
    expected_status: int = 200
    expected_content: str = ""  # substring or regex to match in response body
    content_is_regex: bool = False
    check_ssl: bool = True
    ssl_warn_days: int = 30  # warn if cert expires within N days
    tags: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    """Result of checking one endpoint."""

    name: str
    url: str
    status_code: int = 0
    response_time_ms: float = 0.0
    is_healthy: bool = True
    error: str = ""
    ssl_expiry: str = ""
    ssl_days_remaining: int = -1
    ssl_warning: bool = False
    content_match: bool = True
    timestamp: str = ""


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: Path) -> list[EndpointConfig]:
    """Load endpoints from a JSON config file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    endpoints: list[EndpointConfig] = []
    items = data if isinstance(data, list) else data.get("endpoints", [])
    for item in items:
        endpoints.append(
            EndpointConfig(
                url=item["url"],
                name=item.get("name", ""),
                method=item.get("method", "GET").upper(),
                headers=item.get("headers", {}),
                body=item.get("body", ""),
                timeout=item.get("timeout", 10.0),
                expected_status=item.get("expected_status", 200),
                expected_content=item.get("expected_content", ""),
                content_is_regex=item.get("content_is_regex", False),
                check_ssl=item.get("check_ssl", True),
                ssl_warn_days=item.get("ssl_warn_days", 30),
                tags=item.get("tags", []),
            )
        )
    return endpoints


# ---------------------------------------------------------------------------
# SSL checker
# ---------------------------------------------------------------------------

def check_ssl_expiry(hostname: str, port: int = 443) -> tuple[str, int, bool]:
    """Check SSL certificate expiry for a hostname.

    Returns (expiry_date_str, days_remaining, is_warning).
    """
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                not_after = cert.get("notAfter", "")
                if not_after:
                    # Format: 'Jan  5 12:00:00 2026 GMT'
                    expiry_dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    days = (expiry_dt - now).days
                    is_warning = days < 30
                    return expiry_dt.strftime("%Y-%m-%d"), days, is_warning
        return "", -1, False
    except Exception as exc:
        return f"error: {exc}", -1, True


# ---------------------------------------------------------------------------
# Endpoint checker
# ---------------------------------------------------------------------------

def check_endpoint(ep: EndpointConfig, check_ssl: bool = True) -> CheckResult:
    """Check a single endpoint and return the result."""
    result = CheckResult(
        name=ep.name or ep.url,
        url=ep.url,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    parsed = urlparse(ep.url)

    # HTTP check
    try:
        method = getattr(requests, ep.method.lower(), requests.get)
        kwargs: dict[str, Any] = {
            "headers": ep.headers,
            "timeout": ep.timeout,
            "allow_redirects": True,
        }
        if ep.body and ep.method in ("POST", "PUT", "PATCH"):
            kwargs["data"] = ep.body

        start = time.monotonic()
        resp = method(ep.url, **kwargs)
        elapsed_ms = (time.monotonic() - start) * 1000

        result.status_code = resp.status_code
        result.response_time_ms = round(elapsed_ms, 2)

        # Status check
        if ep.expected_status and resp.status_code != ep.expected_status:
            result.is_healthy = False
            result.error = f"Expected status {ep.expected_status}, got {resp.status_code}"

        # Content check
        if ep.expected_content:
            body = resp.text
            if ep.content_is_regex:
                result.content_match = bool(re.search(ep.expected_content, body))
            else:
                result.content_match = ep.expected_content in body
            if not result.content_match:
                result.is_healthy = False
                result.error = (result.error + " | " if result.error else "") + "Content check failed"

    except requests.Timeout:
        result.is_healthy = False
        result.error = f"Timeout after {ep.timeout}s"
    except requests.ConnectionError as exc:
        result.is_healthy = False
        result.error = f"Connection error: {exc}"
    except Exception as exc:
        result.is_healthy = False
        result.error = str(exc)

    # SSL check
    should_check_ssl = check_ssl and ep.check_ssl and parsed.scheme == "https"
    if should_check_ssl:
        port = parsed.port or 443
        expiry_str, days, warn = check_ssl_expiry(parsed.hostname or "", port)
        result.ssl_expiry = expiry_str
        result.ssl_days_remaining = days
        result.ssl_warning = warn

    return result


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

_STATUS_ICONS = {True: "[green]✓[/green]", False: "[red]✗[/red]"}


def results_table(results: list[CheckResult]) -> Table:
    """Build a Rich table of check results."""
    table = Table(title="API Health Check Results", show_lines=True, expand=True)
    table.add_column("Name", style="cyan", min_width=20)
    table.add_column("Status", justify="center", min_width=8)
    table.add_column("Code", justify="center", min_width=6)
    table.add_column("Response (ms)", justify="right", min_width=12)
    table.add_column("SSL Expiry", min_width=12)
    table.add_column("Error", style="red", min_width=20)

    healthy = 0
    for r in results:
        icon = _STATUS_ICONS.get(r.is_healthy, "?")
        ssl_info = r.ssl_expiry
        if r.ssl_warning:
            ssl_info = f"[yellow]{ssl_info} ({r.ssl_days_remaining}d)[/yellow]"
        elif r.ssl_days_remaining >= 0:
            ssl_info = f"[green]{ssl_info} ({r.ssl_days_remaining}d)[/green]"

        table.add_row(
            r.name,
            icon,
            str(r.status_code) if r.status_code else "-",
            f"{r.response_time_ms:.0f}" if r.response_time_ms else "-",
            ssl_info,
            r.error,
        )
        if r.is_healthy:
            healthy += 1

    return table


def display_summary(results: list[CheckResult]) -> None:
    """Print a summary panel."""
    total = len(results)
    healthy = sum(1 for r in results if r.is_healthy)
    failed = total - healthy
    ssl_warns = sum(1 for r in results if r.ssl_warning)

    color = "green" if failed == 0 else "red"
    console.print(
        Panel(
            f"[{color}]{healthy}/{total} healthy[/{color}]  "
            f"[red]{failed} failed[/red]  "
            f"[yellow]{ssl_warns} SSL warnings[/yellow]",
            title="Summary",
        )
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="1.0.0", prog_name="api_monitor")
def cli() -> None:
    """API Endpoint Health Monitor — check status, response time, and SSL certs."""


@cli.command()
@click.option("--config", type=click.Path(exists=True), help="JSON config file with endpoints.")
@click.option("--url", multiple=True, help="Endpoint URL(s) to check (repeatable).")
@click.option("--method", default="GET", help="HTTP method.")
@click.option("--expected-status", default=200, type=int, help="Expected HTTP status code.")
@click.option("--check-ssl/--no-check-ssl", default=True, help="Check SSL certificate.")
@click.option("--timeout", default=10.0, type=float, help="Request timeout in seconds.")
@click.option("--output", type=click.Path(), help="Save JSON results to file.")
def check(
    config: str | None,
    url: tuple[str, ...],
    method: str,
    expected_status: int,
    check_ssl: bool,
    timeout: float,
    output: str | None,
) -> None:
    """Run a one-shot health check on endpoints."""
    endpoints: list[EndpointConfig] = []

    if config:
        endpoints = load_config(Path(config))

    for u in url:
        endpoints.append(
            EndpointConfig(
                url=u,
                name=u,
                method=method,
                expected_status=expected_status,
                timeout=timeout,
                check_ssl=check_ssl,
            )
        )

    if not endpoints:
        console.print("[red]Error:[/red] Provide --config or --url")
        sys.exit(1)

    console.print(f"[bold]Checking {len(endpoints)} endpoint(s)...[/bold]\n")

    results: list[CheckResult] = []
    for ep in endpoints:
        with console.status(f"Checking {ep.name or ep.url}..."):
            result = check_endpoint(ep, check_ssl=check_ssl)
        results.append(result)

    table = results_table(results)
    console.print(table)
    display_summary(results)

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "name": r.name,
                        "url": r.url,
                        "status_code": r.status_code,
                        "response_time_ms": r.response_time_ms,
                        "is_healthy": r.is_healthy,
                        "error": r.error,
                        "ssl_expiry": r.ssl_expiry,
                        "ssl_days_remaining": r.ssl_days_remaining,
                        "ssl_warning": r.ssl_warning,
                        "timestamp": r.timestamp,
                    }
                    for r in results
                ],
                fh,
                indent=2,
            )
        console.print(f"[green]Results saved to {out_path}[/green]")

    failed = sum(1 for r in results if not r.is_healthy)
    sys.exit(1 if failed else 0)


@cli.command()
@click.option("--config", type=click.Path(exists=True), required=True, help="JSON config file.")
@click.option("--interval", default=60, type=int, help="Seconds between checks.")
@click.option("--count", default=0, type=int, help="Number of checks to run (0 = infinite).")
@click.option("--alert-command", default="", help="Shell command to run on failure (gets JSON on stdin).")
def watch(
    config: str,
    interval: int,
    count: int,
    alert_command: str,
) -> None:
    """Continuously monitor endpoints at a set interval."""
    endpoints = load_config(Path(config))
    if not endpoints:
        console.print("[red]Error:[/red] No endpoints in config")
        sys.exit(1)

    console.print(f"[bold]Watching {len(endpoints)} endpoint(s) every {interval}s[/bold]")
    console.print("Press Ctrl+C to stop.\n")

    run_num = 0
    try:
        while True:
            run_num += 1
            if count > 0 and run_num > count:
                break

            results: list[CheckResult] = []
            for ep in endpoints:
                result = check_endpoint(ep)
                results.append(result)

            # Clear and display
            console.clear()
            console.print(f"[dim]Run #{run_num} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]\n")
            table = results_table(results)
            console.print(table)
            display_summary(results)

            # Alert on failure
            failed = [r for r in results if not r.is_healthy]
            if failed and alert_command:
                import subprocess

                alert_data = json.dumps(
                    [{"name": r.name, "url": r.url, "error": r.error} for r in failed],
                    indent=2,
                )
                try:
                    subprocess.run(alert_command, shell=True, input=alert_data.encode(), timeout=30)
                except Exception as exc:
                    console.print(f"[red]Alert command failed:[/red] {exc}")

            time.sleep(interval)

    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


@cli.command()
def sample_config() -> None:
    """Print a sample JSON config file."""
    sample = {
        "endpoints": [
            {
                "url": "https://api.example.com/health",
                "name": "Main API",
                "method": "GET",
                "expected_status": 200,
                "expected_content": '"status":"ok"',
                "timeout": 5.0,
                "check_ssl": True,
                "ssl_warn_days": 30,
                "tags": ["production", "critical"],
            },
            {
                "url": "https://db.example.com:8080/status",
                "name": "Database Proxy",
                "method": "GET",
                "expected_status": 200,
                "timeout": 10.0,
            },
        ]
    }
    console.print_json(json.dumps(sample, indent=2))


if __name__ == "__main__":
    cli()
