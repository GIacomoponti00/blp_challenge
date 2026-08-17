#!/usr/bin/env python3
"""Explicit requisition state transitions, edits, and sequential approvals."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from .procurement_storage import DEFAULT_DB_PATH
except ImportError:
    from procurement_storage import DEFAULT_DB_PATH

try:
    from .approval_chain import build_approval_route
    from .approval_storage import ApprovalStorageError, load_approval_input
except ImportError:
    from approval_chain import build_approval_route
    from approval_storage import ApprovalStorageError, load_approval_input

try:
    from .workflow_storage import (
        WorkflowStorageError,
        compact_json,
        get_requisition,
        list_requisitions,
        list_route_email_ids,
        load_route_bundle,
        open_workflow_database,
        utc_now,
    )
except ImportError:
    from workflow_storage import (
        WorkflowStorageError,
        compact_json,
        get_requisition,
        list_requisitions,
        list_route_email_ids,
        load_route_bundle,
        open_workflow_database,
        utc_now,
    )


ROUTE_AFFECTING_FIELDS = {"total", "currency", "cost_centre", "requester_id"}
EDITABLE_TOP_LEVEL_FIELDS = {
    "supplier_id",
    "currency",
    "total",
    "requested_delivery_date",
    "cost_centre",
    "gl_account",
    "tax_amount",
    "requester_id",
}
EDITABLE_LINE_FIELDS = {"item_name", "item_code", "quantity", "unit_price", "unit"}


class WorkflowError(RuntimeError):
    pass


def canonical_requisition_data(bundle: dict[str, Any]) -> dict[str, Any]:
    normalized = bundle["normalized"]
    matching = bundle["matching"]
    supplier = matching.get("supplier", {}).get("matched_supplier") or {}
    requester = matching.get("requester", {}).get("matched_employee") or {}
    cost_center = matching.get("cost_center", {}).get("matched_cost_center") or {}
    lines = [
        {
            "item_name": line.get("item_name"),
            "item_code": line.get("item_code"),
            "quantity": line.get("quantity"),
            "unit_price": line.get("normalized_unit_price"),
            "unit": line.get("unit"),
        }
        for line in normalized.get("lines", [])
    ]
    return {
        "supplier_id": supplier.get("id"),
        "currency": normalized.get("currency"),
        "total": normalized.get("totals", {}).get("confirmed_net_total"),
        "requested_delivery_date": normalized.get("dates", {}).get(
            "requested_delivery_date"
        ),
        "cost_centre": cost_center.get("code"),
        "gl_account": None,
        "tax_amount": normalized.get("totals", {}).get("stated_tax_amount"),
        "requester_id": requester.get("id"),
        "requisition_reference": bundle["source_filename"],
        "subject": bundle.get("subject"),
        "line_items": lines,
        "source": {
            "ingested_email_id": bundle["ingested_email_id"],
            "normalization_run_id": bundle["normalization_run_id"],
            "matching_run_id": bundle["matching_run_id"],
            "approval_route_run_id": bundle["approval_route_run_id"],
        },
    }


def route_basis(data: dict[str, Any]) -> dict[str, Any]:
    return {field: data.get(field) for field in sorted(ROUTE_AFFECTING_FIELDS)}


def add_event(
    connection: sqlite3.Connection,
    *,
    requisition_id: int,
    event_type: str,
    actor: str,
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str | None = None,
    details: Any = None,
) -> None:
    connection.execute(
        """
        INSERT INTO workflow_events(
            requisition_id, event_type, from_state, to_state, actor,
            reason, details_json, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            requisition_id,
            event_type,
            from_state,
            to_state,
            actor,
            reason,
            compact_json(details) if details is not None else None,
            utc_now(),
        ),
    )


