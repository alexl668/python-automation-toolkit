#!/usr/bin/env python3
"""Data Validator — Validate CSV and JSON files against schemas.

Checks types, required fields, value ranges, regex patterns, enums, and
custom constraints. Reports every violation with row/field location.

Usage examples:
    python data_validator.py validate data.csv --schema schema.json
    python data_validator.py validate data.json --schema schema.json --strict
    python data_validator.py init-schema data.csv --output schema.json
    python data_validator.py validate data.csv --required id,name --types "id:int,name:str" --range "age:0-150"
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import click
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()


# ---------------------------------------------------------------------------
# Schema format
# ---------------------------------------------------------------------------

# Schema JSON example:
# {
#   "fields": {
#     "id":    {"type": "int",    "required": true, "unique": true},
#     "name":  {"type": "str",    "required": true, "min_length": 1, "max_length": 100},
#     "age":   {"type": "int",    "required": false, "min": 0, "max": 150},
#     "email": {"type": "str",    "required": true, "pattern": "^[\\w.+-]+@[\\w-]+\\.[\\w.]+$"},
#     "role":  {"type": "str",    "required": false, "enum": ["admin", "user", "guest"]}
#   },
#   "no_extra_fields": false
# }


TYPE_MAP = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": float,
    "bool": bool,
    "boolean": bool,
}


def _load_schema(path: str) -> dict:
    """Load a validation schema from JSON."""
    p = Path(path)
    if not p.exists():
        console.print(f"[red]Error:[/red] Schema not found: {path}")
        raise SystemExit(1)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _check_type(value: Any, expected: str) -> bool:
    """Check if value matches the expected type string."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return True  # nulls handled separately
    target = TYPE_MAP.get(expected.lower())
    if target is None:
        return True  # unknown type, skip
    if target is int:
        if isinstance(value, bool):
            return False
        try:
            int(value)
            return True
        except (ValueError, TypeError):
            return False
    if target is float:
        if isinstance(value, bool):
            return False
        try:
            float(value)
            return True
        except (ValueError, TypeError):
            return False
    if target is bool:
        if isinstance(value, bool):
            return True
        return str(value).lower() in ("true", "false", "1", "0", "yes", "no")
    return isinstance(value, target)


# ---------------------------------------------------------------------------
# Validation engine
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    """A single schema violation."""
    row: int | str  # 1-based row number or "header"
    field: str
    rule: str
    message: str
    value: Any = None


def _validate_field(
    field_name: str,
    value: Any,
    rules: dict,
    row_num: int | str,
) -> list[Violation]:
    """Validate a single field value against its rules. Returns violations."""
    violations: list[Violation] = []
    is_null = value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == ""

    # Required
    if rules.get("required") and is_null:
        violations.append(Violation(row_num, field_name, "required", "Value is required but missing"))
        return violations  # no point checking further

    if is_null:
        return violations  # optional and missing — fine

    # Type
    expected_type = rules.get("type")
    if expected_type and not _check_type(value, expected_type):
        violations.append(Violation(row_num, field_name, "type", f"Expected {expected_type}, got {type(value).__name__}: {value!r}", value))
        return violations  # type mismatch makes further checks unreliable

    # Coerce for numeric checks
    num_val = None
    if expected_type and expected_type.lower() in ("int", "integer", "float", "number"):
        try:
            num_val = float(value)
        except (ValueError, TypeError):
            pass

    # Min / Max
    if num_val is not None:
        if "min" in rules and num_val < rules["min"]:
            violations.append(Violation(row_num, field_name, "min", f"Value {num_val} < minimum {rules['min']}", value))
        if "max" in rules and num_val > rules["max"]:
            violations.append(Violation(row_num, field_name, "max", f"Value {num_val} > maximum {rules['max']}", value))

    # String length
    str_val = str(value)
    if "min_length" in rules and len(str_val) < rules["min_length"]:
        violations.append(Violation(row_num, field_name, "min_length", f"Length {len(str_val)} < {rules['min_length']}", value))
    if "max_length" in rules and len(str_val) > rules["max_length"]:
        violations.append(Violation(row_num, field_name, "max_length", f"Length {len(str_val)} > {rules['max_length']}", value))

    # Pattern
    if "pattern" in rules:
        try:
            if not re.match(rules["pattern"], str_val):
                violations.append(Violation(row_num, field_name, "pattern", f"Value does not match pattern: {rules['pattern']}", value))
        except re.error:
            violations.append(Violation(row_num, field_name, "pattern", f"Invalid regex in schema: {rules['pattern']}", value))

    # Enum
    if "enum" in rules:
        allowed = rules["enum"]
        if value not in allowed and str(value) not in [str(a) for a in allowed]:
            violations.append(Violation(row_num, field_name, "enum", f"Value '{value}' not in allowed set: {allowed}", value))

    return violations


