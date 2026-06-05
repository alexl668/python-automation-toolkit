#!/usr/bin/env python3
"""Generate professional PDF invoices from JSON data using Jinja2 HTML templates.

Usage:
    python invoice_generator.py generate --input invoice.json --output invoice.pdf
    python invoice_generator.py sample > sample_invoice.json
"""

import json
import sys
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from jinja2 import Template
from rich.console import Console

console = Console()

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Invoice {{ invoice_number }}</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Helvetica Neue', Arial, sans-serif; color: #333; padding: 40px; }
  .header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 40px; border-bottom: 3px solid {{ brand_color }}; padding-bottom: 20px; }
  .brand h1 { font-size: 28px; color: {{ brand_color }}; }
  .brand p { color: #666; font-size: 13px; margin-top: 4px; }
  .invoice-meta { text-align: right; }
  .invoice-meta h2 { font-size: 36px; color: {{ brand_color }}; text-transform: uppercase; }
  .invoice-meta p { font-size: 13px; color: #666; margin-top: 2px; }
  .parties { display: flex; justify-content: space-between; margin-bottom: 30px; }
  .party { width: 45%; }
  .party h3 { font-size: 12px; text-transform: uppercase; color: {{ brand_color }}; margin-bottom: 8px; letter-spacing: 1px; }
  .party p { font-size: 14px; line-height: 1.6; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 30px; }
  th { background: {{ brand_color }}; color: #fff; padding: 12px 15px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 12px 15px; border-bottom: 1px solid #eee; font-size: 14px; }
  tr:nth-child(even) { background: #f9f9f9; }
  .text-right { text-align: right; }
  .totals { width: 300px; margin-left: auto; }
  .totals table { margin-bottom: 0; }
  .totals td { padding: 8px 15px; }
  .totals tr:last-child { border-top: 2px solid {{ brand_color }}; font-weight: bold; font-size: 16px; }
  .footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; font-size: 12px; color: #999; text-align: center; }
  @media print { body { padding: 0; } }
</style>
</head>
<body>
<div class="header">
  <div class="brand">
    <h1>{{ company.name }}</h1>
    <p>{{ company.address }}</p>
    <p>{{ company.email }}{% if company.phone %} | {{ company.phone }}{% endif %}</p>
    {% if company.website %}<p>{{ company.website }}</p>{% endif %}
  </div>
  <div class="invoice-meta">
    <h2>Invoice</h2>
    <p><strong>#{{ invoice_number }}</strong></p>
    <p>Date: {{ issue_date }}</p>
    <p>Due: {{ due_date }}</p>
  </div>
</div>
<div class="parties">
  <div class="party">
    <h3>Bill To</h3>
    <p><strong>{{ client.name }}</strong></p>
    <p>{{ client.address }}</p>
    <p>{{ client.email }}</p>
  </div>
  <div class="party">
    <h3>Payment Details</h3>
    {% if payment.bank_name %}<p>Bank: {{ payment.bank_name }}</p>{% endif %}
    {% if payment.account_number %}<p>Account: {{ payment.account_number }}</p>{% endif %}
    {% if payment.routing_number %}<p>Routing: {{ payment.routing_number }}</p>{% endif %}
    {% if payment.notes %}<p>{{ payment.notes }}</p>{% endif %}
  </div>
</div>
<table>
  <thead>
    <tr><th>Description</th><th class="text-right">Qty</th><th class="text-right">Rate</th><th class="text-right">Amount</th></tr>
  </thead>
  <tbody>
    {% for item in items %}
    <tr>
      <td>{{ item.description }}</td>
      <td class="text-right">{{ item.quantity }}</td>
      <td class="text-right">${{ "%.2f"|format(item.rate) }}</td>
      <td class="text-right">${{ "%.2f"|format(item.quantity * item.rate) }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
<div class="totals">
  <table>
    <tr><td>Subtotal</td><td class="text-right">${{ "%.2f"|format(subtotal) }}</td></tr>
    {% if tax_rate > 0 %}
    <tr><td>Tax ({{ "%.1f"|format(tax_rate) }}%)</td><td class="text-right">${{ "%.2f"|format(tax_amount) }}</td></tr>
    {% endif %}
    {% if discount > 0 %}
    <tr><td>Discount</td><td class="text-right">-${{ "%.2f"|format(discount) }}</td></tr>
    {% endif %}
    <tr><td>Total Due</td><td class="text-right">${{ "%.2f"|format(total) }}</td></tr>
  </table>
</div>
{% if notes %}
<div style="margin-top:30px; padding:15px; background:#f9f9f9; border-radius:4px;">
  <h3 style="margin-bottom:8px;">Notes</h3>
  <p style="font-size:14px; line-height:1.6;">{{ notes }}</p>
</div>
{% endif %}
<div class="footer">
  <p>Thank you for your business! | {{ company.name }}</p>
</div>
</body>
</html>"""


def _coalesce(data: dict, *keys, default=None):
    """Return first truthy value among keys in data dict."""
    for k in keys:
        v = data.get(k)
        if v:
            return v
    return default


def _build_context(data: dict) -> dict:
    """Build template context from invoice JSON data."""
    company = data.get("company", {})
    client = data.get("client", {})
    items = data.get("items", [])
    payment = data.get("payment", {})

    subtotal = sum(it["quantity"] * it["rate"] for it in items)
    tax_rate = float(data.get("tax_rate", 0))
    discount = float(data.get("discount", 0))
    tax_amount = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax_amount - discount, 2)

    return {
        "invoice_number": _coalesce(data, "invoice_number", "number", default="INV-001"),
        "issue_date": _coalesce(data, "issue_date", "date", default=datetime.now().strftime("%Y-%m-%d")),
        "due_date": _coalesce(data, "due_date", default=""),
        "brand_color": data.get("brand_color", "#2563eb"),
        "company": {
            "name": company.get("name", "Your Company"),
            "address": company.get("address", ""),
            "email": company.get("email", ""),
            "phone": company.get("phone", ""),
            "website": company.get("website", ""),
        },
        "client": {
            "name": client.get("name", "Client Name"),
            "address": client.get("address", ""),
            "email": client.get("email", ""),
        },
        "payment": {
            "bank_name": payment.get("bank_name", ""),
            "account_number": payment.get("account_number", ""),
            "routing_number": payment.get("routing_number", ""),
            "notes": payment.get("notes", ""),
        },
        "items": items,
        "subtotal": subtotal,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "discount": discount,
        "total": total,
        "notes": data.get("notes", ""),
    }


def generate_html(data: dict) -> str:
    """Render invoice HTML from data dict."""
    ctx = _build_context(data)
    return Template(HTML_TEMPLATE).render(**ctx)


def _try_pdf_convert(html: str, output: str) -> bool:
    """Try multiple PDF backends; return True if successful."""
    # Try weasyprint
    try:
        from weasyprint import HTML  # type: ignore
        HTML(string=html).write_pdf(output)
        return True
    except ImportError:
        pass

    # Try pdfkit (wkhtmltopdf)
    try:
        import pdfkit  # type: ignore
        pdfkit.from_string(html, output)
        return True
    except (ImportError, Exception):
        pass

    return False


@click.group()
def cli():
    """📄 Invoice Generator — create professional PDF invoices from JSON."""


@cli.command()
@click.option("--input", "-i", "input_file", required=True, type=click.Path(exists=True), help="Invoice JSON file.")
@click.option("--output", "-o", default="invoice.pdf", help="Output PDF/HTML file path.")
@click.option("--preview", is_flag=True, help="Open HTML preview in browser instead of PDF.")
def generate(input_file: str, output: str, preview: bool):
    """Generate an invoice PDF from a JSON file."""
    with open(input_file) as f:
        data = json.load(f)

    html = generate_html(data)

    if preview:
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as tmp:
            tmp.write(html)
            tmp_path = tmp.name
        webbrowser.open(f"file://{tmp_path}")
        console.print(f"[green]✓[/green] Preview opened in browser: {tmp_path}")
        return

    if output.endswith(".pdf"):
        if _try_pdf_convert(html, output):
            console.print(f"[green]✓[/green] PDF invoice generated: [cyan]{output}[/cyan]")
        else:
            html_fallback = output.replace(".pdf", ".html")
            Path(html_fallback).write_text(html)
            console.print("[yellow]⚠[/yellow] No PDF backend found (install weasyprint or pdfkit).")
            console.print(f"  HTML fallback saved: [cyan]{html_fallback}[/cyan]")
            console.print("  Install: [dim]pip install weasyprint[/dim]")
    else:
        Path(output).write_text(html)
        console.print(f"[green]✓[/green] HTML invoice generated: [cyan]{output}[/cyan]")

    # Print summary
    ctx = _build_context(data)
    console.print(f"  Invoice: [bold]{ctx['invoice_number']}[/bold]")
    console.print(f"  Client:  {ctx['client']['name']}")
    console.print(f"  Total:   [green]${ctx['total']:.2f}[/green] ({len(data.get('items', []))} items)")


@cli.command()
def sample():
    """Print a sample invoice JSON to stdout."""
    sample_data = {
        "invoice_number": "INV-2024-001",
        "issue_date": "2024-01-15",
        "due_date": "2024-02-15",
        "brand_color": "#2563eb",
        "company": {
            "name": "Acme Solutions",
            "address": "123 Business Ave, Suite 100, San Francisco, CA 94102",
            "email": "billing@acme.com",
            "phone": "(555) 123-4567",
            "website": "https://acme.com",
        },
        "client": {
            "name": "Widget Corp",
            "address": "456 Client St, New York, NY 10001",
            "email": "accounts@widgetcorp.com",
        },
        "payment": {
            "bank_name": "First National Bank",
            "account_number": "****1234",
            "routing_number": "021000021",
        },
        "items": [
            {"description": "Website Design & Development", "quantity": 1, "rate": 5000.00},
            {"description": "SEO Optimization", "quantity": 1, "rate": 1200.00},
            {"description": "Content Writing (per page)", "quantity": 10, "rate": 150.00},
            {"description": "Monthly Hosting (12 months)", "quantity": 12, "rate": 29.99},
        ],
        "tax_rate": 8.5,
        "discount": 200.00,
        "notes": "Payment due within 30 days. Thank you for your business!",
    }
    click.echo(json.dumps(sample_data, indent=2))


if __name__ == "__main__":
    cli()