def initialize_requisition(db_path: Path, *, email_id: int) -> dict[str, Any]:
    try:
        bundle = load_route_bundle(db_path, ingested_email_id=email_id)
    except WorkflowStorageError as exc:
        raise WorkflowError(str(exc)) from exc
    data = canonical_requisition_data(bundle)
    route = bundle["route"]
    connection = open_workflow_database(db_path)
    try:
        with connection:
            existing = connection.execute(
                "SELECT id, state, version FROM requisitions WHERE ingested_email_id = ?",
                (email_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "action": "unchanged",
                    "requisition_id": int(existing["id"]),
                    "state": existing["state"],
                    "version": int(existing["version"]),
                }
            pending_duplicate = connection.execute(
                """
                SELECT 1 FROM duplicate_links
                WHERE resolution = 'pending'
                  AND right_ingested_email_id = ?
                LIMIT 1
                """,
                (email_id,),
            ).fetchone()
            can_activate = bool(route.get("activation_ready")) and pending_duplicate is None
            state = "pending_approval" if can_activate else "needs_review"
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO requisitions(
                    ingested_email_id, approval_route_run_id, state, version,
                    data_json, route_basis_json, requires_route_rebuild,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, 1, ?, ?, 0, ?, ?)
                """,
                (
                    email_id,
                    bundle["approval_route_run_id"],
                    state,
                    compact_json(data),
                    compact_json(route_basis(data)),
                    now,
                    now,
                ),
            )
            requisition_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO requisition_revisions(
                    requisition_id, version, data_json, changed_fields_json,
                    actor, reason, created_at_utc
                ) VALUES (?, 1, ?, ?, 'system', 'Initial workflow projection', ?)
                """,
                (requisition_id, compact_json(data), compact_json(["*"]), now),
            )
            for index, step in enumerate(bundle["route_steps"], start=1):
                employee = step.get("employee") or {}
                status = (
                    "active" if can_activate and index == 1 else
                    "pending" if can_activate else "draft"
                )
                connection.execute(
                    """
                    INSERT INTO workflow_approval_steps(
                        requisition_id, route_step_sequence, employee_id,
                        employee_name, employee_email, role_labels_json,
                        status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        requisition_id,
                        index,
                        employee.get("id"),
                        employee.get("name"),
                        employee.get("email"),
                        compact_json(step["role_labels"]),
                        status,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO requisition_route_revisions(
                    requisition_id, requisition_version, route_json, steps_json,
                    actor, reason, created_at_utc
                ) VALUES (?, 1, ?, ?, 'system', 'Initial approval route', ?)
                """,
                (
                    requisition_id,
                    compact_json(route),
                    compact_json(bundle["route_steps"]),
                    now,
                ),
            )
            add_event(
                connection,
                requisition_id=requisition_id,
                event_type="ingested",
                actor="system",
                to_state="inbox",
            )
            add_event(
                connection,
                requisition_id=requisition_id,
                event_type="extracted",
                actor="system",
                from_state="inbox",
                to_state="extracted",
            )
            add_event(
                connection,
                requisition_id=requisition_id,
                event_type="workflow_initialized",
                actor="system",
                from_state="extracted",
                to_state=state,
                details={
                    "approval_route_run_id": bundle["approval_route_run_id"],
                    "activation_ready": can_activate,
                    "pending_duplicate": pending_duplicate is not None,
                },
            )
            return {
                "action": "inserted",
                "requisition_id": requisition_id,
                "state": state,
                "version": 1,
            }
    except sqlite3.Error as exc:
        raise WorkflowError(f"Could not initialize requisition: {exc}") from exc
    finally:
        connection.close()


def initialize_all(db_path: Path) -> dict[str, Any]:
    try:
        email_ids = list_route_email_ids(db_path)
    except WorkflowStorageError as exc:
        raise WorkflowError(str(exc)) from exc
    results = []
    for email_id in email_ids:
        try:
            results.append({"email_id": email_id, **initialize_requisition(db_path, email_id=email_id)})
        except WorkflowError as exc:
            results.append({"email_id": email_id, "action": "failed", "error": str(exc)})
    return {
        "counts": {
            action: sum(item["action"] == action for item in results)
            for action in ("inserted", "unchanged", "failed")
        },
        "results": results,
    }


