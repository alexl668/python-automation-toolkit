#!/usr/bin/env python3
"""Log Analyzer — Parse web server and custom log files for analysis.

Parses Apache/Nginx access logs, extracts error rates, response times,
top URLs, IP address analysis, and generates summary reports.

Usage examples:
    python log_analyzer.py analyze access.log
    python log_analyzer.py analyze /var/log/nginx/access.log --format nginx
    python log_analyzer.py analyze app.log --format custom --pattern "TIMESTAMP LEVEL MSG"
    python log_analyzer.py tail access.log --follow
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()


# ---------------------------------------------------------------------------
# Log format definitions
# ---------------------------------------------------------------------------

# Common Log Format / Combined Log Format (Apache default)
APACHE_PATTERN = re.compile(
    r'(?P<ip>\S+)\s+'           # client IP
    r'\S+\s+'                   # ident (usually -)
    r'(?P<user>\S+)\s+'         # user
    r'\[(?P<timestamp>[^\]]+)\]\s+'  # timestamp
    r'"(?P<method>\S+)\s+(?P<url>\S+)\s+\S+"\s+'  # request line
    r'(?P<status>\d{3})\s+'     # status code
    r'(?P<bytes>\S+)'           # response size
    r'(?:\s+"(?P<referer>[^"]*)"\s+"(?P<ua>[^"]*)")?'  # optional referer + user agent
)

# Nginx combined (same format, but sometimes with $request_time)
NGINX_PATTERN = re.compile(
    r'(?P<ip>\S+)\s+'
    r'\S+\s+(?P<user>\S+)\s+'
    r'\[(?P<timestamp>[^\]]+)\]\s+'
    r'"(?P<method>\S+)\s+(?P<url>\S+)\s+\S+"\s+'
    r'(?P<status>\d{3})\s+(?P<bytes>\S+)\s+'
    r'"(?P<referer>[^"]*)"\s+"(?P<ua>[^"]*)"\s*'
    r'(?P<rt>[\d.]+)?'  # optional request time
)

# Generic pattern for custom formats
CUSTOM_PATTERN: re.Pattern | None = None

LOG_FORMATS = {
    "apache": APACHE_PATTERN,
    "combined": APACHE_PATTERN,
    "nginx": NGINX_PATTERN,
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_line(line: str, pattern: re.Pattern) -> dict[str, Any] | None:
    """Parse a single log line against the given pattern."""
    m = pattern.match(line.strip())
    if not m:
        return None
    entry = m.groupdict()
    # Normalize
    entry["status"] = int(entry.get("status", 0))
    raw_bytes = entry.get("bytes", "-")
    entry["bytes"] = int(raw_bytes) if raw_bytes.isdigit() else 0
    rt = entry.get("rt")
    entry["response_time"] = float(rt) if rt else None
    return entry


def _build_custom_pattern(spec: str) -> re.Pattern:
    """Build a regex from a simplified format spec.

    Supported placeholders:
        IP, USER, TIMESTAMP, METHOD, URL, STATUS, BYTES, REFERER, UA, RT, MSG
    """
    placeholder_map = {
        "IP": r"(?P<ip>\S+)",
        "USER": r"(?P<user>\S+)",
        "TIMESTAMP": r"\[(?P<timestamp>[^\]]+)\]",
        "METHOD": r"(?P<method>\S+)",
        "URL": r"(?P<url>\S+)",
        "STATUS": r"(?P<status>\d{3})",
        "BYTES": r"(?P<bytes>\S+)",
        "REFERER": r'"(?P<referer>[^"]*)"',
        "UA": r'"(?P<ua>[^"]*)"',
        "RT": r"(?P<rt>[\d.]+)",
        "MSG": r"(?P<message>.*)",
    }
    regex = spec
    for token, replacement in placeholder_map.items():
        regex = regex.replace(token, replacement)
    # Escape everything except our injected groups and whitespace
    try:
        return re.compile(regex)
    except re.error as exc:
        console.print(f"[red]Invalid custom pattern:[/red] {exc}")
        raise SystemExit(1)


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse common log timestamp formats."""
    for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts.strip(), fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _analyze_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute analysis metrics from parsed log entries."""
    total = len(entries)
    if total == 0:
        return {"total": 0}

    status_codes = Counter(e["status"] for e in entries)
    ip_counter = Counter(e.get("ip", "-") for e in entries)
    url_counter = Counter(e.get("url", "-") for e in entries)
    method_counter = Counter(e.get("method", "-") for e in entries)
    ua_counter = Counter(e.get("ua", "-") for e in entries)

    # Error rate (4xx + 5xx)
    errors = sum(count for code, count in status_codes.items() if code >= 400)
    error_rate = errors / total * 100

    # Response times
    rtimes = [e["response_time"] for e in entries if e.get("response_time") is not None]
    rt_stats = {}
    if rtimes:
        rtimes_sorted = sorted(rtimes)
        rt_stats = {
            "min": rtimes_sorted[0],
            "max": rtimes_sorted[-1],
            "mean": sum(rtimes) / len(rtimes),
            "p50": rtimes_sorted[len(rtimes_sorted) // 2],
            "p95": rtimes_sorted[int(len(rtimes_sorted) * 0.95)],
            "p99": rtimes_sorted[int(len(rtimes_sorted) * 0.99)],
        }

    # Bytes transferred
    total_bytes = sum(e.get("bytes", 0) for e in entries)

    # Time range
    timestamps = [_parse_timestamp(e.get("timestamp", "")) for e in entries]
    timestamps = [t for t in timestamps if t is not None]
    time_range = None
    if timestamps:
        time_range = (min(timestamps), max(timestamps))

    return {
        "total": total,
        "status_codes": dict(status_codes.most_common(20)),
        "top_ips": ip_counter.most_common(20),
        "top_urls": url_counter.most_common(20),
        "methods": dict(method_counter.most_common()),
        "top_uas": ua_counter.most_common(10),
        "error_rate": error_rate,
        "errors": errors,
        "response_times": rt_stats,
        "total_bytes": total_bytes,
        "time_range": time_range,
    }


def _render_report(stats: dict[str, Any]) -> None:
    """Render analysis results as rich tables."""
    if stats["total"] == 0:
        console.print("[yellow]No log entries found.[/yellow]")
        return

    # Overview panel
    lines = [
        f"Total requests: [cyan]{stats['total']:,}[/cyan]",
        f"Unique errors:  [red]{stats['errors']:,}[/red] ({stats['error_rate']:.1f}%)",
        f"Total bytes:    [cyan]{stats['total_bytes'] / (1024*1024):.2f} MB[/cyan]",
    ]
    if stats.get("time_range"):
        start, end = stats["time_range"]
        lines.append(f"Time range:     [cyan]{start}[/cyan] → [cyan]{end}[/cyan]")
    console.print(Panel("\n".join(lines), title="[bold]Overview[/bold]", border_style="blue"))

    # HTTP methods
    if stats.get("methods"):
        t = Table(title="HTTP Methods", box=box.ROUNDED)
        t.add_column("Method", style="cyan")
        t.add_column("Count", justify="right")
        t.add_column("%", justify="right")
        for method, count in sorted(stats["methods"].items(), key=lambda x: -x[1]):
            t.add_row(method, f"{count:,}", f"{count / stats['total'] * 100:.1f}%")
        console.print(t)

    # Status codes
    if stats.get("status_codes"):
        t = Table(title="Status Codes", box=box.ROUNDED)
        t.add_column("Code", style="cyan")
        t.add_column("Count", justify="right")
        t.add_column("%", justify="right")
        t.add_column("Category")
        for code, count in sorted(stats["status_codes"].items()):
            if 200 <= code < 300:
                cat = "[green]Success[/green]"
            elif 300 <= code < 400:
                cat = "[yellow]Redirect[/yellow]"
            elif 400 <= code < 500:
                cat = "[red]Client Error[/red]"
            else:
                cat = "[bold red]Server Error[/bold red]"
            t.add_row(str(code), f"{count:,}", f"{count / stats['total'] * 100:.1f}%", cat)
        console.print(t)

    # Top IPs
    if stats.get("top_ips"):
        t = Table(title="Top 20 IPs", box=box.ROUNDED)
        t.add_column("IP Address", style="cyan")
        t.add_column("Requests", justify="right")
        t.add_column("%", justify="right")
        for ip, count in stats["top_ips"][:20]:
            t.add_row(ip, f"{count:,}", f"{count / stats['total'] * 100:.1f}%")
        console.print(t)

    # Top URLs
    if stats.get("top_urls"):
        t = Table(title="Top 20 URLs", box=box.ROUNDED)
        t.add_column("URL", style="cyan", overflow="fold")
        t.add_column("Hits", justify="right")
        for url, count in stats["top_urls"][:20]:
            t.add_row(url, f"{count:,}")
        console.print(t)

    # Response times
    if stats.get("response_times"):
        rt = stats["response_times"]
        t = Table(title="Response Times", box=box.ROUNDED)
        t.add_column("Metric", style="cyan")
        t.add_column("Value", justify="right")
        for metric in ("min", "mean", "p50", "p95", "p99", "max"):
            val = rt.get(metric)
            if val is not None:
                t.add_row(metric.upper(), f"{val:.3f}s")
        console.print(t)

    # Top User Agents
    if stats.get("top_uas"):
        t = Table(title="Top 10 User Agents", box=box.ROUNDED)
        t.add_column("User Agent", style="cyan", overflow="fold", max_width=80)
        t.add_column("Count", justify="right")
        for ua, count in stats["top_uas"][:10]:
            t.add_row(ua, f"{count:,}")
        console.print(t)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option("1.0.0", prog_name="log_analyzer")
def cli() -> None:
    """Log Analyzer — parse and analyze web server log files."""


@cli.command()
@click.argument("log_file")
@click.option("--format", "-f", "fmt", default="apache", type=click.Choice(["apache", "nginx", "custom"]), help="Log format.")
@click.option("--pattern", "-p", default=None, help="Custom format pattern (required if format=custom).")
@click.option("--output", "-o", default=None, help="Save report as JSON to this path.")
@click.option("--max-lines", "-n", type=int, default=None, help="Max lines to process.")
def analyze(log_file: str, fmt: str, pattern: str | None, output: str | None, max_lines: int | None) -> None:
    """Analyze a log file and produce a summary report."""
    p = Path(log_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {log_file}")
        raise SystemExit(1)

    # Determine pattern
    if fmt == "custom":
        if not pattern:
            console.print("[red]Error:[/red] --pattern required for custom format.")
            raise SystemExit(1)
        regex = _build_custom_pattern(pattern)
    else:
        regex = LOG_FORMATS[fmt]

    console.print(f"[blue]Parsing:[/blue] {p.name} (format: {fmt})")

    entries: list[dict[str, Any]] = []
    skipped = 0
    with open(p, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if max_lines and i > max_lines:
                break
            entry = _parse_line(line, regex)
            if entry:
                entries.append(entry)
            else:
                skipped += 1

    console.print(f"[green]Parsed:[/green] {len(entries):,} entries ({skipped:,} lines skipped)")

    stats = _analyze_entries(entries)
    _render_report(stats)

    if output:
        import json
        # Serialize stats for JSON
        serializable = {}
        for k, v in stats.items():
            if k == "time_range" and v:
                serializable[k] = [t.isoformat() for t in v]
            else:
                serializable[k] = v
        out = Path(output)
        with open(out, "w") as f:
            json.dump(serializable, f, indent=2, default=str)
        console.print(f"[green]✓[/green] Report saved to [cyan]{out}[/cyan]")


@cli.command()
@click.argument("log_file")
@click.option("--format", "-f", "fmt", default="apache", type=click.Choice(["apache", "nginx", "custom"]))
@click.option("--pattern", "-p", default=None, help="Custom format pattern.")
@click.option("--errors-only", is_flag=True, help="Show only 4xx/5xx entries.")
@click.option("--ip", default=None, help="Filter by IP address.")
@click.option("--follow", is_flag=True, help="Follow file for new entries (like tail -f).")
def tail(log_file: str, fmt: str, pattern: str | None, errors_only: bool, ip: str | None, follow: bool) -> None:
    """Tail a log file with live parsing and filtering."""
    import time

    p = Path(log_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {log_file}")
        raise SystemExit(1)

    if fmt == "custom":
        if not pattern:
            console.print("[red]Error:[/red] --pattern required for custom format.")
            raise SystemExit(1)
        regex = _build_custom_pattern(pattern)
    else:
        regex = LOG_FORMATS[fmt]

    def _display(entry: dict[str, Any]) -> None:
        status = entry["status"]
        style = "green" if status < 300 else "yellow" if status < 400 else "red"
        console.print(
            f"[{style}]{status}[/{style}] "
            f"[cyan]{entry.get('ip', '-')}[/cyan] "
            f"{entry.get('method', '-')} {entry.get('url', '-')} "
            f"[dim]{entry.get('ua', '-')}[/dim]"
        )

    if follow:
        console.print(f"[blue]Following:[/blue] {p.name} (Ctrl+C to stop)")
        with open(p, encoding="utf-8", errors="replace") as f:
            # Seek to end
            f.seek(0, 2)
            try:
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.2)
                        continue
                    entry = _parse_line(line, regex)
                    if not entry:
                        continue
                    if errors_only and entry["status"] < 400:
                        continue
                    if ip and entry.get("ip") != ip:
                        continue
                    _display(entry)
            except KeyboardInterrupt:
                console.print("\n[dim]Stopped.[/dim]")
    else:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                entry = _parse_line(line, regex)
                if not entry:
                    continue
                if errors_only and entry["status"] < 400:
                    continue
                if ip and entry.get("ip") != ip:
                    continue
                _display(entry)


@cli.command()
@click.argument("log_file")
@click.option("--format", "-f", "fmt", default="apache", type=click.Choice(["apache", "nginx", "custom"]))
@click.option("--pattern", "-p", default=None)
@click.option("--threshold", "-t", type=int, default=100, help="Requests-per-IP threshold for alerting.")
def suspicious(log_file: str, fmt: str, pattern: str | None, threshold: int) -> None:
    """Detect suspicious activity: high-frequency IPs, scanning patterns."""
    p = Path(log_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {log_file}")
        raise SystemExit(1)

    if fmt == "custom":
        if not pattern:
            console.print("[red]Error:[/red] --pattern required.")
            raise SystemExit(1)
        regex = _build_custom_pattern(pattern)
    else:
        regex = LOG_FORMATS[fmt]

    entries = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            entry = _parse_line(line, regex)
            if entry:
                entries.append(entry)

    if not entries:
        console.print("[yellow]No entries found.[/yellow]")
        return

    ip_counter = Counter(e.get("ip", "-") for e in entries)
    error_ips = Counter(e.get("ip", "-") for e in entries if e["status"] >= 400)

    # High-frequency IPs
    high_freq = [(ip, count) for ip, count in ip_counter.most_common() if count >= threshold]
    if high_freq:
        t = Table(title=f"⚠ High-Frequency IPs (≥{threshold} requests)", box=box.ROUNDED, border_style="red")
        t.add_column("IP", style="red")
        t.add_column("Total Requests", justify="right")
        t.add_column("Errors", justify="right")
        t.add_column("Error Rate", justify="right")
        for ip, count in high_freq[:20]:
            err = error_ips.get(ip, 0)
            rate = err / count * 100 if count > 0 else 0
            t.add_row(ip, f"{count:,}", f"{err:,}", f"{rate:.1f}%")
        console.print(t)
    else:
        console.print(f"[green]No IPs with ≥{threshold} requests.[/green]")

    # 404 scanners
    not_found_ips = Counter(e.get("ip", "-") for e in entries if e["status"] == 404)
    scanners = [(ip, count) for ip, count in not_found_ips.most_common(10) if count >= 10]
    if scanners:
        t = Table(title="🔍 Potential 404 Scanners", box=box.ROUNDED, border_style="yellow")
        t.add_column("IP", style="yellow")
        t.add_column("404 Count", justify="right")
        for ip, count in scanners:
            t.add_row(ip, f"{count:,}")
        console.print(t)


if __name__ == "__main__":
    cli()
