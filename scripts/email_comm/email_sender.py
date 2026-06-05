#!/usr/bin/env python3
"""
email_sender.py — Bulk Email Sender with Jinja2 Templates

Send templated HTML emails in bulk via SMTP. Supports:
- Jinja2 template rendering for personalized content
- HTML templates with subject/body customization
- File attachments (multiple, any type)
- Rate limiting to avoid spam filters
- Per-recipient variable substitution
- Sent/failed tracking with detailed summary
- Dry-run mode for testing

Usage:
    python email_sender.py send --template welcome.html --recipients contacts.csv
    python email_sender.py send --subject "Hello" --body "<h1>Hi {{name}}</h1>" --to user@example.com
    python email_sender.py send --template report.html --recipients list.csv --dry-run
"""

from __future__ import annotations

import csv
import json
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, field
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import click
import jinja2
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SMTPConfig:
    """SMTP connection configuration."""

    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    use_tls: bool = True
    sender_name: str = ""
    sender_email: str = ""

    @classmethod
    def from_env(cls) -> "SMTPConfig":
        """Load config from environment variables."""
        import os

        return cls(
            host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            port=int(os.environ.get("SMTP_PORT", "587")),
            username=os.environ.get("SMTP_USERNAME", ""),
            password=os.environ.get("SMTP_PASSWORD", ""),
            use_tls=os.environ.get("SMTP_USE_TLS", "true").lower() == "true",
            sender_name=os.environ.get("SMTP_SENDER_NAME", ""),
            sender_email=os.environ.get("SMTP_SENDER_EMAIL", ""),
        )


@dataclass
class EmailRecord:
    """Tracks the result of sending one email."""

    recipient: str
    subject: str
    status: str  # "sent", "failed", "skipped"
    error: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Template engine
# ---------------------------------------------------------------------------

class TemplateEngine:
    """Render Jinja2 templates with recipient variables."""

    def __init__(self, template_dir: Path | None = None) -> None:
        if template_dir:
            self.env = jinja2.Environment(
                loader=jinja2.FileSystemLoader(str(template_dir)),
                autoescape=jinja2.select_autoescape(["html", "xml"]),
            )
        else:
            self.env = jinja2.Environment(
                autoescape=jinja2.select_autoescape(["html", "xml"]),
            )

    def render_template(self, template_path: Path, variables: dict[str, Any]) -> str:
        """Load a template file and render with variables."""
        with open(template_path, "r", encoding="utf-8") as fh:
            template = self.env.from_string(fh.read())
        return template.render(**variables)

    def render_string(self, template_str: str, variables: dict[str, Any]) -> str:
        """Render an inline template string."""
        template = self.env.from_string(template_str)
        return template.render(**variables)


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

