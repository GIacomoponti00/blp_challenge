#!/usr/bin/env python3
"""Deterministically calculate and persist sequential approval-route previews.

Examples:
    python approval_chain.py --db procurement.db --email-id 1
    python approval_chain.py --db procurement.db --matching-run-id 4
    python approval_chain.py --db procurement.db --all
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
    from .approval_storage import (
        ApprovalStorageError,
        list_matched_email_ids,
        load_approval_input,
        save_approval_route_run,
    )
except ImportError:
    from approval_storage import (
        ApprovalStorageError,
        list_matched_email_ids,
        load_approval_input,
        save_approval_route_run,
    )


ROUTE_VERSION = "approval-route-v1"
MONEY_QUANTUM = Decimal("0.01")
IssueSeverity = Literal["blocking", "warning"]


class ApprovalChainError(RuntimeError):
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


def parse_decimal(value: Any, *, field_path: str, issues: list[dict[str, Any]]) -> Decimal | None:
    if value is None:
        add_issue(issues, "MISSING_APPROVAL_AMOUNT", field_path, "A confirmed net total is required")
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        add_issue(issues, "INVALID_APPROVAL_AMOUNT", field_path, f"Invalid decimal value {value!r}")
        return None
    if not parsed.is_finite() or parsed <= 0:
        add_issue(issues, "INVALID_APPROVAL_AMOUNT", field_path, "Approval amount must be finite and greater than zero")
        return None
    return parsed


def money_text(value: Decimal | None) -> str | None:
    return format(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), "f") if value is not None else None


def employee_public(employee: dict[str, Any] | None) -> dict[str, Any] | None:
    if employee is None:
        return None
    return {
        "id": employee.get("id"),
        "name": employee.get("name"),
        "email": employee.get("email"),
        "role": employee.get("role"),
        "deputy_for": employee.get("deputy_for"),
    }


def select_approval_band(
    total_chf: Decimal,
    bands: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Choose the smallest upper bound covering the total.

    This intentionally sends cent-sized gaps in the JSON ranges to the higher
    band, e.g. CHF 1000.50 is above the first upper bound and enters band two.
    """
    parsed: list[tuple[Decimal | None, dict[str, Any]]] = []
    for band in bands:
        try:
            upper = Decimal(str(band["to"])) if band.get("to") is not None else None
            Decimal(str(band["from"]))
        except (KeyError, InvalidOperation, ValueError):
            add_issue(issues, "INVALID_APPROVAL_BAND", "approval_limits_chf", f"Invalid band {band!r}")
            return None
        parsed.append((upper, band))
    parsed.sort(key=lambda item: (item[0] is None, item[0] or Decimal("Infinity")))
    for upper, band in parsed:
        if upper is None or total_chf <= upper:
            required_roles = band.get("required_roles")
            if not isinstance(required_roles, list) or not required_roles:
                add_issue(issues, "INVALID_APPROVAL_BAND", "approval_limits_chf", "Selected band has no required roles")
                return None
            return {
                "from": str(band["from"]),
                "to": str(band["to"]) if band.get("to") is not None else None,
                "required_roles": [str(role) for role in required_roles],
            }
    add_issue(issues, "APPROVAL_BAND_NOT_FOUND", "approval_limits_chf", f"No band covers CHF {money_text(total_chf)}")
    return None


def company_role_holder(
    role: str,
    employees: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        employee
        for employee in employees
        if employee.get("role") == role and not employee.get("deputy_for")
    ]
    if len(candidates) != 1:
        add_issue(
            issues,
            "COMPANY_ROLE_HOLDER_NOT_UNIQUE",
            f"roles.{role}",
            f"Expected exactly one non-deputy holder for {role!r}; found {[item.get('id') for item in candidates]}",
        )
        return None
    return employee_public(candidates[0])


