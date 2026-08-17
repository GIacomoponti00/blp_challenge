#!/usr/bin/env python3
"""Load reference data into SQLite and deterministically match normalized requests.

Examples:
    python master_data_matching.py --db procurement.db \
        --master-data master_data.json --all
    python master_data_matching.py --db procurement.db --email-id 1
    python master_data_matching.py --db procurement.db --normalization-run-id 4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

try:  # Support direct execution and package-style imports.
    from .procurement_storage import DEFAULT_DB_PATH
except ImportError:
    from procurement_storage import DEFAULT_DB_PATH

try:
    from .master_data_storage import (
        MasterDataStorageError,
        list_normalized_email_ids,
        load_current_master_data,
        load_matching_input,
        normalize_email,
        normalize_supplier_name,
        normalize_vat_id,
        save_matching_run,
        sync_master_data,
    )
except ImportError:
    from master_data_storage import (
        MasterDataStorageError,
        list_normalized_email_ids,
        load_current_master_data,
        load_matching_input,
        normalize_email,
        normalize_supplier_name,
        normalize_vat_id,
        save_matching_run,
        sync_master_data,
    )


MATCHER_VERSION = "master-data-matching-v1"
IssueSeverity = Literal["blocking", "warning"]

ISSUE_BLOCKS = {
    "SUPPLIER_NOT_FOUND": ("approval_routing", "purchase_order"),
    "AMBIGUOUS_SUPPLIER_VAT": ("approval_routing", "purchase_order"),
    "AMBIGUOUS_SUPPLIER_NAME": ("approval_routing", "purchase_order"),
    "SUPPLIER_IDENTITY_CONFLICT": ("approval_routing", "purchase_order"),
    "SUPPLIER_BLOCKED": ("approval_routing", "purchase_order"),
    "SUPPLIER_NOT_ACTIVE": ("approval_routing", "purchase_order"),
    "MISSING_COST_CENTER": ("approval_routing",),
    "COST_CENTER_NOT_FOUND": ("approval_routing",),
    "COST_CENTER_MASTER_DATA_BROKEN": ("approval_routing",),
    "REQUESTER_NOT_FOUND": ("approval_routing",),
}


class MatchingError(RuntimeError):
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
        "blocks": list(ISSUE_BLOCKS.get(code, ())) if severity == "blocking" else [],
        "field_path": field_path,
        "message": message,
    }
    if issue not in issues:
        issues.append(issue)


def public_supplier(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "vat_id": row.get("vat_id"),
        "country": row.get("country"),
        "default_currency": row.get("default_currency"),
        "payment_terms_days": row.get("payment_terms_days"),
        "preferred": bool(row.get("preferred")),
        "status": row.get("status"),
    }


def public_employee(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "deputy_for": row.get("deputy_for"),
    }


def match_supplier(
    extraction: dict[str, Any],
    suppliers: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_name = extraction.get("supplier_name")
    raw_vat = extraction.get("supplier_vat_id")
    normalized_name = normalize_supplier_name(raw_name)
    normalized_vat = normalize_vat_id(raw_vat)
    vat_matches = [
        supplier
        for supplier in suppliers
        if normalized_vat is not None
        and supplier.get("normalized_vat_id") == normalized_vat
    ]
    name_matches = [
        supplier
        for supplier in suppliers
        if normalized_name is not None
        and supplier.get("normalized_name") == normalized_name
    ]

    selected: dict[str, Any] | None = None
    method: str | None = None
    assurance: str | None = None
    if len(vat_matches) > 1:
        add_issue(
            issues,
            "AMBIGUOUS_SUPPLIER_VAT",
            "supplier_vat_id",
            f"VAT ID matches multiple suppliers: {[item['id'] for item in vat_matches]}",
        )
    elif len(name_matches) > 1:
        add_issue(
            issues,
            "AMBIGUOUS_SUPPLIER_NAME",
            "supplier_name",
            f"Normalized name matches multiple suppliers: {[item['id'] for item in name_matches]}",
        )
    elif len(vat_matches) == 1:
        vat_supplier = vat_matches[0]
        if len(name_matches) == 1 and name_matches[0]["id"] != vat_supplier["id"]:
            add_issue(
                issues,
                "SUPPLIER_IDENTITY_CONFLICT",
                "supplier",
                f"VAT ID points to {vat_supplier['id']} but name points to {name_matches[0]['id']}",
            )
        else:
            selected = vat_supplier
            method = "vat_and_name" if name_matches else "vat"
            assurance = "high"
            if normalized_name is not None and not name_matches:
                add_issue(
                    issues,
                    "SUPPLIER_NAME_DIFFERS_FROM_MASTER",
                    "supplier_name",
                    "VAT ID matched uniquely, but the extracted name is not an exact normalized master-data name",
                    severity="warning",
                )
    elif len(name_matches) == 1:
        selected = name_matches[0]
        method = "name"
        assurance = "lower"
        add_issue(
            issues,
            "SUPPLIER_MATCHED_BY_NAME_ONLY",
            "supplier_name",
            "Supplier was matched by unique exact normalized name without a master-data VAT match",
            severity="warning",
        )
        if normalized_vat is not None:
            add_issue(
                issues,
                "SUPPLIER_VAT_NOT_FOUND",
                "supplier_vat_id",
                "The extracted VAT ID was not found; the unique name match is lower assurance",
                severity="warning",
            )
    else:
        add_issue(
            issues,
            "SUPPLIER_NOT_FOUND",
            "supplier",
            "No supplier matched the extracted VAT ID or exact normalized name",
        )

    if selected is not None:
        supplier_status = str(selected.get("status") or "").casefold()
        if supplier_status == "blocked":
            add_issue(
                issues,
                "SUPPLIER_BLOCKED",
                "supplier.status",
                f"Supplier {selected['id']} is blocked and cannot be ordered from",
            )
        elif supplier_status != "active":
            add_issue(
                issues,
                "SUPPLIER_NOT_ACTIVE",
                "supplier.status",
                f"Supplier {selected['id']} has unsupported status {selected.get('status')!r}",
            )
        extracted_currency = extraction.get("currency")
        default_currency = selected.get("default_currency")
        if (
            isinstance(extracted_currency, str)
            and isinstance(default_currency, str)
            and extracted_currency.upper() != default_currency.upper()
        ):
            add_issue(
                issues,
                "SUPPLIER_CURRENCY_DIFFERS_FROM_DEFAULT",
                "currency",
                f"Request currency {extracted_currency.upper()} differs from supplier default {default_currency.upper()}",
                severity="warning",
            )

    return {
        "extracted_name": raw_name,
        "extracted_vat_id": raw_vat,
        "normalized_name": normalized_name,
        "normalized_vat_id": normalized_vat,
        "match_method": method,
        "assurance": assurance,
        "matched_supplier": public_supplier(selected),
        "vat_candidate_ids": [item["id"] for item in vat_matches],
        "name_candidate_ids": [item["id"] for item in name_matches],
    }


def match_requester(
    sender_email: Any,
    employees: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized = normalize_email(sender_email)
    matches = [
        employee
        for employee in employees
        if normalized is not None and employee.get("normalized_email") == normalized
    ]
    selected = matches[0] if len(matches) == 1 else None
    if selected is None:
        add_issue(
            issues,
            "REQUESTER_NOT_FOUND",
            "requester_email",
            "Top-level sender email does not uniquely match an employee",
        )
    return {
        "source_email": sender_email,
        "match_method": "exact_email" if selected is not None else None,
        "matched_employee": public_employee(selected),
    }


def match_cost_center(
    extraction: dict[str, Any],
    cost_centers: list[dict[str, Any]],
    departments: list[dict[str, Any]],
    employees: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_code = extraction.get("cost_center_code")
    normalized_code = raw_code.strip().upper() if isinstance(raw_code, str) and raw_code.strip() else None
    center = next(
        (item for item in cost_centers if item["code"] == normalized_code), None
    )
    if normalized_code is None:
        add_issue(
            issues,
            "MISSING_COST_CENTER",
            "cost_center_code",
            "An explicit cost-center code is required to construct the approval chain",
        )
    elif center is None:
        add_issue(
            issues,
            "COST_CENTER_NOT_FOUND",
            "cost_center_code",
            f"Cost center {normalized_code!r} is not in master data; no fuzzy correction was attempted",
        )

    if center is None:
        return {
            "extracted_code": raw_code,
            "normalized_code": normalized_code,
            "match_method": None,
            "matched_cost_center": None,
        }

    employee_by_id = {item["id"]: item for item in employees}
    department_by_code = {item["code"]: item for item in departments}
    owner = employee_by_id.get(center["owner_employee_id"])
    department = department_by_code.get(center["department_code"])
    head = employee_by_id.get(department["head_employee_id"]) if department else None
    if owner is None or department is None or head is None:
        add_issue(
            issues,
            "COST_CENTER_MASTER_DATA_BROKEN",
            "cost_center_code",
            f"Cost center {center['code']} has an unresolved owner, department, or department head",
        )

    return {
        "extracted_code": raw_code,
        "normalized_code": normalized_code,
        "match_method": "exact_code",
        "matched_cost_center": {
            "code": center["code"],
            "name": center["name"],
            "owner": public_employee(owner),
            "department": {
                "code": department["code"],
                "name": department["name"],
                "head": public_employee(head),
            }
            if department is not None
            else None,
        },
    }


def match_against_master_data(
    source: dict[str, Any], master_data: dict[str, Any]
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    extraction = source["extraction"]
    supplier = match_supplier(extraction, master_data["suppliers"], issues)
    requester = match_requester(source.get("sender_email"), master_data["employees"], issues)
    cost_center = match_cost_center(
        extraction,
        master_data["cost_centers"],
        master_data["departments"],
        master_data["employees"],
        issues,
    )
    blocking = [issue for issue in issues if issue["severity"] == "blocking"]
    blocked_stages = sorted({stage for issue in blocking for stage in issue["blocks"]})
    status = "needs_review" if blocking else "valid"
    return {
        "matcher_version": MATCHER_VERSION,
        "status": status,
        "master_snapshot": master_data["snapshot"],
        "upstream": {
            "normalization_run_id": source["normalization_run_id"],
            "normalization_status": source["normalization_status"],
            "confirmed_net_total": source["normalized"].get("totals", {}).get(
                "confirmed_net_total"
            ),
            "currency": source["normalized"].get("currency"),
        },
        "requester": requester,
        "supplier": supplier,
        "cost_center": cost_center,
        "gl_account": {
            "confirmed": None,
            "suggestions": [],
            "note": "Optional GL inference is deliberately left unconfirmed for clerk review",
        },
        "readiness": {
            "master_data_matching_ready": not blocking,
            "approval_routing_match_ready": "approval_routing" not in blocked_stages,
            "purchase_order_match_ready": "purchase_order" not in blocked_stages,
            "blocked_stages": blocked_stages,
        },
        "issues": issues,
    }


def match_and_store(
    db_path: Path,
    *,
    master_data: dict[str, Any] | None = None,
    email_id: int | None = None,
    normalization_run_id: int | None = None,
) -> dict[str, Any]:
    try:
        source = load_matching_input(
            db_path,
            ingested_email_id=email_id,
            normalization_run_id=normalization_run_id,
        )
        master = master_data or load_current_master_data(db_path)
        result = match_against_master_data(source, master)
        matching_run_id = save_matching_run(
            db_path,
            normalization_run_id=source["normalization_run_id"],
            master_snapshot_id=int(master["snapshot"]["id"]),
            status=result["status"],
            matcher_version=MATCHER_VERSION,
            matching=result,
            issues=result["issues"],
        )
    except MasterDataStorageError as exc:
        raise MatchingError(str(exc)) from exc
    return {
        "schema_version": 1,
        "status": result["status"],
        "database": {
            "path": str(db_path.expanduser().resolve()),
            "ingested_email_id": source["ingested_email_id"],
            "normalization_run_id": source["normalization_run_id"],
            "matching_run_id": matching_run_id,
            "master_snapshot_id": int(master["snapshot"]["id"]),
        },
        "matching": result,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load master data into SQLite and match normalized requisitions"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--master-data",
        type=Path,
        help="Load/sync this master_data.json before matching",
    )
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="Only load --master-data; do not match a requisition",
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--email-id", type=int)
    selector.add_argument("--normalization-run-id", type=int)
    selector.add_argument("--all", action="store_true")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected = sum(
        option is not None and option is not False
        for option in (args.email_id, args.normalization_run_id, args.all)
    )
    if args.sync_only:
        if args.master_data is None:
            raise MatchingError("--sync-only requires --master-data")
        if selected:
            raise MatchingError("--sync-only cannot be combined with a match selector")
    elif selected != 1:
        raise MatchingError(
            "Choose exactly one of --email-id, --normalization-run-id, or --all"
        )
    if args.all and args.output is not None:
        raise MatchingError("Use --output-dir, not --output, with --all")
    if not args.all and args.output_dir is not None:
        raise MatchingError("--output-dir requires --all")

    sync_result = None
    if args.master_data is not None:
        try:
            sync_result = sync_master_data(args.db, args.master_data)
        except MasterDataStorageError as exc:
            raise MatchingError(str(exc)) from exc
    if args.sync_only:
        write_json(args.output, {"master_data": sync_result})
        return 0

    try:
        master = load_current_master_data(args.db)
    except MasterDataStorageError as exc:
        raise MatchingError(str(exc)) from exc
    if not args.all:
        payload = match_and_store(
            args.db,
            master_data=master,
            email_id=args.email_id,
            normalization_run_id=args.normalization_run_id,
        )
        if sync_result is not None:
            payload["master_data_sync"] = sync_result
        write_json(args.output, payload)
        return 0

    try:
        email_ids = list_normalized_email_ids(args.db)
    except MasterDataStorageError as exc:
        raise MatchingError(str(exc)) from exc
    results: list[dict[str, Any]] = []
    for email_id in email_ids:
        try:
            payload = match_and_store(args.db, master_data=master, email_id=email_id)
            result = {
                "email_id": email_id,
                "status": payload["status"],
                "normalization_run_id": payload["database"]["normalization_run_id"],
                "matching_run_id": payload["database"]["matching_run_id"],
                "blocking_issue_count": sum(
                    issue["severity"] == "blocking"
                    for issue in payload["matching"]["issues"]
                ),
            }
            results.append(result)
            if args.output_dir is not None:
                write_json(args.output_dir / f"email_{email_id}.json", payload)
        except MatchingError as exc:
            results.append({"email_id": email_id, "status": "failed", "error": str(exc)})
    counts = {
        status: sum(result["status"] == status for result in results)
        for status in ("valid", "needs_review", "failed")
    }
    write_json(
        None,
        {
            "database": str(args.db.expanduser().resolve()),
            "matcher_version": MATCHER_VERSION,
            "master_data_sync": sync_result,
            "counts": counts,
            "results": results,
        },
    )
    return 2 if counts["failed"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MatchingError as exc:
        print(f"Master-data matching failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