def validate_data(connection: sqlite3.Connection, requisition: dict[str, Any]) -> list[dict[str, str]]:
    data = requisition["data"]
    issues: list[dict[str, str]] = []

    def issue(code: str, field: str, message: str) -> None:
        issues.append({"code": code, "field_path": field, "message": message})

    for field in ("supplier_id", "currency", "total", "requested_delivery_date"):
        if data.get(field) in (None, ""):
            issue("MISSING_REQUIRED_FIELD", field, f"{field} is required")
    if data.get("currency") not in {"CHF", "EUR", "USD", "GBP"}:
        issue("INVALID_CURRENCY", "currency", "Currency must be CHF, EUR, USD, or GBP")
    try:
        if Decimal(str(data.get("total"))) <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        issue("INVALID_TOTAL", "total", "Total must be a positive decimal")
    delivery = data.get("requested_delivery_date")
    if isinstance(delivery, str):
        try:
            date.fromisoformat(delivery)
        except ValueError:
            issue("INVALID_DELIVERY_DATE", "requested_delivery_date", "Use YYYY-MM-DD")
    supplier = connection.execute(
        "SELECT status FROM master_suppliers WHERE id = ?", (data.get("supplier_id"),)
    ).fetchone()
    if supplier is None:
        issue("SUPPLIER_NOT_FOUND", "supplier_id", "Supplier ID is not in current master data")
    elif supplier["status"] != "active":
        issue("SUPPLIER_NOT_ACTIVE", "supplier_id", f"Supplier status is {supplier['status']!r}")
    requester_id = data.get("requester_id")
    if not requester_id:
        issue("MISSING_REQUESTER", "requester_id", "A requester is required for approval routing")
    elif connection.execute(
        "SELECT 1 FROM master_employees WHERE id = ?", (requester_id,)
    ).fetchone() is None:
        issue("REQUESTER_NOT_FOUND", "requester_id", "Requester is not in current master data")
    cost_centre = data.get("cost_centre")
    if not cost_centre:
        issue("MISSING_COST_CENTER", "cost_centre", "A cost center is required for approval routing")
    elif connection.execute(
        "SELECT 1 FROM master_cost_centers WHERE code = ?", (cost_centre,)
    ).fetchone() is None:
        issue("COST_CENTER_NOT_FOUND", "cost_centre", "Cost center is not in current master data")
    gl_account = data.get("gl_account")
    if gl_account and connection.execute(
        "SELECT 1 FROM master_gl_accounts WHERE code = ?", (gl_account,)
    ).fetchone() is None:
        issue("GL_ACCOUNT_NOT_FOUND", "gl_account", "GL account is not in current master data")
    lines = data.get("line_items")
    computed_total = Decimal("0")
    lines_numeric = True
    if not isinstance(lines, list) or not lines:
        issue("NO_LINES", "line_items", "At least one line is required")
    else:
        for index, line in enumerate(lines):
            for field in ("item_name", "quantity", "unit_price", "unit"):
                if line.get(field) in (None, ""):
                    issue("MISSING_LINE_FIELD", f"line_items[{index}].{field}", f"{field} is required")
            for field in ("quantity", "unit_price"):
                try:
                    number = Decimal(str(line.get(field)))
                    if not number.is_finite() or (field == "quantity" and number <= 0):
                        raise InvalidOperation
                except (InvalidOperation, TypeError, ValueError):
                    issue("INVALID_LINE_NUMBER", f"line_items[{index}].{field}", "Must be a decimal")
                    lines_numeric = False
            if lines_numeric:
                computed_total += Decimal(str(line.get("quantity"))) * Decimal(str(line.get("unit_price")))
    if lines_numeric and isinstance(lines, list) and lines:
        try:
            stated_total = Decimal(str(data.get("total")))
            if abs(stated_total - computed_total) > Decimal("0.01"):
                issue(
                    "NET_TOTAL_MISMATCH",
                    "total",
                    f"Net total {stated_total} must equal the net line sum {computed_total}",
                )
        except (InvalidOperation, TypeError, ValueError):
            pass
    if requisition["requires_route_rebuild"]:
        issue("ROUTE_REBUILD_REQUIRED", "approval_route", "A route-affecting field changed")
    if route_basis(data) != requisition["route_basis"]:
        issue("ROUTE_BASIS_CHANGED", "approval_route", "Total, currency, cost center, or requester differs from the route snapshot")
    unassigned = connection.execute(
        "SELECT route_step_sequence FROM workflow_approval_steps WHERE requisition_id = ? AND employee_id IS NULL",
        (requisition["id"],),
    ).fetchall()
    for row in unassigned:
        issue("MANUAL_APPROVER_REQUIRED", f"approval_steps[{row['route_step_sequence']}]", "Assign an approver")
    duplicate = connection.execute(
        """
        SELECT id FROM duplicate_links
        WHERE resolution = 'pending'
          AND right_ingested_email_id = ?
        LIMIT 1
        """,
        (requisition["ingested_email_id"],),
    ).fetchone()
    if duplicate is not None:
        issue("DUPLICATE_REVIEW_REQUIRED", "duplicate_links", f"Resolve duplicate link {duplicate['id']}")
    return issues