def resolve_logical_assignments(
    required_roles: list[str],
    matching: dict[str, Any],
    master_payload: dict[str, Any],
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    requester = matching.get("requester", {}).get("matched_employee")
    center = matching.get("cost_center", {}).get("matched_cost_center") or {}
    owner = center.get("owner")
    department = center.get("department") or {}
    head = department.get("head")
    if requester is None:
        add_issue(issues, "REQUESTER_REQUIRED_FOR_SELF_APPROVAL_CHECK", "requester", "A matched requester is required")

    assignments: list[dict[str, Any]] = []
    for role in required_roles:
        employee: dict[str, Any] | None
        source: str
        if role == "cost_center_owner":
            employee = employee_public(owner)
            source = "cost_center.owner_employee_id"
        elif role == "department_head":
            employee = employee_public(head)
            source = "cost_center.department.head_employee_id"
        else:
            employee = company_role_holder(role, master_payload.get("employees", []), issues)
            source = f"employees.role={role};non_deputy"
        if employee is None and role in {"cost_center_owner", "department_head"}:
            add_issue(
                issues,
                "COST_CENTER_ROLE_HOLDER_MISSING",
                f"roles.{role}",
                f"Could not resolve required role {role!r} from the matched cost center",
            )
        assignments.append(
            {
                "role_labels": [role],
                "employee": employee,
                "assignment_sources": [source],
                "requires_manual_assignment": employee is None,
            }
        )
    return assignments, requester


def collapse_consecutive(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collapsed: list[dict[str, Any]] = []
    for assignment in assignments:
        current = deepcopy(assignment)
        employee_id = (current.get("employee") or {}).get("id")
        previous_id = (collapsed[-1].get("employee") or {}).get("id") if collapsed else None
        if employee_id is not None and employee_id == previous_id:
            collapsed[-1]["role_labels"].extend(current["role_labels"])
            collapsed[-1]["assignment_sources"].extend(current["assignment_sources"])
        else:
            collapsed.append(current)
    return collapsed


def apply_self_approval_rule(
    assignments: list[dict[str, Any]],
    requester: dict[str, Any] | None,
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if requester is None:
        return assignments, []
    requester_id = requester.get("id")
    transfers: dict[int, dict[str, list[str]]] = {}
    removed: set[int] = set()
    audit: list[dict[str, Any]] = []
    working = deepcopy(assignments)
    for index, step in enumerate(working):
        employee_id = (step.get("employee") or {}).get("id")
        if employee_id != requester_id:
            continue
        target_index = next(
            (
                later
                for later in range(index + 1, len(working))
                if (working[later].get("employee") or {}).get("id") not in {None, requester_id}
            ),
            None,
        )
        if target_index is None:
            step["employee"] = None
            step["assignment_sources"] = ["manual:self_approval_no_later_distinct_role"]
            step["requires_manual_assignment"] = True
            add_issue(
                issues,
                "SELF_APPROVAL_MANUAL_ASSIGNMENT_REQUIRED",
                "approval_steps",
                f"Requester {requester_id} holds {step['role_labels']} and no later distinct approver exists",
            )
            audit.append(
                {
                    "requester_id": requester_id,
                    "role_labels": list(step["role_labels"]),
                    "transferred_to_employee_id": None,
                    "result": "manual_assignment_required",
                }
            )
            continue
        bucket = transfers.setdefault(target_index, {"roles": [], "sources": []})
        bucket["roles"].extend(step["role_labels"])
        bucket["sources"].extend(
            f"self_approval_transfer:{source}" for source in step["assignment_sources"]
        )
        removed.add(index)
        audit.append(
            {
                "requester_id": requester_id,
                "role_labels": list(step["role_labels"]),
                "transferred_to_employee_id": working[target_index]["employee"]["id"],
                "result": "transferred_to_next_distinct_role",
            }
        )

    result: list[dict[str, Any]] = []
    for index, step in enumerate(working):
        if index in removed:
            continue
        if index in transfers:
            step["role_labels"] = transfers[index]["roles"] + step["role_labels"]
            step["assignment_sources"] = transfers[index]["sources"] + step["assignment_sources"]
        result.append(step)
    return result, audit


def finalize_steps(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "sequence": index,
            "role_labels": step["role_labels"],
            "employee": step.get("employee"),
            "assignment_source": "+".join(step["assignment_sources"]),
            "requires_manual_assignment": bool(step.get("requires_manual_assignment")),
        }
        for index, step in enumerate(assignments, start=1)
    ]


def build_approval_route(source: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    matching = source["matching"]
    normalized = source["normalized"]
    master = source["master_payload"]
    amount = parse_decimal(
        normalized.get("totals", {}).get("confirmed_net_total"),
        field_path="totals.confirmed_net_total",
        issues=issues,
    )
    currency_value = normalized.get("currency")
    currency = currency_value.upper() if isinstance(currency_value, str) else None
    rates = master.get("_meta", {}).get("fx_rates_to_chf", {})
    rate: Decimal | None = None
    if currency is None:
        add_issue(issues, "MISSING_APPROVAL_CURRENCY", "currency", "A normalized currency is required")
    elif currency not in rates:
        add_issue(issues, "FX_RATE_NOT_FOUND", "currency", f"No CHF conversion rate exists for {currency}")
    else:
        try:
            rate = Decimal(str(rates[currency]))
            if not rate.is_finite() or rate <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            rate = None
            add_issue(issues, "INVALID_FX_RATE", f"fx_rates_to_chf.{currency}", "FX rate must be positive and finite")

    total_chf = (
        (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        if amount is not None and rate is not None
        else None
    )
    band = (
        select_approval_band(total_chf, master.get("approval_limits_chf", []), issues)
        if total_chf is not None
        else None
    )

    steps: list[dict[str, Any]] = []
    transfers: list[dict[str, Any]] = []
    if band is not None:
        assignments, requester = resolve_logical_assignments(
            band["required_roles"], matching, master, issues
        )
        rules = master.get("rules", {})
        if rules.get("duplicate_role_collapses", True):
            assignments = collapse_consecutive(assignments)
        if rules.get("self_approval_forbidden", True):
            assignments, transfers = apply_self_approval_rule(assignments, requester, issues)
        if rules.get("duplicate_role_collapses", True):
            assignments = collapse_consecutive(assignments)
        steps = finalize_steps(assignments)

    calculation_blockers = [issue for issue in issues if issue["severity"] == "blocking"]
    calculation_status = "needs_review" if calculation_blockers else "valid"
    activation_blockers: list[dict[str, Any]] = []
    if source["normalization_status"] != "valid":
        activation_blockers.append(
            {
                "code": "UPSTREAM_NORMALIZATION_REVIEW_REQUIRED",
                "source_status": source["normalization_status"],
                "message": "Normalization blockers must be resolved before approvals are activated",
            }
        )
    if source["matching_status"] != "valid":
        activation_blockers.append(
            {
                "code": "UPSTREAM_MATCHING_REVIEW_REQUIRED",
                "source_status": source["matching_status"],
                "message": "Master-data matching blockers must be resolved before approvals are activated",
            }
        )
    activation_blockers.extend(
        {
            "code": issue["code"],
            "message": issue["message"],
        }
        for issue in calculation_blockers
    )
    preview_available = band is not None and bool(steps)
    activation_ready = preview_available and not activation_blockers
    status = "valid" if activation_ready else "needs_review"
    return {
        "route_version": ROUTE_VERSION,
        "status": status,
        "calculation_status": calculation_status,
        "route_preview_available": preview_available,
        "activation_ready": activation_ready,
        "sequential": bool(master.get("rules", {}).get("approvals_are_sequential", True)),
        "master_snapshot": {
            "id": source["master_snapshot_id"],
            "source_sha256": source["master_source_sha256"],
        },
        "amount": {
            "original_net_total": money_text(amount),
            "original_currency": currency,
            "fx_rate_to_chf": str(rate) if rate is not None else None,
            "total_chf": money_text(total_chf),
            "rounding": "ROUND_HALF_UP to CHF 0.01",
        },
        "selected_band": band,
        "steps": steps,
        "self_approval_adjustments": transfers,
        "issues": issues,
        "activation_blockers": activation_blockers,
    }


def build_and_store(
    db_path: Path,
    *,
    email_id: int | None = None,
    matching_run_id: int | None = None,
) -> dict[str, Any]:
    try:
        source = load_approval_input(
            db_path,
            ingested_email_id=email_id,
            matching_run_id=matching_run_id,
        )
        route = build_approval_route(source)
        route_run_id = save_approval_route_run(
            db_path,
            matching_run_id=source["matching_run_id"],
            normalization_run_id=source["normalization_run_id"],
            master_snapshot_id=source["master_snapshot_id"],
            status=route["status"],
            route_version=ROUTE_VERSION,
            route=route,
            issues={
                "calculation": route["issues"],
                "activation": route["activation_blockers"],
            },
            steps=route["steps"],
        )
    except ApprovalStorageError as exc:
        raise ApprovalChainError(str(exc)) from exc
    return {
        "schema_version": 1,
        "status": route["status"],
        "database": {
            "path": str(db_path.expanduser().resolve()),
            "ingested_email_id": source["ingested_email_id"],
            "matching_run_id": source["matching_run_id"],
            "normalization_run_id": source["normalization_run_id"],
            "approval_route_run_id": route_run_id,
            "master_snapshot_id": source["master_snapshot_id"],
        },
        "approval_route": route,
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
        description="Calculate and persist deterministic approval-route previews"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--email-id", type=int)
    selector.add_argument("--matching-run-id", type=int)
    selector.add_argument("--all", action="store_true")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.all and args.output is not None:
        raise ApprovalChainError("Use --output-dir, not --output, with --all")
    if not args.all and args.output_dir is not None:
        raise ApprovalChainError("--output-dir requires --all")
    if not args.all:
        payload = build_and_store(
            args.db, email_id=args.email_id, matching_run_id=args.matching_run_id
        )
        write_json(args.output, payload)
        return 0

    try:
        email_ids = list_matched_email_ids(args.db)
    except ApprovalStorageError as exc:
        raise ApprovalChainError(str(exc)) from exc
    results: list[dict[str, Any]] = []
    for email_id in email_ids:
        try:
            payload = build_and_store(args.db, email_id=email_id)
            route = payload["approval_route"]
            results.append(
                {
                    "email_id": email_id,
                    "status": payload["status"],
                    "approval_route_run_id": payload["database"]["approval_route_run_id"],
                    "route_preview_available": route["route_preview_available"],
                    "activation_ready": route["activation_ready"],
                    "step_count": len(route["steps"]),
                }
            )
            if args.output_dir is not None:
                write_json(args.output_dir / f"email_{email_id}.json", payload)
        except ApprovalChainError as exc:
            results.append({"email_id": email_id, "status": "failed", "error": str(exc)})
    counts = {
        status: sum(result["status"] == status for result in results)
        for status in ("valid", "needs_review", "failed")
    }
    write_json(
        None,
        {
            "database": str(args.db.expanduser().resolve()),
            "route_version": ROUTE_VERSION,
            "counts": counts,
            "route_previews": sum(bool(item.get("route_preview_available")) for item in results),
            "activation_ready": sum(bool(item.get("activation_ready")) for item in results),
            "results": results,
        },
    )
    return 2 if counts["failed"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApprovalChainError as exc:
        print(f"Approval-chain construction failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
