#!/usr/bin/env python3
"""Monitor cryptocurrency prices via CoinGecko API and trigger desktop alerts.

Usage:
    python crypto_alerter.py add --coin bitcoin --above 70000
    python crypto_alerter.py add --coin ethereum --below 2000
    python crypto_alerter.py watch --interval 60
    python crypto_alerter.py prices
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
import requests
from rich.console import Console
from rich.table import Table

console = Console()
ALERTS_FILE = Path(__file__).parent / "crypto_alerts.json"
API_BASE = "https://api.coingecko.com/api/v3"


def load_alerts() -> dict:
    """Load alerts from JSON file."""
    if ALERTS_FILE.exists():
        return json.loads(ALERTS_FILE.read_text())
    return {"alerts": []}


def save_alerts(data: dict) -> None:
    """Save alerts to JSON file."""
    ALERTS_FILE.write_text(json.dumps(data, indent=2))


def fetch_prices(coin_ids: list[str], currency: str = "usd") -> dict:
    """Fetch current prices from CoinGecko."""
    ids = ",".join(coin_ids)
    try:
        resp = requests.get(
            f"{API_BASE}/simple/price",
            params={"ids": ids, "vs_currencies": currency, "include_24hr_change": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        console.print(f"[red]API Error: {e}[/red]")
        return {}


def search_coin(query: str) -> Optional[dict]:
    """Search for a coin by name/symbol on CoinGecko."""
    try:
        resp = requests.get(f"{API_BASE}/search", params={"query": query}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        coins = data.get("coins", [])
        if coins:
            c = coins[0]
            return {"id": c["id"], "name": c["name"], "symbol": c["symbol"].upper()}
    except requests.RequestException:
        pass
    return None


def notify_desktop(title: str, message: str) -> None:
    """Send a desktop notification (macOS/Linux)."""
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                check=False, capture_output=True,
            )
        elif sys.platform == "linux":
            subprocess.run(["notify-send", title, message], check=False, capture_output=True)
        else:
            console.print(f"[yellow]⚠ Desktop notifications not supported on {sys.platform}[/yellow]")
    except FileNotFoundError:
        pass


@click.group()
def cli():
    """🪙 Crypto Price Alerter — monitor prices and get notified on triggers."""


@cli.command()
@click.option("--coin", "-c", required=True, help="Coin name or symbol (e.g. bitcoin, ETH).")
@click.option("--above", type=float, default=None, help="Alert when price goes above this value.")
@click.option("--below", type=float, default=None, help="Alert when price goes below this value.")
@click.option("--currency", default="usd", help="Currency for price (default: usd).")
def add(coin: str, above: Optional[float], below: Optional[float], currency: str):
    """Add a price alert for a cryptocurrency."""
    if above is None and below is None:
        console.print("[red]Specify --above and/or --below for the alert.[/red]")
        return

    # Resolve coin ID
    result = search_coin(coin)
    if not result:
        console.print(f"[red]Could not find coin: {coin}[/red]")
        return

    coin_id = result["id"]
    console.print(f"Found: [cyan]{result['name']}[/cyan] ({result['symbol']}) → {coin_id}")

    data = load_alerts()
    alert = {
        "coin_id": coin_id,
        "coin_name": result["name"],
        "symbol": result["symbol"],
        "above": above,
        "below": below,
        "currency": currency,
        "triggered": False,
        "created": datetime.now().isoformat(),
    }
    data["alerts"].append(alert)
    save_alerts(data)

    parts = []
    if above:
        parts.append(f"above ${above:,.2f}")
    if below:
        parts.append(f"below ${below:,.2f}")
    console.print(f"[green]✓[/green] Alert added: {result['name']} {' and '.join(parts)} ({currency})")


@cli.command("list")
def list_alerts():
    """List all active alerts."""
    data = load_alerts()
    alerts = data.get("alerts", [])
    if not alerts:
        console.print("[yellow]No alerts configured. Use 'add' to create one.[/yellow]")
        return

    table = Table(title="Price Alerts")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Coin", style="cyan")
    table.add_column("Symbol", style="magenta")
    table.add_column("Above", justify="right", style="green")
    table.add_column("Below", justify="right", style="red")
    table.add_column("Currency")
    table.add_column("Status")

    for i, a in enumerate(alerts, 1):
        status = "[yellow]triggered[/yellow]" if a.get("triggered") else "[green]active[/green]"
        table.add_row(
            str(i),
            a["coin_name"],
            a["symbol"],
            f"${a['above']:,.2f}" if a.get("above") else "—",
            f"${a['below']:,.2f}" if a.get("below") else "—",
            a.get("currency", "usd").upper(),
            status,
        )
    console.print(table)


@cli.command()
@click.argument("index", type=int)
def remove(index: int):
    """Remove an alert by its number."""
    data = load_alerts()
    alerts = data.get("alerts", [])
    if index < 1 or index > len(alerts):
        console.print(f"[red]Invalid alert number: {index}[/red]")
        return
    removed = alerts.pop(index - 1)
    save_alerts(data)
    console.print(f"[green]✓[/green] Removed alert for {removed['coin_name']}")


@cli.command()
def prices():
    """Show current prices for all tracked coins."""
    data = load_alerts()
    alerts = data.get("alerts", [])
    if not alerts:
        console.print("[yellow]No alerts configured. Use 'add' to start tracking.[/yellow]")
        return

    coin_ids = list({a["coin_id"] for a in alerts})
    prices_map = fetch_prices(coin_ids)
    if not prices_map:
        return

    table = Table(title="Crypto Prices")
    table.add_column("Coin", style="cyan")
    table.add_column("Symbol", style="magenta")
    table.add_column("Price", justify="right", style="bold green")
    table.add_column("24h Change", justify="right")
    table.add_column("Alerts", style="yellow")

    for coin_id in coin_ids:
        p = prices_map.get(coin_id, {})
        price = p.get("usd", 0)
        change = p.get("usd_24h_change", 0) or 0
        change_str = f"+{change:.2f}%" if change >= 0 else f"{change:.2f}%"
        change_style = "green" if change >= 0 else "red"

        # Find alerts for this coin
        coin_alerts = [a for a in alerts if a["coin_id"] == coin_id]
        alert_parts = []
        for a in coin_alerts:
            if a.get("above"):
                alert_parts.append(f"↑${a['above']:,.0f}")
            if a.get("below"):
                alert_parts.append(f"↓${a['below']:,.0f}")

        coin_info = next((a for a in alerts if a["coin_id"] == coin_id), {})
        table.add_row(
            coin_info.get("coin_name", coin_id),
            coin_info.get("symbol", "?"),
            f"${price:,.2f}",
            f"[{change_style}]{change_str}[/{change_style}]",
            " ".join(alert_parts),
        )
    console.print(table)


@cli.command()
@click.option("--interval", "-i", default=300, help="Check interval in seconds (default: 300).")
@click.option("--once", is_flag=True, help="Check once and exit.")
def watch(interval: int, once: bool):
    """Watch prices and trigger alerts."""
    data = load_alerts()
    alerts = data.get("alerts", [])
    active_alerts = [a for a in alerts if not a.get("triggered")]
    if not active_alerts:
        console.print("[yellow]No active alerts to watch.[/yellow]")
        return

    coin_ids = list({a["coin_id"] for a in active_alerts})
    console.print(f"[cyan]Watching {len(active_alerts)} alerts across {len(coin_ids)} coins...[/cyan]")
    console.print(f"[dim]Interval: {interval}s | Press Ctrl+C to stop[/dim]\n")

    while True:
        prices_map = fetch_prices(coin_ids)
        if not prices_map:
            if once:
                return
            time.sleep(interval)
            continue

        now = datetime.now().strftime("%H:%M:%S")
        for alert in alerts:
            if alert.get("triggered"):
                continue
            p = prices_map.get(alert["coin_id"], {})
            price = p.get("usd", 0)
            if not price:
                continue

            triggered = False
            reasons = []
            if alert.get("above") and price >= alert["above"]:
                triggered = True
                reasons.append(f"${price:,.2f} ≥ ${alert['above']:,.2f}")
            if alert.get("below") and price <= alert["below"]:
                triggered = True
                reasons.append(f"${price:,.2f} ≤ ${alert['below']:,.2f}")

            if triggered:
                alert["triggered"] = True
                alert["triggered_at"] = datetime.now().isoformat()
                alert["triggered_price"] = price
                title = f"🚨 {alert['coin_name']} Alert!"
                msg = " & ".join(reasons)
                console.print(f"[bold red]{title}[/bold red] {msg}")
                notify_desktop(title, msg)
                save_alerts(data)

        # Print status line
        active = sum(1 for a in alerts if not a.get("triggered"))
        if active == 0:
            console.print("[green]All alerts triggered! Exiting.[/green]")
            return

        console.print(f"[dim][{now}] {active} alerts active[/dim]")
        if once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    cli()
