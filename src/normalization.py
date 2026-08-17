#!/usr/bin/env python3
"""Deterministically normalize and reconcile one or all LLM extractions.

This stage performs no LLM calls and no master-data matching. It converts raw
candidate strings into typed business values, recomputes every amount with
Decimal, normalizes dates/units, and creates explicit blocking issues whenever
the source cannot safely establish a routable net total or delivery date.

Examples:
    python subproblem4_normalization.py --db procurement.db --email-id 1
    python subproblem4_normalization.py --db procurement.db --extraction-run-id 4
    python subproblem4_normalization.py --db procurement.db --all
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Literal

try:  # Support direct execution and package-style imports.
    from .procurement_storage import (
        DEFAULT_DB_PATH,
        StorageError,
        list_ingested_emails,
        load_extraction_for_normalization,
        save_normalization_run,
    )
except ImportError:
    from procurement_storage import (
        DEFAULT_DB_PATH,
        StorageError,
        list_ingested_emails,
        load_extraction_for_normalization,
        save_normalization_run,
    )


NORMALIZER_VERSION = "purchase-normalization-v1"
MONEY_QUANTUM = Decimal("0.01")
RECONCILIATION_TOLERANCE = Decimal("0.01")
IssueSeverity = Literal["blocking", "warning"]

UNIT_ALIASES = {
    "pc": "pcs",
    "pcs": "pcs",
    "piece": "pcs",
    "pieces": "pcs",
    "ea": "pcs",
    "each": "pcs",
    "st": "pcs",
    "stk": "pcs",
    "stk.": "pcs",
    "stück": "pcs",
    "stueck": "pcs",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "kilogramm": "kg",
    "g": "g",
    "gram": "g",
    "l": "l",
    "liter": "l",
    "litre": "l",
    "m": "m",
    "meter": "m",
    "metre": "m",
    "h": "h",
    "hr": "h",
    "hrs": "h",
    "hour": "h",
    "hours": "h",
    "std": "h",
    "std.": "h",
    "stunde": "h",
    "stunden": "h",
    "lot": "lot",
    "lots": "lot",
    "pos": "lot",
    "pos.": "lot",
    "position": "lot",
    "set": "set",
    "sets": "set",
    "satz": "set",
    "service": "service",
}

MONTHS = {
    "january": 1,
    "jan": 1,
    "januar": 1,
    "february": 2,
    "feb": 2,
    "februar": 2,
    "march": 3,
    "mar": 3,
    "märz": 3,
    "maerz": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "mai": 5,
    "june": 6,
    "jun": 6,
    "juni": 6,
    "july": 7,
    "jul": 7,
    "juli": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "oktober": 10,
    "okt": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
    "dezember": 12,
    "dez": 12,
}


class NormalizationError(RuntimeError):
    pass


def add_issue(
    issues: list[dict[str, Any]],
    code: str,
    field_path: str,
    message: str,
    *,
    severity: IssueSeverity = "blocking",
) -> None:
    issue = {
        "code": code,
        "severity": severity,
        "field_path": field_path,
        "message": message,
    }
    if issue not in issues:
        issues.append(issue)


def normalize_decimal_text(value: str) -> str:
    """Convert common English/German thousands and decimal separators."""
    text = (
        value.strip()
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace("'", "")
        .replace("’", "")
        .replace("−", "-")
    )
    if not text or not re.fullmatch(r"[+-]?[0-9][0-9.,]*", text):
        raise ValueError("not plain numeric text")

    sign = ""
    if text[0] in "+-":
        sign, text = text[0], text[1:]
    if not text:
        raise ValueError("number contains only a sign")

    comma_count = text.count(",")
    dot_count = text.count(".")
    if comma_count and dot_count:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        text = text.replace(thousands_separator, "")
        if text.count(decimal_separator) != 1:
            raise ValueError("ambiguous decimal separators")
        text = text.replace(decimal_separator, ".")
    elif comma_count or dot_count:
        separator = "," if comma_count else "."
        groups = text.split(separator)
        if any(not group for group in groups):
            raise ValueError("empty number group")
        if len(groups) > 2:
            if all(len(group) == 3 for group in groups[1:]):
                text = "".join(groups)
            elif all(len(group) == 3 for group in groups[1:-1]) and len(groups[-1]) in {1, 2}:
                text = "".join(groups[:-1]) + "." + groups[-1]
            else:
                raise ValueError("ambiguous repeated separator")
        else:
            whole, fraction = groups
            if len(fraction) == 3 and whole != "0":
                text = whole + fraction
            else:
                text = whole + "." + fraction
    return sign + text


def parse_decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("value is not a string")
    try:
        return Decimal(normalize_decimal_text(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc


def parse_optional_decimal(
    value: Any,
    *,
    field_path: str,
    issues: list[dict[str, Any]],
) -> Decimal | None:
    if value is None:
        return None
    try:
        return parse_decimal(value)
    except ValueError as exc:
        add_issue(
            issues,
            "INVALID_NUMBER",
            field_path,
            f"Could not parse numeric text {value!r}: {exc}",
        )
        return None


def money_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), "f")


def decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def amounts_differ(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) > RECONCILIATION_TOLERANCE


def normalize_unit(
    value: Any,
    *,
    field_path: str,
    issues: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        add_issue(issues, "MISSING_UNIT", field_path, "Line unit is required")
        return None, None
    raw = value.strip()
    normalized = UNIT_ALIASES.get(raw.casefold())
    if normalized is None:
        add_issue(
            issues,
            "UNRECOGNIZED_UNIT",
            field_path,
            f"Unit {raw!r} has no configured ERP normalization",
        )
        return raw, None
    return raw, normalized


def source_email_date(record: dict[str, Any]) -> date | None:
    value = record.get("headers", {}).get("date_utc")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_date_text(
    value: Any,
    *,
    reference_date: date | None,
    allow_end_of_month: bool,
) -> tuple[str | None, str]:
    if not isinstance(value, str) or not value.strip():
        return None, "missing"
    raw = value.strip()
    lowered = raw.casefold()

    iso_match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lowered)
    if iso_match:
        parsed = safe_date(*(int(part) for part in iso_match.groups()))
        return (parsed.isoformat(), "exact") if parsed else (None, "invalid")

    numeric_match = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", lowered)
    if numeric_match:
        day, month, year = (int(part) for part in numeric_match.groups())
        parsed = safe_date(year, month, day)
        return (parsed.isoformat(), "exact") if parsed else (None, "invalid")

    month_names = "|".join(sorted((re.escape(name) for name in MONTHS), key=len, reverse=True))
    word_match = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\.?\s+({month_names})\s*,?\s*(\d{{4}})\b",
        lowered,
    )
    if word_match:
        day = int(word_match.group(1))
        month = MONTHS[word_match.group(2)]
        year = int(word_match.group(3))
        parsed = safe_date(year, month, day)
        return (parsed.isoformat(), "exact") if parsed else (None, "invalid")

    if allow_end_of_month:
        specific_end = re.search(
            rf"\b(?:end(?:e)?(?:\s+of)?|ende)\s+(?:of\s+)?({month_names})(?:\s+(\d{{4}}))?\b",
            lowered,
        )
        if specific_end:
            month = MONTHS[specific_end.group(1)]
            year = int(specific_end.group(2)) if specific_end.group(2) else (
                reference_date.year if reference_date else 0
            )
            if year:
                return (
                    date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
                    "derived_end_of_month",
                )
        if re.search(r"\b(?:end of (?:the )?month|ende (?:des )?monats?|ende monat)\b", lowered):
            if reference_date is not None:
                return (
                    date(
                        reference_date.year,
                        reference_date.month,
                        calendar.monthrange(reference_date.year, reference_date.month)[1],
                    ).isoformat(),
                    "derived_end_of_month",
                )
    return None, "unresolved"


def normalize_line(
    raw_line: dict[str, Any],
    index: int,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    prefix = f"lines[{index}]"
    item_name = raw_line.get("item_name")
    if not isinstance(item_name, str) or not item_name.strip():
        add_issue(issues, "MISSING_ITEM_NAME", f"{prefix}.item_name", "Item name is required")
        item_name = None
    else:
        item_name = item_name.strip()

    quantity = parse_optional_decimal(
        raw_line.get("quantity"), field_path=f"{prefix}.quantity", issues=issues
    )
    if quantity is None and raw_line.get("quantity") is None:
        add_issue(issues, "MISSING_QUANTITY", f"{prefix}.quantity", "Quantity is required")
    elif quantity is not None and quantity <= 0:
        add_issue(
            issues,
            "NON_POSITIVE_QUANTITY",
            f"{prefix}.quantity",
            "Quantity must be greater than zero",
        )

    unit_raw, unit = normalize_unit(
        raw_line.get("unit"), field_path=f"{prefix}.unit", issues=issues
    )

    explicitly_free = bool(raw_line.get("explicitly_free", False))
    quoted_price = parse_optional_decimal(
        raw_line.get("quoted_unit_price"),
        field_path=f"{prefix}.quoted_unit_price",
        issues=issues,
    )
    if quoted_price is None and raw_line.get("quoted_unit_price") is None:
        if explicitly_free:
            quoted_price = Decimal("0")
        else:
            add_issue(
                issues,
                "MISSING_PRICE",
                f"{prefix}.quoted_unit_price",
                "A non-free line requires a price",
            )
    if explicitly_free and quoted_price not in {None, Decimal("0")}:
        add_issue(
            issues,
            "FREE_LINE_HAS_NONZERO_PRICE",
            f"{prefix}.quoted_unit_price",
            "Line is marked free but has a non-zero price",
        )

    basis_raw = raw_line.get("price_basis_quantity")
    basis = parse_optional_decimal(
        basis_raw,
        field_path=f"{prefix}.price_basis_quantity",
        issues=issues,
    )
    if basis_raw is None:
        basis = Decimal("1")
    elif basis is not None and basis <= 0:
        add_issue(
            issues,
            "INVALID_PRICE_BASIS",
            f"{prefix}.price_basis_quantity",
            "Price basis must be greater than zero",
        )
        basis = None

    normalized_price: Decimal | None = None
    computed_total: Decimal | None = None
    if quoted_price is not None and basis is not None:
        normalized_price = quoted_price / basis
        if quantity is not None and quantity > 0:
            computed_total = (quantity * normalized_price).quantize(
                MONEY_QUANTUM, rounding=ROUND_HALF_UP
            )

    stated_total = parse_optional_decimal(
        raw_line.get("stated_line_total"),
        field_path=f"{prefix}.stated_line_total",
        issues=issues,
    )
    line_status = "incomplete" if computed_total is None else "computed"
    if computed_total is not None and stated_total is not None:
        if amounts_differ(computed_total, stated_total):
            line_status = "mismatch"
            add_issue(
                issues,
                "LINE_TOTAL_MISMATCH",
                prefix,
                f"Computed {money_text(computed_total)} but document states {money_text(stated_total)}",
            )
        else:
            line_status = "reconciled"

    return {
        "item_name": item_name,
        "item_code": raw_line.get("item_code"),
        "quantity_raw": raw_line.get("quantity"),
        "quantity": decimal_text(quantity),
        "unit_raw": unit_raw,
        "unit": unit,
        "quoted_unit_price_raw": raw_line.get("quoted_unit_price"),
        "quoted_unit_price": decimal_text(quoted_price),
        "price_basis_quantity_raw": basis_raw,
        "price_basis_quantity": decimal_text(basis),
        "normalized_unit_price": decimal_text(normalized_price),
        "stated_line_total_raw": raw_line.get("stated_line_total"),
        "stated_line_total": money_text(stated_total),
        "computed_line_total": money_text(computed_total),
        "explicitly_free": explicitly_free,
        "reconciliation_status": line_status,
    }


def normalize_extraction(
    extraction: dict[str, Any],
    source_record: dict[str, Any],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    currency_raw = extraction.get("currency")
    currency: str | None = None
    if isinstance(currency_raw, str) and re.fullmatch(r"[A-Za-z]{3}", currency_raw.strip()):
        currency = currency_raw.strip().upper()
    else:
        add_issue(
            issues,
            "MISSING_OR_INVALID_CURRENCY",
            "currency",
            "An explicit three-letter currency code is required",
        )

    raw_lines = extraction.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        add_issue(issues, "NO_LINES", "lines", "At least one requested line is required")
        raw_lines = []
    lines = [
        normalize_line(line if isinstance(line, dict) else {}, index, issues)
        for index, line in enumerate(raw_lines)
    ]

    computed_values = [
        Decimal(line["computed_line_total"])
        if line["computed_line_total"] is not None
        else None
        for line in lines
    ]
    stated_values = [
        Decimal(line["stated_line_total"])
        if line["stated_line_total"] is not None
        else None
        for line in lines
    ]
    all_computed = bool(lines) and all(value is not None for value in computed_values)
    all_stated = bool(lines) and all(value is not None for value in stated_values)
    computed_sum = sum((value for value in computed_values if value is not None), Decimal("0"))
    stated_sum = sum((value for value in stated_values if value is not None), Decimal("0"))

    net = parse_optional_decimal(
        extraction.get("stated_net_total"), field_path="stated_net_total", issues=issues
    )
    tax = parse_optional_decimal(
        extraction.get("stated_tax_amount"), field_path="stated_tax_amount", issues=issues
    )
    gross = parse_optional_decimal(
        extraction.get("stated_gross_total"), field_path="stated_gross_total", issues=issues
    )
    unclassified = parse_optional_decimal(
        extraction.get("stated_unclassified_total"),
        field_path="stated_unclassified_total",
        issues=issues,
    )

    if all_computed and net is not None and amounts_differ(computed_sum, net):
        add_issue(
            issues,
            "NET_TOTAL_MISMATCH",
            "stated_net_total",
            f"Computed line sum {money_text(computed_sum)} differs from stated net {money_text(net)}",
        )
    if all_stated and net is not None and amounts_differ(stated_sum, net):
        add_issue(
            issues,
            "STATED_LINE_SUM_MISMATCH",
            "stated_net_total",
            f"Stated line sum {money_text(stated_sum)} differs from stated net {money_text(net)}",
        )
    if net is not None and tax is not None and gross is not None:
        expected_gross = net + tax
        if amounts_differ(expected_gross, gross):
            add_issue(
                issues,
                "GROSS_TOTAL_MISMATCH",
                "stated_gross_total",
                f"Net plus tax is {money_text(expected_gross)} but gross is {money_text(gross)}",
            )
    if unclassified is not None:
        add_issue(
            issues,
            "UNCLASSIFIED_TOTAL_REQUIRES_REVIEW",
            "stated_unclassified_total",
            "Document total does not establish whether VAT is included",
        )

    blocking_line_issue_codes = {
        "MISSING_ITEM_NAME",
        "MISSING_QUANTITY",
        "NON_POSITIVE_QUANTITY",
        "MISSING_UNIT",
        "UNRECOGNIZED_UNIT",
        "MISSING_PRICE",
        "INVALID_PRICE_BASIS",
        "LINE_TOTAL_MISMATCH",
        "INVALID_NUMBER",
    }
    line_is_safe = not any(
        issue["code"] in blocking_line_issue_codes
        and issue["field_path"].startswith("lines[")
        for issue in issues
    )
    confirmed_net: Decimal | None = None
    net_source: str | None = None
    if unclassified is None and all_computed and line_is_safe:
        if net is not None and not amounts_differ(computed_sum, net):
            confirmed_net = net
            net_source = "stated_and_reconciled"
        elif net is None and gross is not None and tax is not None:
            derived_net = gross - tax
            if not amounts_differ(computed_sum, derived_net):
                confirmed_net = derived_net
                net_source = "derived_from_gross_minus_tax_and_reconciled"
            else:
                add_issue(
                    issues,
                    "DERIVED_NET_MISMATCH",
                    "confirmed_net_total",
                    f"Gross minus tax is {money_text(derived_net)} but lines sum to {money_text(computed_sum)}",
                )
        elif net is None:
            confirmed_net = computed_sum
            net_source = "computed_from_complete_lines"
    if confirmed_net is None:
        add_issue(
            issues,
            "NO_CONFIRMED_NET_TOTAL",
            "confirmed_net_total",
            "A net total could not be safely established and reconciled",
        )
    elif confirmed_net <= 0:
        add_issue(
            issues,
            "NON_POSITIVE_NET_TOTAL",
            "confirmed_net_total",
            "Requisition net total must be greater than zero",
        )
        confirmed_net = None
        net_source = None

    reference_date = source_email_date(source_record)
    requested_date, requested_date_mode = normalize_date_text(
        extraction.get("requested_delivery_date_text"),
        reference_date=reference_date,
        allow_end_of_month=True,
    )
    if requested_date_mode == "missing":
        add_issue(
            issues,
            "MISSING_REQUESTED_DELIVERY_DATE",
            "requested_delivery_date",
            "Requested delivery date is required before PO creation",
        )
    elif requested_date is None:
        add_issue(
            issues,
            "UNRESOLVED_REQUESTED_DELIVERY_DATE",
            "requested_delivery_date",
            f"Could not safely normalize {extraction.get('requested_delivery_date_text')!r}",
        )

    quote_date, quote_date_mode = normalize_date_text(
        extraction.get("quote_date_text"), reference_date=None, allow_end_of_month=False
    )
    if extraction.get("quote_date_text") is not None and quote_date is None:
        add_issue(
            issues,
            "UNPARSEABLE_QUOTE_DATE",
            "quote_date",
            f"Could not normalize {extraction.get('quote_date_text')!r}",
            severity="warning",
        )
    valid_until, valid_until_mode = normalize_date_text(
        extraction.get("quote_valid_until_text"),
        reference_date=None,
        allow_end_of_month=False,
    )
    if extraction.get("quote_valid_until_text") is not None and valid_until is None:
        add_issue(
            issues,
            "UNPARSEABLE_QUOTE_VALIDITY_DATE",
            "quote_valid_until",
            f"Could not normalize {extraction.get('quote_valid_until_text')!r}",
            severity="warning",
        )

    status = "needs_review" if any(issue["severity"] == "blocking" for issue in issues) else "valid"
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "status": status,
        "currency_raw": currency_raw,
        "currency": currency,
        "lines": lines,
        "totals": {
            "computed_line_sum": money_text(computed_sum) if all_computed else None,
            "computed_line_sum_complete": all_computed,
            "stated_line_sum": money_text(stated_sum) if all_stated else None,
            "stated_line_sum_complete": all_stated,
            "stated_net_total": money_text(net),
            "stated_tax_amount": money_text(tax),
            "stated_gross_total": money_text(gross),
            "stated_unclassified_total": money_text(unclassified),
            "confirmed_net_total": money_text(confirmed_net),
            "confirmed_net_total_source": net_source,
        },
        "dates": {
            "email_date": reference_date.isoformat() if reference_date else None,
            "requested_delivery_date_raw": extraction.get("requested_delivery_date_text"),
            "requested_delivery_date": requested_date,
            "requested_delivery_date_mode": requested_date_mode,
            "supplier_delivery_text": extraction.get("supplier_delivery_text"),
            "quote_date_raw": extraction.get("quote_date_text"),
            "quote_date": quote_date,
            "quote_date_mode": quote_date_mode,
            "quote_valid_until_raw": extraction.get("quote_valid_until_text"),
            "quote_valid_until": valid_until,
            "quote_valid_until_mode": valid_until_mode,
        },
        "issues": issues,
    }


def write_json(path: Path | None, payload: Any) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        sys.stdout.write(rendered)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, path)


def normalize_and_store(
    db_path: Path,
    *,
    email_id: int | None = None,
    extraction_run_id: int | None = None,
) -> dict[str, Any]:
    try:
        source = load_extraction_for_normalization(
            db_path,
            extraction_run_id=extraction_run_id,
            ingested_email_id=email_id,
        )
    except StorageError as exc:
        raise NormalizationError(str(exc)) from exc
    try:
        result = normalize_extraction(source["extraction"], source["source_record"])
    except Exception as exc:
        try:
            run_id = save_normalization_run(
                db_path,
                extraction_run_id=source["extraction_run_id"],
                status="failed",
                normalizer_version=NORMALIZER_VERSION,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
        except StorageError as storage_exc:
            raise NormalizationError(
                f"Normalization failed and failure could not be stored: {storage_exc}"
            ) from storage_exc
        raise NormalizationError(
            f"Normalization failed; stored failed normalization run ID {run_id}"
        ) from exc
    try:
        normalization_run_id = save_normalization_run(
            db_path,
            extraction_run_id=source["extraction_run_id"],
            status=result["status"],
            normalizer_version=NORMALIZER_VERSION,
            normalized=result,
            issues=result["issues"],
        )
    except StorageError as exc:
        raise NormalizationError(str(exc)) from exc
    return {
        "schema_version": 1,
        "status": result["status"],
        "database": {
            "path": str(db_path.expanduser().resolve()),
            "ingested_email_id": source["ingested_email_id"],
            "extraction_run_id": source["extraction_run_id"],
            "normalization_run_id": normalization_run_id,
        },
        "normalization": result,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize and reconcile stored LLM extractions without an LLM"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite database (default: PROCUREMENT_DB_PATH or ./procurement.db)",
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--email-id", type=int)
    selector.add_argument("--extraction-run-id", type=int)
    selector.add_argument("--all", action="store_true")
    parser.add_argument("-o", "--output", type=Path, help="Full JSON output for one run")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Optional per-email JSON exports with --all; DB storage always occurs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.all and args.output is not None:
        raise NormalizationError("Use --output-dir, not --output, with --all")
    if not args.all and args.output_dir is not None:
        raise NormalizationError("--output-dir requires --all")

    if not args.all:
        payload = normalize_and_store(
            args.db,
            email_id=args.email_id,
            extraction_run_id=args.extraction_run_id,
        )
        write_json(args.output, payload)
        return 0 if payload["status"] == "valid" else 2

    try:
        emails = list_ingested_emails(args.db)
    except StorageError as exc:
        raise NormalizationError(str(exc)) from exc
    summary: list[dict[str, Any]] = []
    for email in emails:
        email_id = int(email["id"])
        try:
            payload = normalize_and_store(args.db, email_id=email_id)
            summary.append(
                {
                    "email_id": email_id,
                    "filename": email["source_filename"],
                    "status": payload["status"],
                    "extraction_run_id": payload["database"]["extraction_run_id"],
                    "normalization_run_id": payload["database"]["normalization_run_id"],
                    "blocking_issue_count": sum(
                        issue["severity"] == "blocking"
                        for issue in payload["normalization"]["issues"]
                    ),
                }
            )
            if args.output_dir is not None:
                write_json(args.output_dir / f"email_{email_id}.json", payload)
        except NormalizationError as exc:
            summary.append(
                {
                    "email_id": email_id,
                    "filename": email["source_filename"],
                    "status": "failed",
                    "error": str(exc),
                }
            )
    counts = {
        status: sum(item["status"] == status for item in summary)
        for status in ("valid", "needs_review", "failed")
    }
    write_json(
        None,
        {
            "database": str(args.db.expanduser().resolve()),
            "normalizer_version": NORMALIZER_VERSION,
            "counts": counts,
            "results": summary,
        },
    )
    return 2 if counts["failed"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NormalizationError as exc:
        print(f"Normalization failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
