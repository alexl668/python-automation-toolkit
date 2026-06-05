#!/usr/bin/env python3
"""
email_analyzer.py — IMAP Email Inbox Analyzer

Connect to an IMAP mailbox and produce a detailed report:
- Total / unread / recent message counts
- Top senders by frequency
- Date-range distribution
- Attachment summary (types, sizes)
- Subject line analysis

Usage:
    python email_analyzer.py analyze --host imap.gmail.com --user me@gmail.com --password ***
    python email_analyzer.py analyze --config inbox.json --folder INBOX
    python email_analyzer.py analyze --host imap.gmail.com --user me@x.com --password *** --since 2025-01-01 --top 15
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AttachmentInfo:
    """Metadata for one attachment."""

    filename: str
    mime_type: str
    size_bytes: int


@dataclass
class EmailMeta:
    """Parsed metadata for a single email."""

    uid: str
    sender: str
    sender_name: str
    subject: str
    date: datetime | None
    size_bytes: int
    is_read: bool
    attachments: list[AttachmentInfo] = field(default_factory=list)


@dataclass
class InboxReport:
    """Aggregated inbox analysis."""

    total: int = 0
    unread: int = 0
    recent: int = 0
    top_senders: list[tuple[str, int]] = field(default_factory=list)
    daily_distribution: dict[str, int] = field(default_factory=dict)
    attachment_types: Counter = field(default_factory=Counter)
    total_attachment_bytes: int = 0
    attachment_count: int = 0
    avg_size_bytes: float = 0.0
    subject_keywords: list[tuple[str, int]] = field(default_factory=list)
    date_range: tuple[str, str] = ("", "")


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def connect(host: str, port: int, user: str, password: str, use_ssl: bool = True) -> imaplib.IMAP4_SSL:
    """Open an IMAP connection."""
    if use_ssl:
        server = imaplib.IMAP4_SSL(host, port)
    else:
        server = imaplib.IMAP4(host, port)
    server.login(user, password)
    return server


def _decode_header(raw: str | None) -> str:
    """Decode an RFC 2047 encoded header into a plain string."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _parse_date(raw: str | None) -> datetime | None:
    """Parse an email Date header."""
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed
    except Exception:
        return None


def _extract_attachments(msg: email.message.Message) -> list[AttachmentInfo]:
    """Walk a message and collect attachment metadata."""
    attachments: list[AttachmentInfo] = []
    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        if "attachment" in content_disposition:
            filename = part.get_filename() or "unnamed"
            filename = _decode_header(filename)
            mime_type = part.get_content_type() or "application/octet-stream"
            payload = part.get_payload(decode=True)
            size = len(payload) if payload else 0
            attachments.append(AttachmentInfo(filename=filename, mime_type=mime_type, size_bytes=size))
    return attachments


def parse_message(raw_bytes: bytes, uid: str = "", flags: str = "") -> EmailMeta:
    """Parse raw RFC822 bytes into an EmailMeta."""
    msg = email.message_from_bytes(raw_bytes)

    sender_raw = msg.get("From", "")
    sender_name, sender_addr = email.utils.parseaddr(sender_raw)
    sender_name = _decode_header(sender_name) or sender_addr

    subject = _decode_header(msg.get("Subject", "(no subject)"))
    date = _parse_date(msg.get("Date"))
    size = len(raw_bytes)
    is_read = "\\Seen" in flags

    attachments = _extract_attachments(msg)

    return EmailMeta(
        uid=uid,
        sender=sender_addr,
        sender_name=sender_name,
        subject=subject,
        date=date,
        size_bytes=size,
        is_read=is_read,
        attachments=attachments,
    )


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

STOP_WORDS = {
    "the", "a", "an", "is", "it", "to", "in", "of", "and", "for", "on", "at", "by",
    "with", "from", "or", "be", "as", "are", "was", "were", "been", "this", "that",
    "re", "fw", "fwd", "you", "your", "we", "our", "i", "my",
}