def validate_requisition(db_path: Path, requisition_id: int) -> dict[str, Any]:
    requisition = get_requisition(db_path, requisition_id)
    connection = open_workflow_database(db_path)
    try:
        issues = validate_data(connection, requisition)
    finally:
        connection.close()
    return {"requisition_id": requisition_id, "valid": not issues, "issues": issues}


def correction_path_parts(field_path: str) -> tuple[str, int | None, str | None]:
    if field_path in EDITABLE_TOP_LEVEL_FIELDS:
        return field_path, None, None
    match = re.fullmatch(r"line_items\[(\d+)]\.([a-z_]+)", field_path)
    if not match or match.group(2) not in EDITABLE_LINE_FIELDS:
        raise WorkflowError(f"Unsupported editable field path: {field_path!r}")
    return "line_items", int(match.group(1)), match.group(2)


def set_field(data: dict[str, Any], field_path: str, value: Any) -> Any:
    root, index, field = correction_path_parts(field_path)
    if root != "line_items":
        previous = data.get(root)
        data[root] = value
        return previous
    lines = data.get("line_items")
    if not isinstance(lines, list) or index is None or index >= len(lines):
        raise WorkflowError(f"Line index is out of range: {field_path!r}")
    previous = lines[index].get(field)
    lines[index][field] = value
    return previous