def _validate_dataframe(df: pd.DataFrame, schema: dict, strict: bool = False) -> list[Violation]:
    """Validate all rows in a DataFrame against the schema."""
    violations: list[Violation] = []
    fields = schema.get("fields", {})

    # Check for extra columns
    if strict or schema.get("no_extra_fields"):
        extra = set(df.columns) - set(fields.keys())
        if extra:
            for col in extra:
                violations.append(Violation("header", col, "extra_field", f"Unexpected column not in schema: {col}"))

    # Check for missing required columns
    for fname, rules in fields.items():
        if fname not in df.columns:
            if rules.get("required"):
                violations.append(Violation("header", fname, "missing_column", f"Required column missing from data"))
            continue

    # Validate each row
    for idx, row in df.iterrows():
        row_num = idx + 2  # 1-based + header row
        for fname, rules in fields.items():
            if fname not in df.columns:
                continue
            value = row[fname]
            violations.extend(_validate_field(fname, value, rules, row_num))

    # Uniqueness check
    for fname, rules in fields.items():
        if rules.get("unique") and fname in df.columns:
            dupes = df[fname][df[fname].duplicated(keep=False) & df[fname].notna()]
            for val in dupes.unique():
                rows = [str(i + 2) for i in df[df[fname] == val].index.tolist()]
                violations.append(Violation(",".join(rows), fname, "unique", f"Duplicate value: {val}"))

    return violations


def _validate_json_records(records: list[dict], schema: dict, strict: bool = False) -> list[Violation]:
    """Validate a list of JSON objects against the schema."""
    violations: list[Violation] = []
    fields = schema.get("fields", {})

    for i, record in enumerate(records):
        row_num = i + 1
        if not isinstance(record, dict):
            violations.append(Violation(row_num, "(root)", "type", f"Expected object, got {type(record).__name__}"))
            continue

        # Extra fields
        if strict or schema.get("no_extra_fields"):
            extra = set(record.keys()) - set(fields.keys())
            for col in extra:
                violations.append(Violation(row_num, col, "extra_field", f"Unexpected field"))

        for fname, rules in fields.items():
            value = record.get(fname)
            if value is None and rules.get("required"):
                violations.append(Violation(row_num, fname, "required", "Required field missing"))
                continue
            if value is not None:
                violations.extend(_validate_field(fname, value, rules, row_num))

        # Uniqueness across records — handled after loop for efficiency
    # Cross-record uniqueness
    for fname, rules in fields.items():
        if rules.get("unique"):
            values = [r.get(fname) for r in records if isinstance(r, dict)]
            seen: dict[Any, list[int]] = {}
            for i, v in enumerate(values):
                if v is not None:
                    seen.setdefault(v, []).append(i + 1)
            for val, rows in seen.items():
                if len(rows) > 1:
                    violations.append(Violation(",".join(str(r) for r in rows), fname, "unique", f"Duplicate value: {val}"))

    return violations


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _build_schema_from_cli(
    required: tuple[str, ...],
    types: tuple[str, ...],
    ranges: tuple[str, ...],
    patterns: tuple[str, ...],
    enums: tuple[str, ...],
    unique: tuple[str, ...],
) -> dict:
    """Build a schema dict from CLI options."""
    fields: dict[str, dict] = {}

    # Parse --types
    for item in types:
        if ":" not in item:
            continue
        name, typ = item.split(":", 1)
        fields.setdefault(name.strip(), {})["type"] = typ.strip()

    # Parse --required
    for name in required:
        fields.setdefault(name.strip(), {})["required"] = True

    # Parse --range
    for item in ranges:
        if ":" not in item:
            continue
        name, rng = item.split(":", 1)
        if "-" in rng:
            lo, hi = rng.split("-", 1)
            f = fields.setdefault(name.strip(), {})
            try:
                f["min"] = float(lo)
                f["max"] = float(hi)
            except ValueError:
                console.print(f"[yellow]Warning:[/yellow] Invalid range: {item}")

    # Parse --patterns
    for item in patterns:
        if ":" not in item:
            continue
        name, pat = item.split(":", 1)
        fields.setdefault(name.strip(), {})["pattern"] = pat.strip()

    # Parse --enums
    for item in enums:
        if ":" not in item:
            continue
        name, vals = item.split(":", 1)
        fields.setdefault(name.strip(), {})["enum"] = [v.strip() for v in vals.split(",")]

    # Parse --unique
    for name in unique:
        fields.setdefault(name.strip(), {})["unique"] = True

    return {"fields": fields}