def analyze_inbox(
    messages: list[EmailMeta],
    top_n: int = 10,
) -> InboxReport:
    """Aggregate parsed messages into a report."""
    report = InboxReport()
    report.total = len(messages)
    report.unread = sum(1 for m in messages if not m.is_read)

    if not messages:
        return report

    # Senders
    sender_counter: Counter = Counter()
    for m in messages:
        key = m.sender or m.sender_name or "(unknown)"
        sender_counter[key] += 1
    report.top_senders = sender_counter.most_common(top_n)

    # Date distribution
    daily: Counter = Counter()
    dates = [m.date for m in messages if m.date]
    for d in dates:
        daily[d.strftime("%Y-%m-%d")] += 1
    report.daily_distribution = dict(daily.most_common(30))
    if dates:
        oldest = min(dates)
        newest = max(dates)
        report.date_range = (oldest.strftime("%Y-%m-%d"), newest.strftime("%Y-%m-%d"))

    # Attachments
    attach_types: Counter = Counter()
    total_attach_bytes = 0
    attach_count = 0
    for m in messages:
        for att in m.attachments:
            ext = Path(att.filename).suffix.lower() or att.mime_type
            attach_types[ext] += 1
            total_attach_bytes += att.size_bytes
            attach_count += 1
    report.attachment_types = attach_types
    report.total_attachment_bytes = total_attach_bytes
    report.attachment_count = attach_count

    # Average message size
    report.avg_size_bytes = sum(m.size_bytes for m in messages) / len(messages)

    # Subject keywords
    word_counter: Counter = Counter()
    for m in messages:
        words = re.findall(r"[a-zA-Z]{3,}", m.subject.lower())
        for w in words:
            if w not in STOP_WORDS:
                word_counter[w] += 1
    report.subject_keywords = word_counter.most_common(top_n)

    return report


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def display_report(report: InboxReport) -> None:
    """Render the report to the console with Rich."""
    # Overview
    overview = Table.grid(padding=1)
    overview.add_row("Total messages", str(report.total))
    overview.add_row("Unread", str(report.unread))
    overview.add_row("Read", str(report.total - report.unread))
    overview.add_row("Date range", f"{report.date_range[0]} → {report.date_range[1]}" if report.date_range[0] else "N/A")
    overview.add_row("Avg message size", _fmt_bytes(int(report.avg_size_bytes)))
    overview.add_row("Attachments", f"{report.attachment_count} files ({_fmt_bytes(report.total_attachment_bytes)})")
    console.print(Panel(overview, title="📬 Inbox Overview"))

    # Top senders
    if report.top_senders:
        t = Table(title="Top Senders", show_lines=True)
        t.add_column("#", justify="right", style="dim")
        t.add_column("Sender", style="cyan")
        t.add_column("Count", justify="right", style="green")
        for i, (sender, count) in enumerate(report.top_senders, 1):
            t.add_row(str(i), sender, str(count))
        console.print(t)

    # Daily distribution
    if report.daily_distribution:
        t = Table(title="Daily Distribution (top 30 days)", show_lines=False)
        t.add_column("Date", style="cyan")
        t.add_column("Count", justify="right", style="green")
        t.add_column("Bar")
        max_count = max(report.daily_distribution.values()) or 1
        for day, count in sorted(report.daily_distribution.items()):
            bar_len = int(30 * count / max_count)
            t.add_row(day, str(count), "█" * bar_len)
        console.print(t)

    # Attachment types
    if report.attachment_types:
        t = Table(title="Attachment Types", show_lines=False)
        t.add_column("Extension / MIME", style="cyan")
        t.add_column("Count", justify="right", style="green")
        for ext, count in report.attachment_types.most_common(20):
            t.add_row(ext, str(count))
        console.print(t)

    # Subject keywords
    if report.subject_keywords:
        t = Table(title="Subject Keywords (top)", show_lines=False)
        t.add_column("Keyword", style="cyan")
        t.add_column("Occurrences", justify="right", style="green")
        for kw, count in report.subject_keywords:
            t.add_row(kw, str(count))
        console.print(t)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="1.0.0", prog_name="email_analyzer")
def cli() -> None:
    """IMAP Email Inbox Analyzer — analyze and report on your mailbox."""