def build_email(
    to_addr: str,
    subject: str,
    html_body: str,
    from_name: str,
    from_addr: str,
    attachments: list[Path] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> MIMEMultipart:
    """Build a MIME email with HTML body and optional attachments."""
    msg = MIMEMultipart("mixed")
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)

    # HTML body
    html_part = MIMEText(html_body, "html", "utf-8")
    msg.attach(html_part)

    # Attachments
    for attach_path in attachments or []:
        p = Path(attach_path)
        if not p.exists():
            console.print(f"[yellow]Warning:[/yellow] Attachment not found: {p}")
            continue
        with open(p, "rb") as fh:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(fh.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{p.name}"')
        msg.attach(part)

    return msg


# ---------------------------------------------------------------------------
# SMTP sender
# ---------------------------------------------------------------------------

class BulkSender:
    """Send emails in bulk via SMTP with rate limiting."""

    def __init__(
        self,
        config: SMTPConfig,
        rate_limit: float = 1.0,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.rate_limit = max(0.1, rate_limit)  # seconds between sends
        self.dry_run = dry_run
        self.results: list[EmailRecord] = []

    def _connect(self) -> smtplib.SMTP:
        """Establish SMTP connection."""
        ctx = ssl.create_default_context()
        server = smtplib.SMTP(self.config.host, self.config.port, timeout=30)
        if self.config.use_tls:
            server.starttls(context=ctx)
        if self.config.username and self.config.password:
            server.login(self.config.username, self.config.password)
        return server

    def send_bulk(
        self,
        recipients: list[dict[str, Any]],
        subject_template: str,
        body_template: str,
        template_engine: TemplateEngine,
        attachments: list[Path] | None = None,
        template_path: Path | None = None,
        subject_field: str = "subject",
    ) -> list[EmailRecord]:
        """Send emails to a list of recipients with per-recipient templating."""
        self.results = []

        # Build connection (or skip in dry-run)
        server = None
        if not self.dry_run:
            try:
                server = self._connect()
                console.print("[green]✓[/green] SMTP connected")
            except Exception as exc:
                console.print(f"[red]✗ SMTP connection failed:[/red] {exc}")
                for rec in recipients:
                    self.results.append(
                        EmailRecord(
                            recipient=rec.get("email", "?"),
                            subject="",
                            status="failed",
                            error=f"SMTP connection: {exc}",
                        )
                    )
                return self.results

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("Sending emails...", total=len(recipients))

                for recipient in recipients:
                    to_addr = recipient.get("email", "")
                    if not to_addr:
                        self.results.append(
                            EmailRecord(recipient="(empty)", subject="", status="skipped", error="No email address")
                        )
                        progress.advance(task)
                        continue

                    try:
                        # Render templates with recipient data
                        if template_path:
                            body_html = template_engine.render_template(template_path, recipient)
                            subject = recipient.get(subject_field, subject_template)
                            # Render subject as template too
                            subject = template_engine.render_string(subject, recipient)
                        else:
                            body_html = template_engine.render_string(body_template, recipient)
                            subject = template_engine.render_string(subject_template, recipient)

                        # Build email
                        msg = build_email(
                            to_addr=to_addr,
                            subject=subject,
                            html_body=body_html,
                            from_name=self.config.sender_name,
                            from_addr=self.config.sender_email or self.config.username,
                            attachments=attachments,
                        )

                        if self.dry_run:
                            status = "sent"
                            error = ""
                            console.print(f"  [cyan]DRY-RUN[/cyan] → {to_addr}: {subject}")
                        else:
                            server.sendmail(
                                self.config.sender_email or self.config.username,
                                [to_addr],
                                msg.as_string(),
                            )
                            status = "sent"
                            error = ""

                        self.results.append(
                            EmailRecord(recipient=to_addr, subject=subject, status=status, error=error)
                        )

                    except Exception as exc:
                        self.results.append(
                            EmailRecord(
                                recipient=to_addr,
                                subject=subject if "subject" in dir() else "",
                                status="failed",
                                error=str(exc),
                            )
                        )
                        console.print(f"  [red]✗[/red] {to_addr}: {exc}")

                    progress.advance(task)
                    time.sleep(self.rate_limit)

        finally:
            if server:
                try:
                    server.quit()
                except Exception:
                    pass

        return self.results

    def summary_table(self) -> Table:
        """Return a Rich table summarising send results."""
        table = Table(title="Email Send Summary", show_lines=True)
        table.add_column("Recipient", style="cyan")
        table.add_column("Subject")
        table.add_column("Status", justify="center")
        table.add_column("Error", style="red")

        sent = failed = skipped = 0
        for rec in self.results:
            style = {"sent": "green", "failed": "red", "skipped": "yellow"}.get(rec.status, "")
            table.add_row(rec.recipient, rec.subject, f"[{style}]{rec.status}[/{style}]", rec.error)
            if rec.status == "sent":
                sent += 1
            elif rec.status == "failed":
                failed += 1
            else:
                skipped += 1

        console.print(table)
        console.print(
            Panel(
                f"[green]Sent: {sent}[/green]  [red]Failed: {failed}[/red]  [yellow]Skipped: {skipped}[/yellow]  "
                f"Total: {len(self.results)}",
                title="Totals",
            )
        )
        return table


# ---------------------------------------------------------------------------
# Load recipients from CSV or JSON
# ---------------------------------------------------------------------------

def load_recipients(source: Path) -> list[dict[str, Any]]:
    """Load recipients from CSV or JSON file."""
    if source.suffix.lower() == ".json":
        with open(source, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
        console.print("[red]Error:[/red] JSON must be a list of objects")
        sys.exit(1)

    # CSV
    recipients = []
    with open(source, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            recipients.append(dict(row))
    return recipients


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="1.0.0", prog_name="email_sender")
def cli() -> None:
    """Bulk Email Sender — send templated HTML emails via SMTP."""


@cli.command()
@click.option("--host", envvar="SMTP_HOST", default="smtp.gmail.com", help="SMTP host.")
@click.option("--port", envvar="SMTP_PORT", default=587, type=int, help="SMTP port.")
@click.option("--username", envvar="SMTP_USERNAME", default="", help="SMTP username.")
@click.option("--password", envvar="SMTP_PASSWORD", default="", help="SMTP password.")
@click.option("--no-tls", is_flag=True, help="Disable STARTTLS.")
@click.option("--sender-name", default="", help="Display name for From header.")
@click.option("--sender-email", default="", help="Email address for From header.")
@click.option("--template", type=click.Path(exists=True), help="Path to Jinja2 HTML template file.")
@click.option("--subject", default="No Subject", help="Subject line (Jinja2 supported).")
@click.option("--body", default="", help="Inline HTML body (Jinja2 supported).")
@click.option("--recipients", type=click.Path(exists=True), help="CSV or JSON file with recipient data.")
@click.option("--to", multiple=True, help="Direct recipient email(s) for simple sends.")
@click.option("--var", multiple=True, type=(str, str), help="Template variables as key=value pairs.")
@click.option("--attachment", multiple=True, type=click.Path(exists=True), help="Files to attach.")
@click.option("--rate-limit", default=1.0, type=float, help="Seconds between sends (default: 1.0).")
@click.option("--dry-run", is_flag=True, help="Preview without actually sending.")
@click.option("--output", type=click.Path(), help="Save results JSON to file.")
def send(
    host: str,
    port: int,
    username: str,
    password: str,
    no_tls: bool,
    sender_name: str,
    sender_email: str,
    template: str | None,
    subject: str,
    body: str,
    recipients: str | None,
    to: tuple[str, ...],
    var: tuple[tuple[str, str], ...],
    attachment: tuple[str, ...],
    rate_limit: float,
    dry_run: bool,
    output: str | None,
) -> None:
    """Send bulk emails."""
    if not template and not body:
        console.print("[red]Error:[/red] Provide --template or --body")
        sys.exit(1)

    config = SMTPConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        use_tls=not no_tls,
        sender_name=sender_name,
        sender_email=sender_email,
    )

    engine = TemplateEngine()

    # Build recipient list
    recipient_list: list[dict[str, Any]] = []
    if recipients:
        recipient_list = load_recipients(Path(recipients))
    if to:
        for addr in to:
            recipient_list.append({"email": addr})
    for key, value in var:
        # Attach extra vars to all recipients
        for rec in recipient_list:
            rec.setdefault(key, value)

    if not recipient_list:
        console.print("[red]Error:[/red] No recipients provided")
        sys.exit(1)

    console.print(f"[bold]Sending to {len(recipient_list)} recipient(s)...[/bold]")
    if dry_run:
        console.print("[yellow]DRY-RUN mode — no emails will actually be sent[/yellow]")

    sender = BulkSender(config=config, rate_limit=rate_limit, dry_run=dry_run)

    template_path = Path(template) if template else None
    results = sender.send_bulk(
        recipients=recipient_list,
        subject_template=subject,
        body_template=body or "",
        template_engine=engine,
        attachments=[Path(a) for a in attachment],
        template_path=template_path,
    )

    sender.summary_table()

    # Save results
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "recipient": r.recipient,
                        "subject": r.subject,
                        "status": r.status,
                        "error": r.error,
                        "timestamp": r.timestamp,
                    }
                    for r in results
                ],
                fh,
                indent=2,
            )
        console.print(f"[green]Results saved to {out_path}[/green]")

    failed = sum(1 for r in results if r.status == "failed")
    sys.exit(1 if failed else 0)


@cli.command()
@click.option("--email", required=True, help="Test recipient email.")
@click.option("--host", envvar="SMTP_HOST", default="smtp.gmail.com")
@click.option("--port", envvar="SMTP_PORT", default=587, type=int)
@click.option("--username", envvar="SMTP_USERNAME", default="")
@click.option("--password", envvar="SMTP_PASSWORD", default="")
def test_connection(host: str, port: int, username: str, password: str, email: str) -> None:
    """Test SMTP connection and send a test email."""
    config = SMTPConfig(host=host, port=port, username=username, password=password)
    sender = BulkSender(config=config, dry_run=False)
    try:
        server = sender._connect()
        console.print(f"[green]✓[/green] Connected to {host}:{port}")

        msg = build_email(
            to_addr=email,
            subject="Test Email — email_sender.py",
            html_body="<h1>It works!</h1><p>Sent via <b>email_sender.py</b></p>",
            from_name="Email Sender Test",
            from_addr=username or email,
        )
        server.sendmail(username or email, [email], msg.as_string())
        server.quit()
        console.print(f"[green]✓[/green] Test email sent to {email}")
    except Exception as exc:
        console.print(f"[red]✗[/red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