def _render_violations(violations: list[Violation]) -> None:
    """Render violations as a rich table."""
    if not violations:
        console.print(Panel("[green]✓ All checks passed![/green]", title="Validation Result", border_style="green"))
        return

    # Group by rule
    by_rule: dict[str, list[Violation]] = {}
    for v in violations:
        by_rule.setdefault(v.rule, []).append(v)

    summary_table = Table(title="Violation Summary", box=box.ROUNDED)
    summary_table.add_column("Rule", style="cyan")
    summary_table.add_column("Count", justify="right")
    for rule, items in sorted(by_rule.items(), key=lambda x: -len(x[1])):
        summary_table.add_row(rule, str(len(items)))
    console.print(summary_table)

    # Detailed table (max 200 rows)
    detail_table = Table(title="Violations (first 200)", box=box.ROUNDED, show_lines=True)
    detail_table.add_column("Row", style="cyan", justify="right")
    detail_table.add_column("Field", style="yellow")
    detail_table.add_column("Rule", style="red")
    detail_table.add_column("Message")
    detail_table.add_column("Value", overflow="fold")
    for v in violations[:200]:
        detail_table.add_row(str(v.row), v.field, v.rule, v.message, str(v.value)[:100])
    console.print(detail_table)

    if len(violations) > 200:
        console.print(f"[dim]... and {len(violations) - 200} more violations[/dim]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option("1.0.0", prog_name="data_validator")
def cli() -> None:
    """Data Validator — validate CSV/JSON files against schemas."""


@cli.command()
@click.argument("data_file")
@click.option("--schema", "-s", default=None, help="Path to schema JSON file.")
@click.option("--strict", is_flag=True, help="Flag extra fields as errors.")
@click.option("--required", multiple=True, help="Required field names. Repeatable.")
@click.option("--types", "type_specs", multiple=True, help="Type specs: 'field:type'. Repeatable.")
@click.option("--range", "range_specs", multiple=True, help="Range specs: 'field:min-max'. Repeatable.")
@click.option("--pattern", "pattern_specs", multiple=True, help="Pattern specs: 'field:regex'. Repeatable.")
@click.option("--enum", "enum_specs", multiple=True, help="Enum specs: 'field:val1,val2'. Repeatable.")
@click.option("--unique", multiple=True, help="Fields that must be unique. Repeatable.")
@click.option("--output", "-o", default=None, help="Export violations as JSON.")
@click.option("--fail-on-errors", is_flag=True, help="Exit with code 1 if violations found.")
def validate(
    data_file: str,
    schema: str | None,
    strict: bool,
    required: tuple[str, ...],
    type_specs: tuple[str, ...],
    range_specs: tuple[str, ...],
    pattern_specs: tuple[str, ...],
    enum_specs: tuple[str, ...],
    unique: tuple[str, ...],
    output: str | None,
    fail_on_errors: bool,
) -> None:
    """Validate a CSV or JSON file."""
    p = Path(data_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {data_file}")
        raise SystemExit(1)

    # Build or load schema
    if schema:
        schema_dict = _load_schema(schema)
        console.print(f"[blue]Schema:[/blue] {schema}")
    elif required or type_specs or range_specs or pattern_specs or enum_specs or unique:
        schema_dict = _build_schema_from_cli(required, type_specs, range_specs, pattern_specs, enum_specs, unique)
        console.print("[blue]Schema:[/blue] built from CLI options")
    else:
        console.print("[red]Error:[/red] Provide --schema or at least one validation rule.")
        raise SystemExit(1)

    # Load data
    suffix = p.suffix.lower()
    if suffix == ".csv":
        try:
            df = pd.read_csv(p)
            console.print(f"[green]✓[/green] Loaded CSV: {len(df)} rows × {len(df.columns)} columns")
        except Exception as exc:
            console.print(f"[red]Error reading CSV:[/red] {exc}")
            raise SystemExit(1)
        violations = _validate_dataframe(df, schema_dict, strict=strict)
    elif suffix == ".json":
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            console.print(f"[red]Error reading JSON:[/red] {exc}")
            raise SystemExit(1)
        if isinstance(data, list):
            violations = _validate_json_records(data, schema_dict, strict=strict)
        elif isinstance(data, dict):
            violations = _validate_json_records([data], schema_dict, strict=strict)
        else:
            console.print("[red]Error:[/red] JSON must be an object or array of objects.")
            raise SystemExit(1)
    else:
        console.print(f"[red]Error:[/red] Unsupported file format: {suffix}. Use .csv or .json.")
        raise SystemExit(1)

    console.print(f"[blue]Found {len(violations)} violation(s)[/blue]")
    _render_violations(violations)

    if output:
        out = Path(output)
        with open(out, "w") as f:
            json.dump(
                [{"row": v.row, "field": v.field, "rule": v.rule, "message": v.message, "value": v.value} for v in violations],
                f, indent=2, default=str,
            )
        console.print(f"[green]✓[/green] Violations exported to [cyan]{out}[/cyan]")

    if fail_on_errors and violations:
        raise SystemExit(1)


@cli.command(name="init-schema")
@click.argument("data_file")
@click.option("--output", "-o", default="schema.json", help="Output schema file path.")
def init_schema(data_file: str, output: str) -> None:
    """Auto-generate a schema from a CSV or JSON file."""
    p = Path(data_file)
    if not p.exists():
        console.print(f"[red]Error:[/red] File not found: {data_file}")
        raise SystemExit(1)

    suffix = p.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(p)
        columns = df.columns.tolist()
        sample_data = df
    elif suffix == ".json":
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            columns = list(data[0].keys())
            sample_data = pd.DataFrame(data)
        elif isinstance(data, dict):
            columns = list(data.keys())
            sample_data = pd.DataFrame([data])
        else:
            console.print("[red]Error:[/red] Cannot infer schema from this JSON structure.")
            raise SystemExit(1)
    else:
        console.print(f"[red]Error:[/red] Unsupported: {suffix}")
        raise SystemExit(1)

    fields: dict[str, dict] = {}
    for col in columns:
        rules: dict[str, Any] = {}
        series = sample_data[col].dropna()

        # Infer type
        if pd.api.types.is_integer_dtype(series):
            rules["type"] = "int"
        elif pd.api.types.is_float_dtype(series):
            rules["type"] = "float"
        elif pd.api.types.is_bool_dtype(series):
            rules["type"] = "bool"
        else:
            rules["type"] = "str"

        # Check if all non-null → required
        if series.notna().all() and len(series) > 0:
            rules["required"] = True

        # Unique check
        if series.is_unique:
            rules["unique"] = True

        # Numeric range
        if rules["type"] in ("int", "float"):
            try:
                rules["min"] = float(series.min())
                rules["max"] = float(series.max())
            except (TypeError, ValueError):
                pass

        fields[col] = rules

    schema = {"fields": fields}
    out = Path(output)
    with open(out, "w") as f:
        json.dump(schema, f, indent=2)
    console.print(f"[green]✓[/green] Schema generated → [cyan]{out}[/cyan]")

    # Preview
    t = Table(title="Generated Schema", box=box.ROUNDED)
    t.add_column("Field", style="cyan")
    t.add_column("Type")
    t.add_column("Required")
    t.add_column("Unique")
    t.add_column("Range")
    for fname, rules in fields.items():
        t.add_row(
            fname,
            rules.get("type", "?"),
            "✓" if rules.get("required") else "—",
            "✓" if rules.get("unique") else "—",
            f"{rules.get('min', '?')}–{rules.get('max', '?')}" if "min" in rules else "—",
        )
    console.print(t)


@cli.command()
@click.argument("schema_file")
def inspect(schema_file: str) -> None:
    """Display a schema file in a readable format."""
    schema = _load_schema(schema_file)
    fields = schema.get("fields", {})

    t = Table(title=f"Schema: {schema_file}", box=box.ROUNDED, show_lines=True)
    t.add_column("Field", style="cyan")
    t.add_column("Type")
    t.add_column("Required", justify="center")
    t.add_column("Unique", justify="center")
    t.add_column("Constraints")

    for fname, rules in fields.items():
        constraints = []
        if "min" in rules:
            constraints.append(f"min={rules['min']}")
        if "max" in rules:
            constraints.append(f"max={rules['max']}")
        if "min_length" in rules:
            constraints.append(f"min_len={rules['min_length']}")
        if "max_length" in rules:
            constraints.append(f"max_len={rules['max_length']}")
        if "pattern" in rules:
            constraints.append(f"pattern=/{rules['pattern']}/")
        if "enum" in rules:
            constraints.append(f"enum={rules['enum']}")
        t.add_row(
            fname,
            rules.get("type", "—"),
            "✓" if rules.get("required") else "—",
            "✓" if rules.get("unique") else "—",
            ", ".join(constraints) or "—",
        )
    console.print(t)


if __name__ == "__main__":
    cli()
