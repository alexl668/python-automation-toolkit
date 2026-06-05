#!/usr/bin/env python3
"""
webhook_server.py — FastAPI Webhook Receiver & Logger

A simple but production-ready webhook server that:
- Logs all incoming webhook payloads (headers + body)
- Supports custom handler registration via decorators
- Stores payloads for replay
- Provides a REST API to list, inspect, and replay webhooks
- Optional HMAC signature verification

Usage:
    python webhook_server.py serve --port 8000
    python webhook_server.py serve --port 8000 --secret my-webhook-secret
    python webhook_server.py replay --id <webhook-id> --target https://example.com/hook
    python webhook_server.py list --limit 20
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import click
import requests
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from rich.console import Console
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DATA_DIR = Path.home() / ".webhook_server" / "data"


def _ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


@dataclass
class WebhookRecord:
    """A stored webhook payload."""

    id: str
    timestamp: str
    method: str
    path: str
    headers: dict[str, str]
    body: str
    query_params: dict[str, str]
    source_ip: str
    content_type: str
    size_bytes: int
    handler: str = ""
    response_status: int = 200
    replayed: bool = False
    tags: list[str] = field(default_factory=list)


def save_webhook(record: WebhookRecord) -> None:
    """Persist a webhook record to disk."""
    data_dir = _ensure_data_dir()
    filepath = data_dir / f"{record.id}.json"
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(asdict(record), fh, indent=2)


def load_webhook(webhook_id: str) -> WebhookRecord | None:
    """Load a webhook record by ID."""
    filepath = DATA_DIR / f"{webhook_id}.json"
    if not filepath.exists():
        return None
    with open(filepath, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return WebhookRecord(**data)


def list_webhooks(limit: int = 50, path_filter: str = "") -> list[WebhookRecord]:
    """List stored webhooks, most recent first."""
    data_dir = _ensure_data_dir()
    records: list[WebhookRecord] = []
    for fp in sorted(data_dir.glob("*.json"), reverse=True):
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            rec = WebhookRecord(**data)
            if path_filter and path_filter not in rec.path:
                continue
            records.append(rec)
            if len(records) >= limit:
                break
        except Exception:
            continue
    return records


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

# Custom handlers: path_pattern -> handler_fn
# handler_fn signature: (path: str, headers: dict, body: Any) -> dict | None
_custom_handlers: dict[str, Callable] = {}


def register_handler(path_pattern: str) -> Callable:
    """Decorator to register a custom webhook handler for a path pattern."""

    def decorator(fn: Callable) -> Callable:
        _custom_handlers[path_pattern] = fn
        return fn

    return decorator


def _find_handler(path: str) -> tuple[str, Callable | None]:
    """Find a matching handler for the given path."""
    for pattern, fn in _custom_handlers.items():
        if pattern == "*" or pattern in path:
            return pattern, fn
    return "", None


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(
    payload: bytes,
    signature: str | None,
    secret: str,
    algorithm: str = "sha256",
) -> bool:
    """Verify HMAC signature on a webhook payload."""
    if not signature or not secret:
        return not secret  # if no secret configured, pass; if secret set, require sig

    # Support common formats: "sha256=<hex>", "v1=<hex>", raw hex
    sig_value = signature
    for prefix in ("sha256=", "sha1=", "v1=", "v0="):
        if signature.startswith(prefix):
            sig_value = signature[len(prefix) :]
            break

    expected = hmac.new(secret.encode(), payload, getattr(hashlib, algorithm)).hexdigest()
    return hmac.compare_digest(expected, sig_value)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def create_app(webhook_secret: str = "", log_to_console: bool = True) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Webhook Server",
        description="Webhook receiver, logger, and replayer",
        version="1.0.0",
    )

    @app.post("/webhook/{path:path}")
    async def receive_webhook(path: str, request: Request) -> Response:
        """Catch-all webhook endpoint. Logs and processes incoming webhooks."""
        # Read body
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8", errors="replace")

        # Parse JSON if possible
        try:
            body_data = json.loads(body_str)
        except (json.JSONDecodeError, ValueError):
            body_data = body_str

        # Verify signature if secret is set
        if webhook_secret:
            sig_header = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Signature-256") or ""
            if not verify_signature(body_bytes, sig_header, webhook_secret):
                return JSONResponse(status_code=401, content={"error": "Invalid signature"})

        # Build record
        record = WebhookRecord(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            method=request.method,
            path=f"/{path}",
            headers={k: v for k, v in request.headers.items()},
            body=body_str,
            query_params=dict(request.query_params),
            source_ip=request.client.host if request.client else "",
            content_type=request.headers.get("content-type", ""),
            size_bytes=len(body_bytes),
        )

        # Find and run custom handler
        handler_name, handler_fn = _find_handler(path)
        if handler_fn:
            try:
                handler_result = handler_fn(path, dict(request.headers), body_data)
                record.handler = handler_name
                record.tags.append("handled")
                if handler_result:
                    save_webhook(record)
                    return JSONResponse(content=handler_result)
            except Exception as exc:
                record.tags.append(f"handler_error:{exc}")
                record.response_status = 500

        # Save
        save_webhook(record)

        if log_to_console:
            console.print(
                f"[dim]{record.timestamp}[/dim]  "
                f"[cyan]{record.method}[/cyan] [bold]/{path}[/bold]  "
                f"[green]{record.size_bytes}B[/green]  "
                f"[dim]{record.source_ip}[/dim]"
            )

        return JSONResponse(
            status_code=record.response_status,
            content={"status": "received", "id": record.id},
        )

    @app.get("/api/webhooks")
    async def api_list_webhooks(limit: int = 50, path: str = "") -> Any:
        """List recent webhooks."""
        records = list_webhooks(limit=limit, path_filter=path)
        return [
            {
                "id": r.id,
                "timestamp": r.timestamp,
                "method": r.method,
                "path": r.path,
                "size_bytes": r.size_bytes,
                "handler": r.handler,
                "tags": r.tags,
            }
            for r in records
        ]

    @app.get("/api/webhooks/{webhook_id}")
    async def api_get_webhook(webhook_id: str) -> Any:
        """Get full details of a webhook by ID."""
        rec = load_webhook(webhook_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Webhook not found")
        return asdict(rec)

    @app.post("/api/webhooks/{webhook_id}/replay")
    async def api_replay_webhook(webhook_id: str, target: str = "") -> Any:
        """Replay a stored webhook to its original path or a new target."""
        rec = load_webhook(webhook_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Webhook not found")

        target_url = target or f"http://localhost:8000{rec.path}"
        try:
            resp = requests.request(
                method=rec.method,
                url=target_url,
                headers={k: v for k, v in rec.headers.items() if k.lower() not in ("host", "content-length")},
                data=rec.body.encode("utf-8") if isinstance(rec.body, str) else rec.body,
                timeout=30,
            )
            rec.replayed = True
            rec.tags.append(f"replayed_to:{target_url}")
            save_webhook(rec)
            return {
                "replayed_to": target_url,
                "response_status": resp.status_code,
                "response_body": resp.text[:1000],
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Replay failed: {exc}")

    @app.delete("/api/webhooks/{webhook_id}")
    async def api_delete_webhook(webhook_id: str) -> Any:
        """Delete a stored webhook."""
        filepath = DATA_DIR / f"{webhook_id}.json"
        if not filepath.exists():
            raise HTTPException(status_code=404, detail="Webhook not found")
        filepath.unlink()
        return {"deleted": webhook_id}

    @app.get("/health")
    async def health() -> Any:
        """Health check endpoint."""
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="1.0.0", prog_name="webhook_server")
def cli() -> None:
    """Webhook Server — receive, log, inspect, and replay webhooks."""


@cli.command()
@click.option("--host", default="0.0.0.0", help="Bind host.")
@click.option("--port", default=8000, type=int, help="Bind port.")
@click.option("--secret", default="", envvar="WEBHOOK_SECRET", help="HMAC secret for signature verification.")
@click.option("--no-console-log", is_flag=True, help="Suppress console logging of incoming webhooks.")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development.")
def serve(host: str, port: int, secret: str, no_console_log: bool, reload: bool) -> None:
    """Start the webhook server."""
    app = create_app(webhook_secret=secret, log_to_console=not no_console_log)
    _ensure_data_dir()

    console.print(Panel(
        f"[bold]Webhook Server[/bold]\n"
        f"Listening on [cyan]{host}:{port}[/cyan]\n"
        f"Signature verification: {'[green]ON[/green]' if secret else '[dim]OFF[/dim]'}\n"
        f"Data dir: [dim]{DATA_DIR}[/dim]\n\n"
        f"Endpoints:\n"
        f"  POST /webhook/<path>        — Receive webhooks\n"
        f"  GET  /api/webhooks          — List webhooks\n"
        f"  GET  /api/webhooks/<id>     — Get webhook details\n"
        f"  POST /api/webhooks/<id>/replay — Replay webhook\n"
        f"  GET  /health                — Health check",
        title="Starting...",
    ))

    uvicorn.run(app, host=host, port=port, reload=reload)


@cli.command()
@click.option("--limit", default=20, type=int, help="Number of webhooks to show.")
@click.option("--path", default="", help="Filter by path substring.")
def list_cmd(limit: int, path: str) -> None:
    """List stored webhooks."""
    records = list_webhooks(limit=limit, path_filter=path)
    if not records:
        console.print("[dim]No webhooks found.[/dim]")
        return

    table = Table(title="Stored Webhooks", show_lines=True)
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Timestamp", style="cyan")
    table.add_column("Method", style="green")
    table.add_column("Path")
    table.add_column("Size", justify="right")
    table.add_column("Handler")
    table.add_column("Tags")

    for r in records:
        table.add_row(
            r.id[:12],
            r.timestamp[:19],
            r.method,
            r.path,
            f"{r.size_bytes}B",
            r.handler or "-",
            ", ".join(r.tags) if r.tags else "-",
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(records)} (showing {limit} max)[/dim]")


@cli.command()
@click.argument("webhook_id")
def show(webhook_id: str) -> None:
    """Show full details of a stored webhook."""
    rec = load_webhook(webhook_id)
    if not rec:
        # Try prefix match
        data_dir = _ensure_data_dir()
        matches = list(data_dir.glob(f"{webhook_id}*.json"))
        if matches:
            with open(matches[0], "r", encoding="utf-8") as fh:
                data = json.load(fh)
            rec = WebhookRecord(**data)

    if not rec:
        console.print(f"[red]Webhook not found:[/red] {webhook_id}")
        sys.exit(1)

    console.print(Panel(
        f"[bold]ID:[/bold] {rec.id}\n"
        f"[bold]Time:[/bold] {rec.timestamp}\n"
        f"[bold]Method:[/bold] {rec.method}\n"
        f"[bold]Path:[/bold] {rec.path}\n"
        f"[bold]Source IP:[/bold] {rec.source_ip}\n"
        f"[bold]Content-Type:[/bold] {rec.content_type}\n"
        f"[bold]Size:[/bold] {rec.size_bytes} bytes\n"
        f"[bold]Handler:[/bold] {rec.handler or 'none'}\n"
        f"[bold]Tags:[/bold] {', '.join(rec.tags) or '-'}\n"
        f"[bold]Replayed:[/bold] {'yes' if rec.replayed else 'no'}",
        title="Webhook Details",
    ))

    # Headers
    h_table = Table(title="Headers")
    h_table.add_column("Header", style="cyan")
    h_table.add_column("Value")
    skip_headers = {"host", "content-length", "connection"}
    for k, v in rec.headers.items():
        if k.lower() not in skip_headers:
            h_table.add_row(k, v)
    console.print(h_table)

    # Body
    try:
        body_json = json.loads(rec.body)
        console.print(Panel(json.dumps(body_json, indent=2), title="Body (JSON)"))
    except (json.JSONDecodeError, ValueError):
        console.print(Panel(rec.body[:3000], title="Body"))


@cli.command()
@click.argument("webhook_id")
@click.option("--target", default="", help="Target URL. Defaults to original path on localhost:8000.")
def replay(webhook_id: str, target: str) -> None:
    """Replay a stored webhook."""
    rec = load_webhook(webhook_id)
    if not rec:
        # Prefix match
        data_dir = _ensure_data_dir()
        matches = list(data_dir.glob(f"{webhook_id}*.json"))
        if matches:
            with open(matches[0], "r", encoding="utf-8") as fh:
                data = json.load(fh)
            rec = WebhookRecord(**data)

    if not rec:
        console.print(f"[red]Webhook not found:[/red] {webhook_id}")
        sys.exit(1)

    target_url = target or f"http://localhost:8000{rec.path}"
    console.print(f"Replaying [cyan]{rec.method}[/cyan] to [bold]{target_url}[/bold]...")

    try:
        resp = requests.request(
            method=rec.method,
            url=target_url,
            headers={k: v for k, v in rec.headers.items() if k.lower() not in ("host", "content-length")},
            data=rec.body.encode("utf-8") if isinstance(rec.body, str) else rec.body,
            timeout=30,
        )
        console.print(f"[green]✓[/green] Response: {resp.status_code}")
        if resp.text:
            console.print(f"Body: {resp.text[:500]}")

        rec.replayed = True
        rec.tags.append(f"replayed:{datetime.now(timezone.utc).isoformat()}")
        save_webhook(rec)

    except Exception as exc:
        console.print(f"[red]✗ Replay failed:[/red] {exc}")
        sys.exit(1)


@cli.command()
def data_dir() -> None:
    """Print the webhook data directory path."""
    console.print(str(DATA_DIR))


if __name__ == "__main__":
    cli()