def edit_requisition(
    db_path: Path,
    *,
    requisition_id: int,
    expected_version: int,
    field_path: str,
    value: Any,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    if not actor.strip() or not reason.strip():
        raise WorkflowError("Actor and reason are required")
    connection = open_workflow_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM requisitions WHERE id = ?", (requisition_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"Requisition ID {requisition_id} does not exist")
        if int(row["version"]) != expected_version:
            raise WorkflowError(f"Stale version: expected {expected_version}, current is {row['version']}")
        if row["state"] in {"ordered", "duplicate", "rejected", "ordering"}:
            raise WorkflowError(f"Requisition in state {row['state']!r} cannot be edited")
        data = json.loads(row["data_json"])
        previous = set_field(data, field_path, value)
        old_state = row["state"]
        new_state = "needs_review"
        new_version = expected_version + 1
        root = correction_path_parts(field_path)[0]
        rebuild = bool(row["requires_route_rebuild"]) or root in ROUTE_AFFECTING_FIELDS
        connection.execute(
            """
            UPDATE requisitions
            SET state = ?, version = ?, data_json = ?, requires_route_rebuild = ?, updated_at_utc = ?
            WHERE id = ? AND version = ?
            """,
            (new_state, new_version, compact_json(data), int(rebuild), utc_now(), requisition_id, expected_version),
        )
        connection.execute(
            """
            UPDATE workflow_approval_steps
            SET status = 'cancelled'
            WHERE requisition_id = ? AND status IN ('active', 'pending', 'approved')
            """,
            (requisition_id,),
        )
        connection.execute(
            """
            INSERT INTO requisition_revisions(
                requisition_id, version, data_json, changed_fields_json,
                actor, reason, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (requisition_id, new_version, compact_json(data), compact_json([field_path]), actor.strip(), reason.strip(), utc_now()),
        )
        add_event(
            connection,
            requisition_id=requisition_id,
            event_type="material_edit",
            actor=actor.strip(),
            from_state=old_state,
            to_state=new_state,
            reason=reason.strip(),
            details={"field_path": field_path, "previous": previous, "new": value, "route_rebuild": rebuild},
        )
        connection.commit()
        return {"requisition_id": requisition_id, "state": new_state, "version": new_version, "requires_route_rebuild": rebuild}
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        connection.rollback()
        raise WorkflowError(f"Could not edit requisition: {exc}") from exc
    except WorkflowError:
        connection.rollback()
        raise
    finally:
        connection.close()


def _employee_by_id(master: dict[str, Any], employee_id: Any) -> dict[str, Any] | None:
    return next(
        (item for item in master.get("employees", []) if item.get("id") == employee_id),
        None,
    )


def revised_route_source(db_path: Path, requisition: dict[str, Any]) -> dict[str, Any]:
    """Overlay reviewed route fields on the immutable upstream approval input."""
    matching_run_id = requisition["data"].get("source", {}).get("matching_run_id")
    if not matching_run_id:
        raise WorkflowError("Requisition has no source matching run")
    try:
        source = load_approval_input(db_path, matching_run_id=int(matching_run_id))
    except ApprovalStorageError as exc:
        raise WorkflowError(f"Could not load route source: {exc}") from exc
    data = requisition["data"]
    master = source["master_payload"]
    source["normalized"]["currency"] = data.get("currency")
    source["normalized"].setdefault("totals", {})["confirmed_net_total"] = data.get("total")

    requester = _employee_by_id(master, data.get("requester_id"))
    source["matching"]["requester"] = {
        "matched_employee": requester,
        "match_method": "reviewed_employee_id" if requester else None,
    }

    center = next(
        (
            item
            for item in master.get("cost_centers", [])
            if item.get("code") == data.get("cost_centre")
        ),
        None,
    )
    matched_center = None
    if center is not None:
        department_code = center.get("department_code") or center.get("department")
        department = next(
            (
                item
                for item in master.get("departments", [])
                if item.get("code") == department_code
            ),
            None,
        )
        owner = _employee_by_id(master, center.get("owner_employee_id"))
        head = _employee_by_id(master, department.get("head_employee_id")) if department else None
        matched_center = {
            "code": center.get("code"),
            "name": center.get("name"),
            "owner": owner,
            "department": {
                "code": department.get("code"),
                "name": department.get("name"),
                "head": head,
            }
            if department
            else None,
        }
    source["matching"]["cost_center"] = {
        "matched_cost_center": matched_center,
        "match_method": "reviewed_exact_code" if matched_center else None,
    }
    # These four values have now been explicitly reviewed.  The route builder
    # still emits calculation/master-data blockers if any ID is unusable.
    source["normalization_status"] = "valid"
    source["matching_status"] = "valid"
    return source


def rebuild_approval_route(
    db_path: Path,
    *,
    requisition_id: int,
    expected_version: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    if not actor.strip() or not reason.strip():
        raise WorkflowError("Actor and reason are required")
    requisition = get_requisition(db_path, requisition_id)
    if requisition["state"] != "needs_review":
        raise WorkflowError("Route rebuild requires needs_review state")
    if requisition["version"] != expected_version:
        raise WorkflowError(f"Stale version: current is {requisition['version']}")
    if not requisition["requires_route_rebuild"]:
        raise WorkflowError("No route-affecting field has changed")
    route = build_approval_route(revised_route_source(db_path, requisition))
    if not route.get("route_preview_available") or not route.get("steps"):
        messages = "; ".join(item["message"] for item in route.get("issues", []))
        raise WorkflowError(f"A usable route could not be rebuilt: {messages or 'unknown route error'}")

    connection = open_workflow_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT state, version, data_json FROM requisitions WHERE id = ?",
            (requisition_id,),
        ).fetchone()
        if current is None or current["state"] != "needs_review":
            raise WorkflowError("Requisition changed before route rebuild")
        if int(current["version"]) != expected_version:
            raise WorkflowError(f"Stale version: current is {current['version']}")
        data = json.loads(current["data_json"])
        new_version = expected_version + 1
        now = utc_now()
        connection.execute(
            "DELETE FROM workflow_approval_steps WHERE requisition_id = ?",
            (requisition_id,),
        )
        snapshot_steps = []
        for sequence, step in enumerate(route["steps"], start=1):
            employee = step.get("employee") or {}
            connection.execute(
                """
                INSERT INTO workflow_approval_steps(
                    requisition_id, route_step_sequence, employee_id,
                    employee_name, employee_email, role_labels_json,
                    status, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (
                    requisition_id,
                    sequence,
                    employee.get("id"),
                    employee.get("name"),
                    employee.get("email"),
                    compact_json(step["role_labels"]),
                    now,
                ),
            )
            snapshot_steps.append(
                {
                    "sequence": sequence,
                    "employee": employee or None,
                    "role_labels": step["role_labels"],
                    "requires_manual_assignment": bool(step.get("requires_manual_assignment")),
                }
            )
        connection.execute(
            """
            UPDATE requisitions
            SET version = ?, route_basis_json = ?, requires_route_rebuild = 0,
                updated_at_utc = ?
            WHERE id = ? AND version = ?
            """,
            (new_version, compact_json(route_basis(data)), now, requisition_id, expected_version),
        )
        connection.execute(
            """
            INSERT INTO requisition_route_revisions(
                requisition_id, requisition_version, route_json, steps_json,
                actor, reason, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                requisition_id,
                new_version,
                compact_json(route),
                compact_json(snapshot_steps),
                actor.strip(),
                reason.strip(),
                now,
            ),
        )
        add_event(
            connection,
            requisition_id=requisition_id,
            event_type="approval_route_rebuilt",
            actor=actor.strip(),
            reason=reason.strip(),
            details={
                "requisition_version": new_version,
                "amount": route.get("amount"),
                "selected_band": route.get("selected_band"),
                "step_count": len(snapshot_steps),
            },
        )
        connection.commit()
        return {
            "requisition_id": requisition_id,
            "state": "needs_review",
            "version": new_version,
            "requires_route_rebuild": False,
            "route_status": route["status"],
            "steps": snapshot_steps,
        }
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        connection.rollback()
        raise WorkflowError(f"Could not rebuild approval route: {exc}") from exc
    except WorkflowError:
        connection.rollback()
        raise
    finally:
        connection.close()


def assign_manual_approver(
    db_path: Path,
    *,
    requisition_id: int,
    expected_version: int,
    sequence: int,
    employee_id: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    connection = open_workflow_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        req = connection.execute("SELECT state, version FROM requisitions WHERE id = ?", (requisition_id,)).fetchone()
        if req is None or req["state"] != "needs_review":
            raise WorkflowError("Manual assignment requires a requisition in needs_review")
        if int(req["version"]) != expected_version:
            raise WorkflowError(f"Stale version: current is {req['version']}")
        employee = connection.execute("SELECT id, name, email FROM master_employees WHERE id = ?", (employee_id,)).fetchone()
        if employee is None:
            raise WorkflowError(f"Employee {employee_id!r} is not in master data")
        cursor = connection.execute(
            """
            UPDATE workflow_approval_steps
            SET employee_id = ?, employee_name = ?, employee_email = ?, status = 'draft'
            WHERE requisition_id = ? AND route_step_sequence = ?
            """,
            (employee["id"], employee["name"], employee["email"], requisition_id, sequence),
        )
        if cursor.rowcount != 1:
            raise WorkflowError(f"Approval step {sequence} does not exist")
        new_version = expected_version + 1
        connection.execute(
            "UPDATE requisitions SET version = ?, updated_at_utc = ? WHERE id = ?",
            (new_version, utc_now(), requisition_id),
        )
        add_event(
            connection,
            requisition_id=requisition_id,
            event_type="manual_approver_assigned",
            actor=actor,
            reason=reason,
            details={"sequence": sequence, "employee_id": employee_id},
        )
        connection.commit()
        return {"requisition_id": requisition_id, "version": new_version, "sequence": sequence, "employee_id": employee_id}
    except sqlite3.Error as exc:
        connection.rollback()
        raise WorkflowError(f"Could not assign manual approver: {exc}") from exc
    except WorkflowError:
        connection.rollback()
        raise
    finally:
        connection.close()


def resolve_review(
    db_path: Path, *, requisition_id: int, expected_version: int, actor: str
) -> dict[str, Any]:
    requisition = get_requisition(db_path, requisition_id)
    if requisition["state"] != "needs_review":
        raise WorkflowError("Only needs_review requisitions can enter approval")
    if requisition["version"] != expected_version:
        raise WorkflowError(f"Stale version: current is {requisition['version']}")
    connection = open_workflow_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT state, version FROM requisitions WHERE id = ?", (requisition_id,)
        ).fetchone()
        if current is None or current["state"] != "needs_review":
            raise WorkflowError("Requisition changed before review was resolved")
        if int(current["version"]) != expected_version:
            raise WorkflowError(f"Stale version: current is {current['version']}")
        issues = validate_data(connection, requisition)
        if issues:
            raise WorkflowError("Review is not resolved: " + "; ".join(item["message"] for item in issues))
        steps = connection.execute(
            "SELECT id FROM workflow_approval_steps WHERE requisition_id = ? ORDER BY route_step_sequence",
            (requisition_id,),
        ).fetchall()
        if not steps:
            raise WorkflowError("No approval route steps exist")
        connection.execute(
            "UPDATE workflow_approval_steps SET status = 'pending', acted_by = NULL, action_reason = NULL, acted_at_utc = NULL WHERE requisition_id = ?",
            (requisition_id,),
        )
        connection.execute("UPDATE workflow_approval_steps SET status = 'active' WHERE id = ?", (steps[0]["id"],))
        new_version = expected_version + 1
        connection.execute(
            "UPDATE requisitions SET state = 'pending_approval', version = ?, updated_at_utc = ? WHERE id = ? AND version = ?",
            (new_version, utc_now(), requisition_id, expected_version),
        )
        add_event(
            connection,
            requisition_id=requisition_id,
            event_type="review_resolved",
            actor=actor,
            from_state="needs_review",
            to_state="pending_approval",
        )
        connection.commit()
        return {"requisition_id": requisition_id, "state": "pending_approval", "version": new_version}
    except sqlite3.Error as exc:
        connection.rollback()
        raise WorkflowError(f"Could not resolve review: {exc}") from exc
    except WorkflowError:
        connection.rollback()
        raise
    finally:
        connection.close()


def act_on_approval(
    db_path: Path,
    *,
    requisition_id: int,
    expected_version: int,
    actor_employee_id: str,
    action: str,
    reason: str | None = None,
) -> dict[str, Any]:
    if action not in {"approve", "reject"}:
        raise WorkflowError(f"Unsupported approval action: {action}")
    if action == "reject" and not (reason or "").strip():
        raise WorkflowError("A rejection reason is required")
    connection = open_workflow_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        req = connection.execute("SELECT * FROM requisitions WHERE id = ?", (requisition_id,)).fetchone()
        if req is None or req["state"] != "pending_approval":
            raise WorkflowError("Approval action requires pending_approval state")
        if int(req["version"]) != expected_version:
            raise WorkflowError(f"Stale version: current is {req['version']}")
        active = connection.execute(
            "SELECT * FROM workflow_approval_steps WHERE requisition_id = ? AND status = 'active'",
            (requisition_id,),
        ).fetchall()
        if len(active) != 1:
            raise WorkflowError(f"Expected one active approval step; found {len(active)}")
        step = active[0]
        if step["employee_id"] != actor_employee_id:
            raise WorkflowError(f"Only active approver {step['employee_id']} may act")
        data = json.loads(req["data_json"])
        if data.get("requester_id") == actor_employee_id:
            raise WorkflowError("Requester self-approval is forbidden")
        now = utc_now()
        new_version = expected_version + 1
        if action == "reject":
            connection.execute(
                "UPDATE workflow_approval_steps SET status='rejected', acted_by=?, action_reason=?, acted_at_utc=? WHERE id=?",
                (actor_employee_id, reason.strip(), now, step["id"]),
            )
            connection.execute(
                "UPDATE workflow_approval_steps SET status='cancelled' WHERE requisition_id=? AND status='pending'",
                (requisition_id,),
            )
            new_state = "rejected"
        else:
            connection.execute(
                "UPDATE workflow_approval_steps SET status='approved', acted_by=?, acted_at_utc=? WHERE id=?",
                (actor_employee_id, now, step["id"]),
            )
            next_step = connection.execute(
                "SELECT id FROM workflow_approval_steps WHERE requisition_id=? AND status='pending' ORDER BY route_step_sequence LIMIT 1",
                (requisition_id,),
            ).fetchone()
            if next_step is None:
                new_state = "approved"
            else:
                connection.execute("UPDATE workflow_approval_steps SET status='active' WHERE id=?", (next_step["id"],))
                new_state = "pending_approval"
        connection.execute(
            "UPDATE requisitions SET state=?, version=?, updated_at_utc=? WHERE id=? AND version=?",
            (new_state, new_version, now, requisition_id, expected_version),
        )
        add_event(
            connection,
            requisition_id=requisition_id,
            event_type=f"approval_{action}d" if action == "approve" else "approval_rejected",
            actor=actor_employee_id,
            from_state="pending_approval",
            to_state=new_state,
            reason=reason,
            details={"step_sequence": step["route_step_sequence"]},
        )
        connection.commit()
        return {"requisition_id": requisition_id, "state": new_state, "version": new_version, "action": action}
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        connection.rollback()
        raise WorkflowError(f"Could not perform approval action: {exc}") from exc
    except WorkflowError:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_json(path: Path | None, payload: Any) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        sys.stdout.write(rendered)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, path)


def parse_value(args: argparse.Namespace) -> Any:
    if getattr(args, "clear", False):
        return None
    if getattr(args, "value", None) is not None:
        return args.value
    try:
        return json.loads(args.value_json)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Invalid --value-json: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize and operate requisition workflows")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    target = init.add_mutually_exclusive_group(required=True)
    target.add_argument("--email-id", type=int)
    target.add_argument("--all", action="store_true")
    sub.add_parser("list")
    show = sub.add_parser("show"); show.add_argument("--id", type=int, required=True)
    validate = sub.add_parser("validate"); validate.add_argument("--id", type=int, required=True)
    edit = sub.add_parser("edit")
    edit.add_argument("--id", type=int, required=True); edit.add_argument("--version", type=int, required=True)
    edit.add_argument("--field", required=True); edit.add_argument("--actor", required=True); edit.add_argument("--reason", required=True)
    values = edit.add_mutually_exclusive_group(required=True)
    values.add_argument("--value"); values.add_argument("--value-json"); values.add_argument("--clear", action="store_true")
    resolve = sub.add_parser("resolve-review")
    resolve.add_argument("--id", type=int, required=True); resolve.add_argument("--version", type=int, required=True); resolve.add_argument("--actor", required=True)
    assign = sub.add_parser("assign-approver")
    assign.add_argument("--id", type=int, required=True); assign.add_argument("--version", type=int, required=True)
    assign.add_argument("--sequence", type=int, required=True); assign.add_argument("--employee-id", required=True)
    assign.add_argument("--actor", required=True); assign.add_argument("--reason", required=True)
    rebuild = sub.add_parser("rebuild-route")
    rebuild.add_argument("--id", type=int, required=True); rebuild.add_argument("--version", type=int, required=True)
    rebuild.add_argument("--actor", required=True); rebuild.add_argument("--reason", required=True)
    for name in ("approve", "reject"):
        action = sub.add_parser(name)
        action.add_argument("--id", type=int, required=True); action.add_argument("--version", type=int, required=True)
        action.add_argument("--actor-employee-id", required=True)
        if name == "reject": action.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        result = initialize_all(args.db) if args.all else initialize_requisition(args.db, email_id=args.email_id)
    elif args.command == "list": result = list_requisitions(args.db)
    elif args.command == "show": result = get_requisition(args.db, args.id)
    elif args.command == "validate": result = validate_requisition(args.db, args.id)
    elif args.command == "edit":
        result = edit_requisition(args.db, requisition_id=args.id, expected_version=args.version, field_path=args.field, value=parse_value(args), actor=args.actor, reason=args.reason)
    elif args.command == "resolve-review":
        result = resolve_review(args.db, requisition_id=args.id, expected_version=args.version, actor=args.actor)
    elif args.command == "assign-approver":
        result = assign_manual_approver(args.db, requisition_id=args.id, expected_version=args.version, sequence=args.sequence, employee_id=args.employee_id, actor=args.actor, reason=args.reason)
    elif args.command == "rebuild-route":
        result = rebuild_approval_route(args.db, requisition_id=args.id, expected_version=args.version, actor=args.actor, reason=args.reason)
    elif args.command in {"approve", "reject"}:
        result = act_on_approval(args.db, requisition_id=args.id, expected_version=args.version, actor_employee_id=args.actor_employee_id, action=args.command, reason=getattr(args, "reason", None))
    else: raise WorkflowError(f"Unsupported command {args.command}")
    write_json(None, result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WorkflowError, WorkflowStorageError) as exc:
        print(f"Workflow failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
