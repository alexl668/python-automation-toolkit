#!/usr/bin/env python3
"""JSON Pipeline — Process, filter, transform, merge, and flatten JSON data.

A standalone CLI for JSON data processing pipelines with JSONPath-like queries,
nested structure flattening, merging, filtering, and multi-format output.

Usage examples:
    python json_pipeline.py filter data.json --query "users[*].age > 25"
    python json_pipeline.py flatten nested.json --separator "__"
    python json_pipeline.py merge a.json b.json c.json
    python json_pipeline.py transform data.json --set "status=active" --remove "temp_field"
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Any:
    """Load and return parsed JSON from a file."""
    p = Path(path)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {path}")
        raise SystemExit(1)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        console.print(f"[green]✓[/green] Loaded [cyan]{p.name}[/cyan]")
        return data
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid JSON:[/red] {exc}")
        raise SystemExit(1)


def _write_json(data: Any, output: str | None, indent: int = 2) -> None:
    """Write JSON data to file or stdout."""
    if output is None:
        console.print_json(json.dumps(data, indent=indent, ensure_ascii=False, default=str))
        return
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False, default=str)
    console.print(f"[green]✓[/green] Written to [cyan]{out}[/cyan]")


def _write_csv_from_list(records: list[dict], output: str | None) -> None:
    """Convert a list of dicts to CSV."""
    import pandas as pd
    df = pd.DataFrame(records)
    if output:
        df.to_csv(output, index=False)
        console.print(f"[green]✓[/green] Written to [cyan]{output}[/cyan]")
    else:
        console.print(df.to_csv(index=False))


# ---------------------------------------------------------------------------
# JSONPath-lite query engine
# ---------------------------------------------------------------------------

def _resolve_path(obj: Any, path: str) -> list[Any]:
    """Resolve a simple JSONPath-like expression against obj.

    Supports:
        .key          — object key access
        [*]           — iterate all array elements
        [N]           — array index
        .key.subkey   — chained access
    """
    parts = _tokenize_path(path)
    results = [obj]
    for part in parts:
        new_results: list[Any] = []
        for item in results:
            if part == "[*]":
                if isinstance(item, list):
                    new_results.extend(item)
            elif part.startswith("[") and part.endswith("]"):
                idx = int(part[1:-1])
                if isinstance(item, list) and 0 <= idx < len(item):
                    new_results.append(item[idx])
            elif isinstance(item, dict) and part in item:
                new_results.append(item[part])
        results = new_results
    return results


def _tokenize_path(path: str) -> list[str]:
    """Tokenize a dotted JSONPath expression into parts."""
    tokens: list[str] = []
    for segment in path.split("."):
        segment = segment.strip()
        if not segment:
            continue
        # Handle bracket notation: key[0] or key[*]
        bracket_match = re.match(r"^(\w+)(\[.*\])$", segment)
        if bracket_match:
            tokens.append(bracket_match.group(1))
            tokens.append(bracket_match.group(2))
        else:
            tokens.append(segment)
    return tokens


def _set_nested(obj: dict, path: str, value: Any) -> None:
    """Set a value at a dotted path in a dict."""
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    # Try to parse JSON-like values
    try:
        value = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        pass
    current[parts[-1]] = value


def _del_nested(obj: dict, path: str) -> None:
    """Delete a key at a dotted path in a dict."""
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


# ---------------------------------------------------------------------------
# Flatten nested structures
# ---------------------------------------------------------------------------

def _flatten(obj: Any, parent_key: str = "", sep: str = ".") -> dict:
    """Recursively flatten a nested dict/list into a single-level dict."""
    items: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            items.extend(_flatten(v, new_key, sep).items())
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            new_key = f"{parent_key}{sep}{i}" if parent_key else str(i)
            items.extend(_flatten(v, new_key, sep).items())
    else:
        items.append((parent_key, obj))
    return dict(items)


def _unflatten(flat: dict, sep: str = ".") -> dict:
    """Unflatten a dot-separated dict back into nested structure."""
    result: dict = {}
    for key, value in flat.items():
        parts = key.split(sep)
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option("1.0.0", prog_name="json_pipeline")
def cli() -> None:
    """JSON Pipeline — process, filter, transform, and flatten JSON data."""


@cli.command()
@click.argument("input_file")
@click.option("--query", "-q", required=True, help="JSONPath-like query (e.g. 'users[*].name').")
@click.option("--output", "-o", default=None, help="Output file path.")
def query(input_file: str, query: str, output: str | None) -> None:
    """Query JSON data using JSONPath-like expressions."""
    data = _load_json(input_file)
    results = _resolve_path(data, query)
    console.print(f"[blue]Query:[/blue] {query} → {len(results)} result(s)")
    _write_json(results if len(results) != 1 else results[0], output)


@cli.command()
@click.argument("input_file")
@click.option("--query", "-q", required=True, help="JSONPath to the array to filter.")
@click.option("--where", "-w", required=True, help="Filter condition: 'field op value' (op: ==, !=, >, <, >=, <=, contains, startswith).")
@click.option("--output", "-o", default=None, help="Output file path.")
def filter_items(input_file: str, query: str, where: str, output: str | None) -> None:
    """Filter array elements by condition."""
    data = _load_json(input_file)
    items = _resolve_path(data, query)
    if not items or not isinstance(items[0], list):
        console.print("[red]Error:[/red] Query did not resolve to an array.")
        raise SystemExit(1)

    arr = items[0]
    # Parse condition
    match = re.match(r"(\w+)\s*(==|!=|>=|<=|>|<|contains|startswith)\s*(.+)", where.strip())
    if not match:
        console.print(f"[red]Invalid filter syntax:[/red] {where}")
        raise SystemExit(1)

    field, op, raw_val = match.group(1), match.group(2), match.group(3).strip().strip("'\"")

    # Type coercion
    try:
        val: Any = json.loads(raw_val)
    except (json.JSONDecodeError, TypeError):
        val = raw_val

    def _check(item: dict) -> bool:
        v = item.get(field)
        if v is None:
            return False
        try:
            if op == "==":
                return v == val
            if op == "!=":
                return v != val
            if op == ">":
                return float(v) > float(val)
            if op == "<":
                return float(v) < float(val)
            if op == ">=":
                return float(v) >= float(val)
            if op == "<=":
                return float(v) <= float(val)
            if op == "contains":
                return str(val) in str(v)
            if op == "startswith":
                return str(v).startswith(str(val))
        except (TypeError, ValueError):
            return False
        return False

    filtered = [item for item in arr if isinstance(item, dict) and _check(item)]
    console.print(f"[blue]Filtered:[/blue] {len(arr)} → {len(filtered)} items")
    _write_json(filtered, output)


@cli.command()
@click.argument("input_file")
@click.option("--set", "setters", multiple=True, help="Set field: 'path=value'. Repeatable.")
@click.option("--remove", "removers", multiple=True, help="Remove field by path. Repeatable.")
@click.option("--rename", "-r", multiple=True, help="Rename field: 'old_path:new_path'. Repeatable.")
@click.option("--output", "-o", default=None, help="Output file path.")
def transform(input_file: str, setters: tuple[str, ...], removers: tuple[str, ...], rename: tuple[str, ...], output: str | None) -> None:
    """Transform JSON data by setting, removing, or renaming fields."""
    data = _load_json(input_file)

    # Work on top-level dict or list of dicts
    items = data if isinstance(data, list) else [data]

    for item in items:
        if not isinstance(item, dict):
            continue
        # Set fields
        for s in setters:
            if "=" not in s:
                console.print(f"[yellow]Warning:[/yellow] Malformed setter: {s}")
                continue
            path, val = s.split("=", 1)
            _set_nested(item, path.strip(), val)
        # Remove fields
        for r in removers:
            _del_nested(item, r.strip())
        # Rename fields
        for rn in rename:
            if ":" not in rn:
                console.print(f"[yellow]Warning:[/yellow] Malformed rename: {rn}")
                continue
            old_path, new_path = rn.split(":", 1)
            parts_old = old_path.strip().split(".")
            parts_new = new_path.strip().split(".")
            # Navigate to parent
            src = item
            for p in parts_old[:-1]:
                src = src.get(p, {})
            val = src.pop(parts_old[-1], None) if isinstance(src, dict) else None
            if val is not None:
                _set_nested(item, new_path.strip(), json.dumps(val) if not isinstance(val, str) else val)

    result = data if not isinstance(data, list) else items
    _write_json(result, output)


@cli.command()
@click.argument("files", nargs=-1, required=True)
@click.option("--strategy", "-s", default="concat", type=click.Choice(["concat", "merge_deep"]), help="Merge strategy.")
@click.option("--output", "-o", default=None, help="Output file path.")
def merge(files: tuple[str, ...], strategy: str, output: str | None) -> None:
    """Merge multiple JSON files."""
    if len(files) < 2:
        console.print("[red]Error:[/red] Need at least 2 files.")
        raise SystemExit(1)

    datas = [_load_json(f) for f in files]

    if strategy == "concat":
        # If all are lists, concatenate; otherwise wrap in list
        if all(isinstance(d, list) for d in datas):
            result: Any = []
            for d in datas:
                result.extend(d)
        else:
            result = datas
    else:
        # Deep merge dicts
        def _deep_merge(a: dict, b: dict) -> dict:
            merged = a.copy()
            for k, v in b.items():
                if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                    merged[k] = _deep_merge(merged[k], v)
                else:
                    merged[k] = v
            return merged

        result = datas[0]
        for d in datas[1:]:
            if isinstance(result, dict) and isinstance(d, dict):
                result = _deep_merge(result, d)
            else:
                console.print("[yellow]Warning:[/yellow] Deep merge requires all inputs to be objects. Falling back to concat.")
                result = [result, d]
                break

    count = len(result) if isinstance(result, list) else 1
    console.print(f"[green]Merged:[/green] {len(files)} files → {count} items")
    _write_json(result, output)


@cli.command()
@click.argument("input_file")
@click.option("--separator", "-s", default=".", help="Separator for flattened keys.")
@click.option("--output", "-o", default=None, help="Output file path.")
@click.option("--to-csv", is_flag=True, help="Output as CSV (for flat records).")
def flatten(input_file: str, separator: str, output: str | None, to_csv: bool) -> None:
    """Flatten nested JSON into dot-notation keys."""
    data = _load_json(input_file)

    if isinstance(data, list):
        result = [_flatten(item, sep=separator) for item in data]
    else:
        result = _flatten(data, sep=separator)

    if to_csv and isinstance(result, list):
        _write_csv_from_list(result, output)
    else:
        _write_json(result, output)


@cli.command()
@click.argument("input_file")
@click.option("--keys", "-k", multiple=True, help="Top-level keys to keep. Repeatable.")
@click.option("--output", "-o", default=None, help="Output file path.")
def extract(input_file: str, keys: tuple[str, ...], output: str | None) -> None:
    """Extract specific top-level keys from JSON."""
    data = _load_json(input_file)
    if not isinstance(data, dict):
        console.print("[red]Error:[/red] Top-level value must be an object.")
        raise SystemExit(1)

    result = {k: data[k] for k in keys if k in data}
    missing = [k for k in keys if k not in data]
    if missing:
        console.print(f"[yellow]Missing keys:[/yellow] {missing}")
    console.print(f"[blue]Extracted {len(result)} keys[/blue]")
    _write_json(result, output)


@cli.command()
@click.argument("input_file")
@click.option("--indent", "-i", type=int, default=2, help="JSON indentation.")
def stats(input_file: str, indent: int) -> None:
    """Show structural statistics of a JSON file."""
    data = _load_json(input_file)

    def _describe(obj: Any, depth: int = 0) -> dict[str, Any]:
        if isinstance(obj, dict):
            return {
                "type": "object",
                "keys": len(obj),
                "children": {k: _describe(v, depth + 1) for k, v in list(obj.items())[:20]},
            }
        elif isinstance(obj, list):
            return {
                "type": "array",
                "length": len(obj),
                "item_type": type(obj[0]).__name__ if obj else "empty",
            }
        else:
            return {"type": type(obj).__name__, "value": str(obj)[:80]}

    desc = _describe(data)
    table = Table(title="JSON Structure", box=box.ROUNDED)
    table.add_column("Key", style="cyan")
    table.add_column("Type")
    table.add_column("Info")

    def _add_rows(d: dict, prefix: str = "") -> None:
        if d.get("type") == "object" and "children" in d:
            for k, v in d["children"].items():
                if v.get("type") == "object":
                    table.add_row(f"{prefix}{k}", "object", f"{v.get('keys', '?')} keys")
                    _add_rows(v, f"  {prefix}{k}.")
                elif v.get("type") == "array":
                    table.add_row(f"{prefix}{k}", "array", f"{v.get('length', '?')} items ({v.get('item_type', '?')})")
                else:
                    table.add_row(f"{prefix}{k}", v.get("type", "?"), v.get("value", "—"))

    _add_rows(desc)
    console.print(table)


if __name__ == "__main__":
    cli()