@cli.command()
@click.option("--host", envvar="IMAP_HOST", default="", help="IMAP server host.")
@click.option("--port", envvar="IMAP_PORT", default=993, type=int, help="IMAP port.")
@click.option("--user", envvar="IMAP_USER", default="", help="IMAP username.")
@click.option("--password", envvar="IMAP_PASSWORD", default="", help="IMAP password.")
@click.option("--no-ssl", is_flag=True, help="Disable SSL.")
@click.option("--folder", default="INBOX", help="Mailbox folder to analyze.")
@click.option("--since", default="", help="Only messages since this date (YYYY-MM-DD).")
@click.option("--before", default="", help="Only messages before this date (YYYY-MM-DD).")
@click.option("--limit", default=500, type=int, help="Max messages to fetch (0 = all).")
@click.option("--top", default=10, type=int, help="Number of top items to show.")
@click.option("--config", type=click.Path(exists=True), help="JSON config file with connection details.")
@click.option("--output", type=click.Path(), help="Save JSON report to file.")
def analyze(
    host: str,
    port: int,
    user: str,
    password: str,
    no_ssl: bool,
    folder: str,
    since: str,
    before: str,
    limit: int,
    top: int,
    config: str | None,
    output: str | None,
) -> None:
    """Connect to IMAP and analyze the inbox."""
    # Load from config if provided
    if config:
        with open(config, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        host = cfg.get("host", host)
        port = cfg.get("port", port)
        user = cfg.get("user", user)
        password = cfg.get("password", password)
        folder = cfg.get("folder", folder)

    if not host or not user or not password:
        console.print("[red]Error:[/red] Provide --host, --user, --password or --config")
        sys.exit(1)

    console.print(f"[bold]Connecting to {host}:{port}...[/bold]")
    try:
        server = connect(host, port, user, password, use_ssl=not no_ssl)
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {exc}")
        sys.exit(1)

    try:
        status, data = server.select(folder, readonly=True)
        if status != "OK":
            console.print(f"[red]Cannot open folder '{folder}':[/red] {data}")
            sys.exit(1)

        # Build search criteria
        criteria_parts = ["ALL"]
        if since:
            criteria_parts = [f'(SINCE "{since}")']
        if before:
            criteria_parts.append(f'(BEFORE "{before}")')
        search_criteria = " ".join(criteria_parts)

        status, msg_ids = server.search(None, search_criteria)
        if status != "OK":
            console.print(f"[red]Search failed:[/red] {msg_ids}")
            sys.exit(1)

        all_ids = msg_ids[0].split()
        if limit > 0 and len(all_ids) > limit:
            all_ids = all_ids[-limit:]  # most recent N

        console.print(f"[bold]Fetching {len(all_ids)} messages...[/bold]")
        messages: list[EmailMeta] = []

        # Fetch in batches of 50
        batch_size = 50
        for i in range(0, len(all_ids), batch_size):
            batch = all_ids[i : i + batch_size]
            id_range = b",".join(batch)
            status, fetch_data = server.fetch(id_range, "(FLAGS RFC822)")
            if status != "OK":
                continue

            for j in range(0, len(fetch_data), 2):
                item = fetch_data[j]
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                meta_line = item[0]
                raw_bytes = item[1]
                uid_match = re.search(rb"(\d+)", meta_line)
                uid = uid_match.group(1).decode() if uid_match else ""
                flags_match = re.search(rb"FLAGS \(([^)]*)\)", meta_line)
                flags = flags_match.group(1).decode() if flags_match else ""

                try:
                    parsed = parse_message(raw_bytes, uid=uid, flags=flags)
                    messages.append(parsed)
                except Exception:
                    pass

        console.print(f"[green]✓[/green] Parsed {len(messages)} messages")

        report = analyze_inbox(messages, top_n=top)
        display_report(report)

        if output:
            out_path = Path(output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "total": report.total,
                        "unread": report.unread,
                        "top_senders": report.top_senders,
                        "daily_distribution": report.daily_distribution,
                        "attachment_types": dict(report.attachment_types),
                        "total_attachment_bytes": report.total_attachment_bytes,
                        "attachment_count": report.attachment_count,
                        "subject_keywords": report.subject_keywords,
                        "date_range": report.date_range,
                    },
                    fh,
                    indent=2,
                )
            console.print(f"[green]Report saved to {out_path}[/green]")

    finally:
        try:
            server.close()
            server.logout()
        except Exception:
            pass


@cli.command()
@click.option("--host", envvar="IMAP_HOST", default="", help="IMAP server host.")
@click.option("--port", envvar="IMAP_PORT", default=993, type=int)
@click.option("--user", envvar="IMAP_USER", default="")
@click.option("--password", envvar="IMAP_PASSWORD", default="")
def folders(host: str, port: int, user: str, password: str) -> None:
    """List all mailbox folders."""
    if not host or not user or not password:
        console.print("[red]Error:[/red] Provide --host, --user, --password")
        sys.exit(1)

    server = connect(host, port, user, password)
    try:
        status, folder_list = server.list()
        if status == "OK":
            t = Table(title="Mailbox Folders")
            t.add_column("Flags", style="dim")
            t.add_column("Folder", style="cyan")
            for item in folder_list:
                parts = item.decode() if isinstance(item, bytes) else str(item)
                match = re.match(r'\(([^)]*)\)\s+"([^"]*?)"\s+(.+)', parts)
                if match:
                    t.add_row(match.group(1), match.group(3))
                else:
                    t.add_row("", parts)
            console.print(t)
    finally:
        server.logout()


if __name__ == "__main__":
    cli()
